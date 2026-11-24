import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Project, SpecItem
from worker.git_bridge import GitBridge, PROJECT_METADATA


logger = logging.getLogger(__name__)
METADATA_FIELDS = dict(zip(PROJECT_METADATA, ("spec_content", "plan_content", "context_content")))


async def migrate_workspace_metadata(session: AsyncSession, project: Project) -> None:
    if not project.workspace_path:
        return
    workspace = Path(project.workspace_path)
    if not workspace.is_dir():
        return

    pending = {name.casefold(): field for name, field in METADATA_FIELDS.items()
               if getattr(project, field) is None}

    def read_pending_sources():
        return {
            pending[path.name.casefold()]: path.read_bytes().decode("utf-8")
            for path in workspace.iterdir()
            if path.name.casefold() in pending and not path.is_symlink() and path.is_file()
        }

    for field, content in (await asyncio.to_thread(read_pending_sources)).items():
        setattr(project, field, content)
    await session.commit()
    await GitBridge(workspace, project.id).clean_project_metadata()


async def migrate_project_metadata(session: AsyncSession) -> None:
    projects = list(await session.scalars(select(Project)))
    for project in projects:
        try:
            await migrate_workspace_metadata(session, project)
        except (OSError, UnicodeError, RuntimeError, TimeoutError):
            logger.exception("Could not migrate workspace metadata for project %s", project.id)


async def build_spec(session: AsyncSession, project: Project) -> None:
    items = await session.scalars(
        select(SpecItem)
        .where(SpecItem.project_id == project.id)
        .order_by(SpecItem.position, SpecItem.id)
    )
    sections = [f"# Техническое задание — {' '.join(project.name.split())}"]
    if project.description:
        sections.append(project.description.strip())
    for index, item in enumerate(items, start=1):
        title = " ".join(item.title.split())
        sections.append(f"## {index}. {title}\n\n{item.content.strip()}")
    project.spec_content = "\n\n".join(sections) + "\n"
