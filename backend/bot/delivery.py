import asyncio
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from aiogram.types import FSInputFile
from fastapi import HTTPException

from backend.bot.i18n import localize_markup, translate
from backend.bot.keyboards import inline_menu
from backend.database.db import async_session_factory

logger = logging.getLogger(__name__)


def archive_workspace(workspace: Path, archive: Path) -> None:
    workspace = workspace.resolve(strict=True)

    def raise_walk_error(error):
        raise error

    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for directory, dirs, files in os.walk(workspace, onerror=raise_walk_error):
            parent = Path(directory)
            dirs[:] = sorted(name for name in dirs if name.casefold() != ".git"
                             and not (parent / name).is_symlink() and (parent / name).resolve() == parent / name)
            for name in sorted(files):
                path = parent / name
                if name.casefold() == ".git" or path.is_symlink() or path.resolve() != path or not path.is_file():
                    continue
                output.write(path, path.relative_to(workspace))


async def send_mobile_result(bot, project, owner) -> None:
    if project.delivery_mode != "mobile" or not owner.telegram_id:
        return
    try:
        with TemporaryDirectory(prefix="loomis-export-") as folder:
            archive = Path(folder) / f"Loomis-{project.id}.zip"
            # Let the writer close the ZIP before removing the temporary directory on cancellation.
            writer = asyncio.create_task(asyncio.to_thread(archive_workspace, Path(project.workspace_path), archive))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await asyncio.gather(writer, return_exceptions=True)
                raise
            await bot.send_document(
                owner.telegram_id, FSInputFile(archive),
                caption=translate(f"Сохраните архив в папку телефона: {owner.mobile_download_dir}", owner.language),
                parse_mode=None,
            )
    except Exception:
        logger.exception("Could not deliver mobile result for project %s", project.id)
        markup = inline_menu([[("Отправить архив", f"mobile_result:{project.id}")]])
        await bot.send_message(
            owner.telegram_id, translate("Не удалось отправить архив проекта.", owner.language),
            reply_markup=localize_markup(markup, owner.language),
        )


async def completion_summary(project):
    from backend.api.projects import project_commits, project_stats

    async with async_session_factory() as session:
        stats = await project_stats(project, session)
    try:
        commits = str(len(await project_commits(project)))
    except HTTPException:
        commits = "—"
    duration = round(stats["duration_seconds"])
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"Проект завершён: {project.name}\n"
        f"Время работы: {hours:02}:{minutes:02}:{seconds:02}\n"
        f"Токены: {stats['total_tokens']}\n"
        f"Коммиты: {commits}"
    )
