import asyncio
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Project, SpecItem


def _write_spec(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary_path = Path(stream.name)
        try:
            stream.write(content.encode("utf-8"))
            stream.close()
            temporary_path.replace(path)
        finally:
            stream.close()
            temporary_path.unlink(missing_ok=True)
    return path


async def build_spec(session: AsyncSession, project: Project) -> Path:
    if not project.workspace_path:
        raise ValueError("Для проекта не задана рабочая папка.")
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
    path = Path(project.workspace_path) / "SPEC.md"
    return await asyncio.to_thread(_write_spec, path, "\n\n".join(sections) + "\n")
