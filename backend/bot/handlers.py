import asyncio
import json
import logging
import re
import secrets
from contextlib import suppress
from datetime import datetime
from time import time
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardRemove
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import (
    Credentials, GitHubDevicePoll, UserPreferences, create_account, disconnect_github,
    link_telegram, poll_github_device, set_password, start_github_device, update_preferences,
)
from backend.api.projects import (
    AgentCreate, AgentPatch, ClearView, ExistingAgentTest, HubImport, PresetCreate, PresetPatch,
    PresetView, ProjectCreate, ProjectCreateRequest, ProjectPatch, clear_view,
    create_preset, create_project, default_workspace, delete_agent, delete_preset, edit_agent,
    edit_preset, import_presets, list_presets, new_workspace, persist_new_agent,
    project_commits, project_stats, push_project, require_github, retry_agent, rollback_commit,
    store_spec_media, test_agent, update_project,
)
from backend.api.ws import manager
from backend.core.context_manager import build_spec
from backend.core.doc_parser import parse_document
from backend.core.media import MAX_MEDIA_BYTES, MEDIA_TYPES, make_media
from backend.core.model_performance import model_key
from backend.core.orchestrator import history_visible, orchestrator
from backend.core.voice import VoiceTranscriber
from backend.database.db import async_session_factory
from backend.database.models import (
    DEFAULT_PROJECTS_DIR, AgentConfig, AgentMessage, MagicLoginRequest, ModelLeaderboard, Project, ProjectMedia,
    SpecItem, User, utc_now,
)
from backend.settings import ROOT
from worker.adapters import CLI_PROVIDERS

from .formatting import telegram_html
from .keyboards import (
    PROJECT_BUTTONS, PROJECT_ROWS, agent_chat_keyboard, cancel_keyboard, first_project_keyboard, inline_menu,
    main_menu, profile_keyboard, project_keyboard, project_reply_keyboard, projects_keyboard, spec_keyboard,
)
from .middlewares import AccountMiddleware
from .states import AccountSetup, AgentInteraction, HubSetup, ProjectSettings, ProjectSetup

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
account_middleware = AccountMiddleware()
router.message.outer_middleware(account_middleware)
router.callback_query.outer_middleware(account_middleware)

logger = logging.getLogger(__name__)
transcriber = VoiceTranscriber()
LINK_HELP = "Создайте аккаунт или привяжите его кнопками ниже."
DOCUMENT_TYPES = {".pdf", ".docx", ".txt", ".md"}
AUDIO_TYPES = {".ogg", ".mp3", ".wav"}


async def select_project(message, state, account, project_id):
    await state.clear()
    await state.update_data(project_id=project_id)
    if orchestrator.telegram_projects.get(account.id) != project_id:
        orchestrator.telegram_projects[account.id] = project_id
        await message.answer("Управление проектом", reply_markup=project_reply_keyboard())


async def leave_project(message, state, account):
    await state.clear()
    if account and orchestrator.telegram_projects.pop(account.id, None) is not None:
        await message.answer("Главное меню", reply_markup=ReplyKeyboardRemove())


@router.message(F.text.func(lambda value: isinstance(value, str) and value.strip() in PROJECT_BUTTONS))
async def project_navigation(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    label = message.text.strip()
    if label == PROJECT_ROWS[3][1]:
        if account:
            orchestrator.telegram_projects.pop(account.id, None)
        await state.clear()
        await message.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
        await message.answer("Выберите действие.", reply_markup=main_menu(account is not None))
        return
    project_id = (await state.get_data()).get("project_id")
    project = await owned_project(session, account, project_id)
    if project is None:
        if account:
            orchestrator.telegram_projects.pop(account.id, None)
        await state.clear()
        await message.answer("Выберите проект.", reply_markup=ReplyKeyboardRemove())
        await show_projects(message, session, account)
        return
    await state.set_data({"project_id": project.id})
    await state.set_state(None)
    if label == PROJECT_ROWS[0][0]:
        await show_team_chat(message, project, session)
    elif label == PROJECT_ROWS[0][1]:
        await show_private_agents(message, project, session)
    elif label == PROJECT_ROWS[1][0]:
        await show_agents(message, project, session)
    elif label == PROJECT_ROWS[1][1]:
        await show_plan(message, project)
    elif label == PROJECT_ROWS[2][0]:
        action = {"planning": "council", "awaiting_approval": "approve", "running": "pause",
                  "paused": "resume"}.get(project.status)
        if action:
            run_project_action(message, project, action)
        else:
            await message.answer("Действия для текущего статуса", reply_markup=project_keyboard(project))
    elif label == PROJECT_ROWS[2][1]:
        await show_settings(message, project)
    elif label == PROJECT_ROWS[4][0]:
        await show_commits(message, project)
    elif label == PROJECT_ROWS[4][1]:
        await send_push(message, project)
    elif label == PROJECT_ROWS[5][0]:
        await show_clear_menu(message, project)
    elif label == PROJECT_ROWS[3][0]:
        stats = await project_stats(project, session)
        lines = [f"<b>Всего токенов: {stats['total_tokens']}</b>"]
        lines.extend(f"{escape(agent['name'])} · {agent['tokens']} токенов · {agent['contribution_percent']:g}% вклада"
                     for agent in stats["agents"])
        for line in lines:
            await message.answer(line, parse_mode="HTML")


async def prompt_username(message: Message, state: FSMContext):
    await state.set_state(AccountSetup.username)
    await message.answer("Введите желаемый логин (никнейм) без пробелов:", reply_markup=cancel_keyboard())


async def prompt_code(message: Message, state: FSMContext):
    await state.set_state(AccountSetup.link_code)
    await message.answer("Введите 6-значный код из десктопного приложения Loomis:", reply_markup=cancel_keyboard())


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, session: AsyncSession,
                account: User | None, state: FSMContext):
    payload = (command.args or "").strip()
    if payload.startswith("link_"):
        await link(message, CommandObject(args=payload.removeprefix("link_")), session, account, state)
        return
    if await state.get_state() == ProjectSetup.spec_buffer.state:
        project_id = (await state.get_data()).get("project_id")
        await message.answer("Буфер ТЗ открыт. Отправьте текст, изображение, аудио или документ.", reply_markup=spec_keyboard(project_id))
    else:
        await leave_project(message, state, account)
        text = (f"Здравствуйте, {account.username}! Выберите действие." if account else
                "Добро пожаловать в Loomis. Создайте аккаунт или привяжите существующий.")
        await message.answer(text, reply_markup=main_menu(account is not None))


@router.message(Command("quick_register"))
async def quick_register(message: Message, command: CommandObject, session: AsyncSession,
                         account: User | None, state: FSMContext):
    if account:
        await message.answer("Telegram уже привязан к аккаунту.", reply_markup=main_menu(True))
    elif (command.args or "").strip():
        await register_account(message, (command.args or "").strip(), session, state)
    else:
        await prompt_username(message, state)


async def register_account(message: Message, username: str, session: AsyncSession, state: FSMContext):
    try:
        credentials = Credentials(username=username, password=secrets.token_urlsafe(24))
        user = await create_account(session, credentials.username, credentials.password, message.from_user.id)
    except ValidationError:
        await message.answer("Логин должен быть корректным и не содержать пробелов. Попробуйте ещё раз:")
        await state.set_state(AccountSetup.username)
    except HTTPException as error:
        await message.answer(str(error.detail))
    else:
        await state.clear()
        await message.answer(f"Аккаунт создан.\nЛогин: {user.username}\nПароль: {credentials.password}\nСохраните пароль для входа в Loomis.",
                             reply_markup=first_project_keyboard())


@router.message(AccountSetup.username)
async def username_input(message: Message, state: FSMContext, session: AsyncSession):
    await register_account(message, (message.text or "").strip(), session, state)


@router.message(Command("link"))
async def link(message: Message, command: CommandObject, session: AsyncSession,
               account: User | None, state: FSMContext):
    if account:
        await message.answer("Telegram уже привязан.", reply_markup=main_menu(True))
        return
    code = (command.args or "").strip()
    if not code:
        await prompt_code(message, state)
    else:
        await link_account(message, code, session, state)


async def save_telegram_avatar(message: Message, session: AsyncSession) -> None:
    telegram_id = message.from_user.id
    directory = ROOT / "temp_uploads" / "avatars"
    filename = f"tg_{telegram_id}.jpg"
    temporary = None
    try:
        photos = await message.bot.get_user_profile_photos(telegram_id, limit=1)
        if not photos.photos:
            return
        directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=directory, delete=False) as file:
            temporary = Path(file.name)
            await message.bot.download(photos.photos[0][-1], destination=file)
        temporary.replace(directory / filename)
    except (TelegramAPIError, OSError, TimeoutError):
        logger.warning("Не удалось загрузить аватар Telegram для пользователя %s.", telegram_id)
        return
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    await session.execute(update(User).where(User.telegram_id == telegram_id)
                          .values(telegram_avatar_url=f"/static/avatars/{filename}"))
    await session.commit()


async def link_account(message: Message, code: str, session: AsyncSession, state: FSMContext):
    if not re.fullmatch(r"[0-9]{6}", code):
        await message.answer("Код должен состоять из 6 цифр. Введите код ещё раз:")
        await state.set_state(AccountSetup.link_code)
        return
    sender = message.from_user
    username = f"@{sender.username}" if sender.username else sender.first_name
    if await link_telegram(session, code, sender.id, username):
        await save_telegram_avatar(message, session)
        await state.clear()
        await message.answer("Telegram привязан к аккаунту Loomis.", reply_markup=main_menu(True))
    else:
        await message.answer("Код недействителен, истёк или уже использован. Введите другой код:")
        await state.set_state(AccountSetup.link_code)


@router.message(AccountSetup.link_code)
async def code_input(message: Message, state: FSMContext, session: AsyncSession):
    await link_account(message, (message.text or "").strip(), session, state)


@router.message(Command("new_project"))
@router.message(Command("project"))
async def new_project(message: Message, state: FSMContext, account: User | None):
    if account is None:
        await message.answer(LINK_HELP, reply_markup=main_menu(False))
    elif await state.get_state() == ProjectSetup.spec_buffer.state:
        project_id = (await state.get_data()).get("project_id")
        await message.answer("Сначала завершите текущий буфер ТЗ.", reply_markup=spec_keyboard(project_id))
    else:
        await state.set_state(ProjectSetup.name)
        await message.answer("Введите название проекта:", reply_markup=cancel_keyboard())


@router.message(Command("projects"))
async def projects(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    await leave_project(message, state, account)
    await show_projects(message, session, account)


async def show_projects(message, session: AsyncSession, account: User | None):
    if account is None:
        await message.answer(LINK_HELP, reply_markup=main_menu(False))
        return
    items = (await session.scalars(select(Project).where(Project.owner_id == account.id).order_by(Project.created_at.desc()))).all()
    if not items:
        await message.answer("У вас пока нет проектов.", reply_markup=main_menu(True))
    else:
        await message.answer("Ваши проекты:", reply_markup=projects_keyboard(items))


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    project = await owned_project(session, account, (await state.get_data()).get("project_id"))
    if project is not None:
        await select_project(message, state, account, project.id)
        await message.answer("Текущее действие отменено.", reply_markup=project_keyboard(project))
    else:
        await leave_project(message, state, account)
        await message.answer("Текущее действие отменено.", reply_markup=main_menu(account is not None))


@router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    await callback.answer("Действие отменено")
    await cancel(callback.message, state, session, account)


@router.callback_query(F.data == "quick_register")
async def quick_register_callback(callback: CallbackQuery, state: FSMContext, account: User | None):
    await callback.answer()
    if account:
        await callback.message.answer("Telegram уже привязан.", reply_markup=main_menu(True))
    else:
        await prompt_username(callback.message, state)


@router.callback_query(F.data == "link_account")
async def link_callback(callback: CallbackQuery, state: FSMContext, account: User | None):
    await callback.answer()
    if account:
        await callback.message.answer("Telegram уже привязан.", reply_markup=main_menu(True))
    else:
        await prompt_code(callback.message, state)


@router.callback_query(F.data == "new_project")
async def new_project_callback(callback: CallbackQuery, state: FSMContext, account: User | None):
    await callback.answer()
    if account is None:
        await callback.message.answer(LINK_HELP, reply_markup=main_menu(False))
    else:
        await state.set_state(ProjectSetup.name)
        await callback.message.answer("Введите название проекта:", reply_markup=cancel_keyboard())


@router.callback_query(F.data == "projects")
async def projects_callback(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    await callback.answer()
    await leave_project(callback.message, state, account)
    await show_projects(callback.message, session, account)


async def show_account_settings(message, account):
    github = f"@{account.github_username}" if account.github_username else "не привязан"
    logs = "включены" if account.tg_logs_enabled else "выключены"
    folder = account.default_projects_dir or DEFAULT_PROJECTS_DIR
    await message.answer(
        f"<b>Настройки аккаунта</b> · {escape(account.username or account.login_name or 'Loomis')}\n"
        f"GitHub: {escape(github)}\nОбщая папка для проектов: <code>{escape(folder)}</code>\n"
        f"Рабочие логи в Telegram: {logs}", parse_mode="HTML",
        reply_markup=profile_keyboard(bool(account.github_username)),
    )


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery, account: User | None):
    if account is None:
        await callback.answer("Привяжите Telegram к аккаунту Loomis.", show_alert=True)
        return
    await callback.answer()
    await show_account_settings(callback.message, account)


github_flows = set()


@router.callback_query(F.data.in_({"profile:github", "profile:unlink", "profile:unlink_yes",
                                 "profile:folder", "profile:logs"}))
async def account_setting(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    if account is None:
        await callback.answer("Привяжите Telegram к аккаунту Loomis.", show_alert=True)
        return
    await callback.answer()
    message = callback.message
    action = callback.data.split(":")[1]
    if action == "folder":
        await state.set_state(AccountSetup.projects_dir)
        await message.answer(WORKSPACE_PROMPT, reply_markup=cancel_keyboard())
    elif action == "logs":
        await session.execute(update(User).where(User.id == account.id).values(tg_logs_enabled=~User.tg_logs_enabled))
        await session.commit()
        await session.refresh(account)
        await show_account_settings(message, account)
    elif action == "unlink":
        await message.answer("Отвязать GitHub?", reply_markup=inline_menu([
            [("Отвязать GitHub", "profile:unlink_yes"), ("Отмена", "profile")],
        ]))
    elif action == "unlink_yes":
        if account.id in github_flows:
            await message.answer("Дождитесь завершения входа через GitHub.")
            return
        await disconnect_github(session, account)
        await show_account_settings(message, account)
    elif account.github_username:
        await show_account_settings(message, account)
    elif account.id in github_flows:
        await message.answer("Код уже выдан. Используйте предыдущее сообщение.")
    else:
        owner_id = account.id
        github_flows.add(owner_id)
        try:
            flow = await start_github_device(account)
            await message.answer(
                f'Код GitHub: <code>{escape(flow["user_code"])}</code>\n'
                'Введите код на <a href="https://github.com/login/device">github.com/login/device</a>. '
                'Бот сообщит о результате.', parse_mode="HTML",
            )
        except HTTPException as exc:
            github_flows.discard(owner_id)
            await answer_error(message, exc)
        except BaseException:
            github_flows.discard(owner_id)
            raise
        else:
            orchestrator.background_reply(wait_for_github(message, owner_id, callback.from_user.id, flow))


async def wait_for_github(message, owner_id, telegram_id, flow):
    interval = flow["interval"]
    request = GitHubDevicePoll.model_validate(flow)
    try:
        while (remaining := request.expires_at - time()) > 0:
            await asyncio.sleep(min(interval, remaining))
            async with async_session_factory() as session:
                account = await session.get(User, owner_id)
                if account is None or account.telegram_id != telegram_id:
                    return
                if account.github_username:
                    await show_account_settings(message, account)
                    return
                result = await poll_github_device(request, session, account)
                status = result["status"]
                if status == "authorized":
                    await show_account_settings(message, account)
                    return
            if status == "access_denied":
                await message.answer("Вход через GitHub отклонён.")
                return
            if status == "expired_token":
                break
            if status == "slow_down":
                interval = max(interval + 5, result["interval"] or 0)
        await message.answer("Код GitHub истёк. Начните вход заново.")
    except HTTPException as exc:
        await answer_error(message, exc)
    finally:
        github_flows.discard(owner_id)


@router.message(AccountSetup.projects_dir)
async def save_projects_dir(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    if account is None:
        await state.set_state(None)
        return
    path = (message.text or "").strip()
    if not Path(path).is_absolute():
        await message.answer(WORKSPACE_PROMPT)
        return
    try:
        await update_preferences(UserPreferences(default_projects_dir=path), session, account)
    except ValidationError:
        await message.answer(WORKSPACE_PROMPT)
        return
    except HTTPException as exc:
        await answer_error(message, exc)
        return
    await state.set_state(None)
    await show_account_settings(message, account)


@router.callback_query(F.data == "profile:new_password")
async def new_password_callback(callback: CallbackQuery, session: AsyncSession, account: User | None):
    if account is None:
        await callback.answer("Привяжите Telegram к аккаунту Loomis.", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer("Откройте профиль заново через /start.", show_alert=True)
        return
    password = secrets.token_urlsafe(18)
    await callback.answer()
    await set_password(session, account, password)
    await callback.message.answer(
        f"Новый пароль для {account.username}: {password}\nСохраните его. Старый пароль больше не действует."
    )


@router.callback_query(F.data.startswith("magic_approve:") | F.data.startswith("magic_reject:"))
async def resolve_magic_login(callback: CallbackQuery, session: AsyncSession, account: User | None):
    if (account is None or account.telegram_id != callback.from_user.id
            or not isinstance(callback.message, Message)):
        await callback.answer("Запрос входа недоступен.", show_alert=True)
        return

    action, request_id = callback.data.split(":", 1)
    approved = action == "magic_approve"
    now = utc_now()
    resolved_id = await session.scalar(
        update(MagicLoginRequest)
        .where(MagicLoginRequest.request_id == request_id, MagicLoginRequest.user_id == account.id,
               MagicLoginRequest.status == "pending", MagicLoginRequest.expires_at > now)
        .values(status="approved" if approved else "rejected", resolved_at=now)
        .returning(MagicLoginRequest.id)
    )
    if resolved_id is None:
        await session.rollback()
        await callback.answer("Запрос входа истёк или недоступен.", show_alert=True)
        return

    await session.commit()
    await callback.answer()
    text = "✅ Вход в Loomis Desktop подтверждён." if approved else "❌ Вход отклонён."
    await callback.message.edit_text(text, reply_markup=None)


@router.callback_query(F.data.startswith("project:"))
async def project_card(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project_id = int(callback.data.split(":", 1)[1])
    project = await owned_project(session, account, project_id)
    await callback.answer()
    if project is None:
        await callback.message.answer("Проект не найден.")
        return
    await select_project(callback.message, state, account, project.id)
    count = await session.scalar(select(func.count(SpecItem.id)).where(SpecItem.project_id == project.id))
    created = project.created_at.strftime("%Y-%m-%d %H:%M") if project.created_at else "—"
    updated = project.updated_at.strftime("%Y-%m-%d %H:%M") if getattr(project, "updated_at", None) else "—"
    await callback.message.answer(
        f"{project.name}\nСтатус: {project.status}\nСоздан: {created}\nОбновлён: {updated}\n"
        f"Записей ТЗ: {count}\n{workspace_details(project)}", reply_markup=project_keyboard(project),
    )


def workspace_details(project: Project) -> str:
    path = str(Path(project.workspace_path).absolute()) if project.workspace_path else default_workspace(project)
    return f"Папка проекта на ПК: {path}" + (f"\nGit-репозиторий: {project.git_repo_url}" if project.git_repo_url else "")


async def owned_project(session, account: User | None, project_id: int):
    if account is None:
        return None
    return await session.scalar(select(Project).where(Project.id == project_id, Project.owner_id == account.id))


@router.callback_query(F.data.startswith("open_buffer:"))
async def open_buffer(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project_id = int(callback.data.split(":", 1)[1])
    project = await buffer_project(session, account, project_id)
    await callback.answer()
    if project is None:
        await callback.message.answer("Буфер доступен только для проекта в статусе черновика.")
    else:
        await select_project(callback.message, state, account, project.id)
        await state.set_state(ProjectSetup.spec_buffer)
        count = await session.scalar(select(func.count(SpecItem.id)).where(SpecItem.project_id == project.id))
        await callback.message.answer(f"Буфер ТЗ — {count} записей. Отправьте текст, изображение, аудио или документ.", reply_markup=spec_keyboard(project.id))


@router.callback_query(F.data.startswith("view_spec:"))
async def view_spec(callback: CallbackQuery, session: AsyncSession, account: User | None):
    project_id = int(callback.data.split(":", 1)[1])
    project = await owned_project(session, account, project_id)
    await callback.answer()
    if project is None:
        await callback.message.answer("Проект не найден.")
        return
    path = Path(project.workspace_path or default_workspace(project)) / "SPEC.md"
    if not path.is_file():
        await callback.message.answer("SPEC.md пока не собран.")
    else:
        content = path.read_text(encoding="utf-8")
        for part in telegram_html(content.strip() or "SPEC.md пуст."):
            await callback.message.answer(part, parse_mode="HTML")


@router.callback_query(F.data.startswith("delete_project:"))
async def delete_project_callback(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project_id = int(callback.data.split(":", 1)[1])
    project = await owned_project(session, account, project_id)
    await callback.answer()
    if project is None:
        await callback.message.answer("Проект не найден.")
        return
    await orchestrator.stop(project.id)
    await session.delete(project)
    await session.commit()
    await manager.close_project(project.id)
    if orchestrator.telegram_projects.get(account.id) == project.id:
        await leave_project(callback.message, state, account)
    await callback.message.answer("Проект удалён.")
    await show_projects(callback.message, session, account)


@router.message(ProjectSetup.name)
async def project_name(message: Message, state: FSMContext, account: User | None):
    if account is None:
        await message.answer(LINK_HELP, reply_markup=main_menu(False))
        return
    try:
        data = ProjectCreate(name=message.text or "")
    except ValidationError:
        await message.answer("Введите название проекта текстом, до 255 символов.")
        return
    await state.update_data(project_name=data.name, default_projects_dir=account.default_projects_dir)
    await prompt_workspace(message, state, account)


async def prompt_workspace(message, state, account):
    name = (await state.get_data())["project_name"]
    path = new_workspace(name, account.default_projects_dir)
    await state.set_state(ProjectSetup.workspace)
    await message.answer(
        f"Папка по умолчанию: <code>{escape(path)}</code>", parse_mode="HTML",
        reply_markup=inline_menu([
            [("Использовать эту папку", "new_workspace:default")],
            [("Ввести свой путь на ПК", "new_workspace:custom")],
        ]),
    )


def git_options(prefix):
    return [[("✨ Создать новый репозиторий на GitHub автоматически", f"{prefix}:create")]]


def repo_visibility_keyboard(prefix):
    return inline_menu([[("Приватный", f"{prefix}:private"), ("Публичный", f"{prefix}:public")]])


def github_required_keyboard():
    return inline_menu([[("⚙️ Настройки аккаунта", "profile")]])


async def prompt_project_git(message, state, account, path):
    await state.update_data(workspace_path=path, default_projects_dir=account.default_projects_dir)
    if not (account.github_token and account.github_username):
        await state.update_data(git_repo_url=None, github_visibility=None)
        await message.answer(
            "Чтобы добавить GitHub-репозиторий, привяжите GitHub в настройках аккаунта.",
            reply_markup=github_required_keyboard(),
        )
        await new_project_options(message, state)
        return
    await state.set_state(ProjectSetup.git)
    await message.answer(
        "Введите адрес существующего репозитория, например https://github.com/user/repo.git.",
        reply_markup=inline_menu(git_options("new_git") + [[("Без репозитория", "new_git:skip"), ("Назад к настройкам", "new_git:back")]]),
    )


WORKSPACE_PROMPT = r"Введите полный путь к папке на ПК, например D:\Projects\NewApp."


@router.callback_query(ProjectSetup.workspace, F.data.in_({"new_workspace:default", "new_workspace:custom"}))
async def choose_workspace(callback: CallbackQuery, state: FSMContext, account: User | None):
    await callback.answer()
    if account is None:
        return
    if callback.data == "new_workspace:default":
        await prompt_project_git(callback.message, state, account, None)
    else:
        await callback.message.answer(WORKSPACE_PROMPT, reply_markup=cancel_keyboard())


@router.message(ProjectSetup.workspace)
async def enter_workspace(message: Message, state: FSMContext, account: User | None):
    if account is None:
        return
    path = (message.text or "").strip()
    if len(path) > 1024 or not Path(path).is_absolute():
        await message.answer(WORKSPACE_PROMPT)
        return
    await prompt_project_git(message, state, account, path)


@router.callback_query(ProjectSetup.git, F.data.startswith("new_git:"))
async def choose_project_git(callback: CallbackQuery, state: FSMContext, account: User | None):
    await callback.answer()
    if account is None:
        return
    action = callback.data.split(":")[1]
    if action == "create" and account.github_username:
        await callback.message.answer("Выберите видимость репозитория.",
                                      reply_markup=repo_visibility_keyboard("new_git"))
    elif action == "back":
        await new_project_options(callback.message, state)
    elif action in {"skip", "private", "public"}:
        await state.update_data(git_repo_url=None, github_visibility=None if action == "skip" else action)
        await new_project_options(callback.message, state)


@router.message(ProjectSetup.git)
async def enter_project_git(message: Message, state: FSMContext):
    try:
        data = ProjectCreate(name="Git", git_repo_url=message.text or "")
    except ValidationError:
        await message.answer("Введите адрес Git-репозитория длиной до 500 символов.")
        return
    await state.update_data(git_repo_url=data.git_repo_url, github_visibility=None)
    await new_project_options(message, state)


def setting_rows(settings, prefix):
    return [
        [(f"✓ {label}" if getattr(settings, field) == key else label, f"{prefix}{field}:{key}")
         for key, label in options.items()]
        for field, options in SETTING_OPTIONS.items()
    ]


async def new_project_data(state):
    fields = await state.get_data()
    return ProjectCreateRequest(name=fields["project_name"], **{
        field: value for field, value in fields.items() if field in ProjectCreateRequest.model_fields
    })


async def new_project_options(message, state):
    data = await new_project_data(state)
    await state.set_state(ProjectSetup.options)
    rows = setting_rows(data, "new_option:")
    rows.extend([
        [(f"Дедлайн · {data.deadline_minutes} мин.", "new_option:deadline")],
        [("Изменить папку", "new_option:workspace"), ("Настроить GitHub", "new_option:git")],
        [("Создать проект", "new_option:save"), ("Отмена", "cancel")],
    ])
    git = data.git_repo_url or {"private": "создать приватный", "public": "создать публичный"}.get(
        data.github_visibility, "без репозитория",
    )
    path = data.workspace_path or new_workspace(data.name, (await state.get_data()).get("default_projects_dir"))
    await message.answer(
        f"Настройки нового проекта · <b>{escape(data.name)}</b>\n"
        f"Папка на ПК: <code>{escape(path)}</code>\nGitHub: {escape(git)}",
        parse_mode="HTML", reply_markup=inline_menu(rows),
    )


@router.callback_query(ProjectSetup.options, F.data.startswith("new_option:"))
async def change_new_project(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    await callback.answer()
    if account is None:
        return
    _, field, *values = callback.data.split(":")
    message = callback.message
    if field == "deadline":
        await state.set_state(ProjectSetup.deadline)
        await message.answer(DEADLINE_PROMPT, reply_markup=cancel_keyboard())
    elif field == "workspace":
        await prompt_workspace(message, state, account)
    elif field == "git":
        await prompt_project_git(message, state, account, (await state.get_data()).get("workspace_path"))
    elif field == "save":
        await finish_new_project(message, state, session, account)
    elif len(values) == 1 and values[0] in SETTING_OPTIONS.get(field, {}):
        await state.update_data({field: values[0]})
        await new_project_options(message, state)


DEADLINE_PROMPT = "Введите дедлайн запроса целым числом от 1 до 1440 минут."


@router.message(ProjectSetup.deadline)
async def new_project_deadline(message: Message, state: FSMContext):
    try:
        data = ProjectPatch(deadline_minutes=int(message.text or ""))
    except ValueError:
        await message.answer(DEADLINE_PROMPT)
        return
    await state.update_data(deadline_minutes=data.deadline_minutes)
    await new_project_options(message, state)


async def finish_new_project(message, state, session, account):
    try:
        data = await new_project_data(state)
        if data.git_repo_url or data.github_visibility:
            require_github(account)
        project = await create_project(session, account.id, data)
    except HTTPException as exc:
        await answer_error(message, exc)
        await new_project_options(message, state)
        return
    await select_project(message, state, account, project.id)
    await state.set_state(ProjectSetup.spec_buffer)
    await message.answer(f"{escape(workspace_details(project))}\n\n"
                         "Буфер ТЗ — 0 записей. Отправьте текст, изображение, аудио или документ.",
                         parse_mode="HTML", reply_markup=spec_keyboard(project.id))


async def buffer_project(session: AsyncSession, account: User | None, project_id: int | None) -> Project | None:
    if account is None or project_id is None:
        return None
    return await session.scalar(select(Project).where(Project.id == project_id, Project.owner_id == account.id,
                                                        Project.status == "draft"))


async def read_attachment(message: Message, bot: Bot) -> tuple[str, str]:
    if message.voice:
        attachment, title, suffix = message.voice, "Голосовое сообщение", ".ogg"
    elif message.audio:
        attachment = message.audio
        title = attachment.file_name or attachment.title or "Аудиозапись"
        suffix = Path(attachment.file_name or "audio.mp3").suffix.lower()
    else:
        attachment = message.document
        title = attachment.file_name or "Документ"
        suffix = Path(title).suffix.lower()
    if suffix not in DOCUMENT_TYPES | AUDIO_TYPES:
        raise ValueError("Поддерживаются .pdf, .docx, .txt, .md, .ogg, .mp3 и .wav.")
    upload_dir = ROOT / "temp_uploads"
    upload_dir.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=upload_dir) as directory:
        path = Path(directory) / f"upload{suffix}"
        await bot.download(attachment, destination=path)
        if suffix in AUDIO_TYPES:
            return title, await transcriber.transcribe(path)
        return title, await parse_document(path)


async def read_media(message, bot, project_id):
    if message.photo:
        attachment, name = message.photo[-1], "photo.jpg"
    elif message.voice:
        attachment, name = message.voice, "voice.ogg"
    else:
        attachment = message.audio or message.document
        if attachment is None:
            return None
        name = attachment.file_name or "audio.mp3"
        if Path(name).suffix.lower() not in MEDIA_TYPES and message.document:
            return None
    if attachment.file_size and attachment.file_size > MAX_MEDIA_BYTES:
        raise ValueError("Размер медиафайла должен быть до 5 МБ.")
    stream = await bot.download(attachment)
    return make_media(project_id, name, stream.read(MAX_MEDIA_BYTES + 1))


@router.message(ProjectSetup.spec_buffer, F.text | F.voice | F.audio | F.document | F.photo)
async def spec_item(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, account: User | None):
    project = await buffer_project(session, account, (await state.get_data()).get("project_id"))
    if project is None:
        await leave_project(message, state, account)
        await message.answer("Буфер ТЗ недоступен. Создайте новый проект кнопкой меню.", reply_markup=main_menu(account is not None))
        return
    if message.text is not None:
        title, content = "Текст", message.text.strip()
    else:
        try:
            media = await read_media(message, bot, project.id)
            if media is not None:
                await store_spec_media(session, project, media)
                await message.answer("Медиафайл добавлен в буфер ТЗ.", reply_markup=spec_keyboard(project.id))
                return
            title, content = await read_attachment(message, bot)
        except ValueError as error:
            await message.answer(str(error))
            return
        except Exception:
            logger.exception("Could not read Telegram attachment for project %s", project.id)
            await message.answer("Не удалось прочитать файл. Попробуйте отправить его ещё раз.")
            return
    if not content:
        await message.answer("В сообщении или файле не найден текст.")
        return
    count, last_position = (await session.execute(select(func.count(SpecItem.id), func.max(SpecItem.position)).where(SpecItem.project_id == project.id))).one()
    session.add(SpecItem(project_id=project.id, title=title[:500], content=content, position=(last_position or 0) + 1))
    await session.commit()
    await manager.publish_log(project.id, "Добавлена запись в буфер ТЗ.")
    await message.answer(f"Буфер ТЗ — записей: {count + 1}.", reply_markup=spec_keyboard(project.id))


@router.callback_query(F.data.startswith("build_spec:"))
async def finish_spec(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project_id = (await state.get_data()).get("project_id")
    if await state.get_state() != ProjectSetup.spec_buffer.state or callback.data != f"build_spec:{project_id}":
        await callback.answer("Эта кнопка больше не активна.", show_alert=True)
        return
    project = await buffer_project(session, account, project_id)
    if project is None:
        await state.clear()
        await callback.answer("Буфер ТЗ недоступен.", show_alert=True)
        return
    await callback.answer()
    try:
        if not project.workspace_path:
            project.workspace_path = default_workspace(project)
        await build_spec(session, project)
        project.status = "planning"
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Could not assemble SPEC.md for project %s", project_id)
        await callback.message.answer("Не удалось собрать SPEC.md. Можно повторить сборку.")
        return
    await state.set_state(None)
    await manager.publish_status(project.id, project.status)
    await manager.publish_log(project.id, "Собран SPEC.md.")
    await callback.message.answer("SPEC.md собран. Проект переведён в планирование.", reply_markup=project_keyboard(project))


@router.callback_query(F.data.startswith("cancel_project:"))
async def cancel_project(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project_id = int(callback.data.split(":", 1)[1])
    project = await buffer_project(session, account, project_id)
    await callback.answer()
    if project is None:
        await callback.message.answer("Черновик проекта не найден.")
        return
    await session.delete(project)
    await session.commit()
    if orchestrator.telegram_projects.get(account.id) == project.id:
        await leave_project(callback.message, state, account)
    await callback.message.answer("Черновик проекта и его буфер ТЗ удалены.", reply_markup=main_menu(True))


@router.message(ProjectSetup.spec_buffer)
async def unsupported_spec_item(message: Message):
    await message.answer("Отправьте текст, голосовое сообщение, аудио или документ.")


@router.callback_query(F.data.startswith("project_action:"))
async def project_action(callback: CallbackQuery, session: AsyncSession, account: User | None):
    _, project_id, action = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None:
        return
    run_project_action(callback.message, project, action)


def run_project_action(message, project, action, **options):
    async def reply():
        try:
            project.status = await orchestrator.action(project.id, action, **options)
        except (OSError, ValueError, RuntimeError) as exc:
            await answer_formatted(message, str(exc))
        else:
            text = "Последний шаг откачен." if action == "rollback" else f"Статус проекта — {project.status}."
            await message.answer(text, reply_markup=project_keyboard(project))

    orchestrator.background_reply(reply())


@router.callback_query(F.data.startswith("agent_add:"))
async def configure_agent(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, provider = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None or project.status == "draft" or provider not in {"api", "cli"}:
        return
    await select_project(callback.message, state, account, project.id)
    await state.set_data({"project_id": project.id, "provider": provider})
    await state.set_state(AgentInteraction.role)
    await callback.message.answer("Выберите специализацию модели.", reply_markup=role_keyboard(
        "agent_role", allow_images=provider == "api",
    ))


ROLE_LABELS = {
    "conversational": "Универсальная LLM (Код / Чат)",
    "image_gen": "Генератор изображений",
}


def role_keyboard(prefix, allow_images=True):
    return inline_menu([
        [(label, f"{prefix}:{role}")]
        for role, label in ROLE_LABELS.items() if allow_images or role != "image_gen"
    ] + [[("Отмена", "cancel")]])


@router.callback_query(AgentInteraction.role, F.data.startswith("agent_role:"))
async def choose_agent_role(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    role = callback.data.split(":")[1]
    provider = (await state.get_data())["provider"]
    if role not in ROLE_LABELS or (role == "image_gen" and provider != "api"):
        return
    await state.update_data(agent_role_type=role)
    await state.set_state(AgentInteraction.configure)
    examples = {
        "cli": '{"name":"Codex","model":"codex","settings":{"command":"codex exec --json --sandbox workspace-write -","question_command":"codex exec --json --skip-git-repo-check --sandbox read-only -"}}',
        "api": '{"name":"Ollama","model":"qwen3","settings":{"base_url":"http://localhost:11434/v1"}}',
    }
    example = examples[provider] if role == "conversational" else (
        '{"name":"Изображения","model":"gpt-image-1","settings":'
        '{"base_url":"https://api.openai.com/v1","api_key":"..."}}'
    )
    await callback.message.answer(f"Отправьте конфигурацию агента в JSON.\n<pre>{escape(example)}</pre>",
                                  parse_mode="HTML", reply_markup=cancel_keyboard())


@router.message(AgentInteraction.configure, F.text)
async def save_agent_config(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    data = await state.get_data()
    project = await owned_project(session, account, data["project_id"])
    if project is None:
        await state.clear()
        return
    try:
        values = json.loads(message.text)
        if not isinstance(values, dict):
            raise ValueError("Ожидается объект JSON.")
        values["provider"] = data["provider"]
        values["agent_role_type"] = data["agent_role_type"]
        agent = AgentCreate.model_validate(values)
        await persist_new_agent(AgentConfig(project_id=project.id, **agent.model_dump()), project, session)
    except HTTPException as exc:
        await message.answer(str(exc.detail)[:3900])
        return
    except ValueError:
        await message.answer("Проверьте JSON, модель и уникальность имени агента.")
        return
    await state.set_data({"project_id": project.id})
    await state.set_state(None)
    await message.answer("Агент добавлен.", reply_markup=project_keyboard(project))


async def enabled_agents(session, project_id):
    return list(await session.scalars(select(AgentConfig).where(
        AgentConfig.project_id == project_id, AgentConfig.enabled.is_(True)
    ).order_by(AgentConfig.id)))


@router.callback_query(F.data.startswith("agent_list:"))
async def choose_chat_agent(callback: CallbackQuery, session: AsyncSession, account: User | None):
    project = await owned_project(session, account, int(callback.data.split(":")[1]))
    await callback.answer()
    if project is None:
        return
    await show_private_agents(callback.message, project, session)


async def show_private_agents(message, project, session):
    agents = await enabled_agents(session, project.id)
    text = "Выберите агента." if agents else "Добавьте агента в карточке проекта."
    await message.answer(text, reply_markup=agent_chat_keyboard(project.id, agents))


@router.callback_query(F.data.startswith("agent_chat:"))
async def open_agent_chat(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, agent_id = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None or not any(item.id == int(agent_id) for item in await enabled_agents(session, project.id)):
        return
    await select_project(callback.message, state, account, project.id)
    await state.set_data({"project_id": project.id, "agent_id": int(agent_id)})
    await state.set_state(AgentInteraction.question)
    await show_chat_history(callback.message, project, session, "direct", int(agent_id))
    await callback.message.answer("Отправьте вопрос, изображение, аудио или документ.", reply_markup=inline_menu([
        [("Очистить экран чата", f"clear_chat:{project.id}:direct:{agent_id}")],
        [("Отмена", "cancel")],
    ]))


@router.callback_query(F.data.startswith("team_chat:"))
async def open_team_chat(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project = await owned_project(session, account, int(callback.data.split(":")[1]))
    await callback.answer()
    if project is None:
        return
    await select_project(callback.message, state, account, project.id)
    await show_team_chat(callback.message, project, session)


@router.message(AgentInteraction.question, F.text | F.voice | F.audio | F.photo | F.document)
async def agent_chat_question(message: Message, bot: Bot, state: FSMContext,
                              session: AsyncSession, account: User | None):
    data = await state.get_data()
    project = await owned_project(session, account, data["project_id"])
    if project is None:
        await state.clear()
        return
    try:
        question = (message.text or message.caption or "").strip()
        media = await read_media(message, bot, project.id)
        if message.document and media is None:
            if message.document.file_size and message.document.file_size > MAX_MEDIA_BYTES:
                raise ValueError("Размер документа должен быть до 5 МБ.")
            title, content = await read_attachment(message, bot)
            question = f"{question or 'Проанализируй вложение.'}\n\n{title}\n{content}"
        if media is not None:
            question = f"{question or 'Проанализируй вложение.'}\n\nФайл: {media.filename}"
        if not question:
            await message.answer("В сообщении нет вопроса.")
            return
        if len(question) > 32000:
            raise ValueError("Текст вместе с содержимым документа должен содержать до 32000 символов.")
        if data.get("plan_feedback") and media is not None:
            raise ValueError("Отправьте правки к плану текстом или документом.")
    except (OSError, ValueError) as exc:
        await answer_formatted(message, str(exc))
        return
    except Exception:
        logger.exception("Could not download Telegram media for project %s", project.id)
        await message.answer("Не удалось загрузить медиафайл.")
        return

    targets = [data["target_id"]] if data.get("target_id") else None
    if data.get("plan_feedback"):
        await state.set_data({"project_id": project.id})
        await state.set_state(None)
        run_project_action(message, project, "regenerate_plan", feedback=question, target_agent_ids=targets)
        return

    async def reply():
        try:
            if "agent_id" in data:
                response = await orchestrator.question(project.id, data["agent_id"], question, media=media)
                await answer_formatted(message, response["message"] or response["status"])
                if response.get("media_id") is not None:
                    async with async_session_factory() as media_session:
                        await send_generated_image(message, media_session, project.id, response["media_id"])
            else:
                await orchestrator.chat(project.id, question, targets, media=media)
        except (OSError, ValueError) as exc:
            await answer_formatted(message, str(exc))

    orchestrator.background_reply(reply())


@router.callback_query(F.data.startswith("permit:"))
async def permit_command(callback: CallbackQuery, session: AsyncSession, account: User | None):
    _, key, decision = callback.data.split(":")
    request = orchestrator.approvals.get(key)
    if request is None or await owned_project(session, account, request[0]) is None:
        await callback.answer("Запрос закрыт.", show_alert=True)
        return
    try:
        orchestrator.decide(request[0], key, decision == "1")
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
    else:
        await callback.answer("Решение принято.")


async def answer_formatted(message, content):
    for part in telegram_html(content):
        await message.answer(part, parse_mode="HTML")


async def show_chat_history(message, project, session, channel, agent_id=None):
    query = select(AgentMessage).where(
        AgentMessage.project_id == project.id, AgentMessage.channel == channel,
        AgentMessage.is_ui_cleared.is_(False), history_visible(project, AgentMessage),
    )
    if agent_id is not None:
        agent = await session.get(AgentConfig, agent_id)
        query = query.where((AgentMessage.agent_id == agent_id) | (
            AgentMessage.agent_id.is_(None) & (AgentMessage.target_name == f"@{agent.name}")
        ))
    rows = list(await session.scalars(query.order_by(AgentMessage.id.desc()).limit(10)))
    if not rows:
        await message.answer("В чате пока нет сообщений.")
    for row in reversed(rows):
        for part in telegram_html(row.content or "—"):
            header = escape(f"{row.sender_name} → {row.target_name}"[:200])
            await message.answer(f"<b>{header}</b>\n{part}", parse_mode="HTML")
        if row.media_id is not None:
            await send_generated_image(message, session, project.id, row.media_id)


async def show_team_chat(message, project, session):
    await show_chat_history(message, project, session, "team_chat")
    await message.answer("История общего чата", reply_markup=inline_menu([
        [("Очистить экран чата", f"clear_chat:{project.id}:team_chat")],
    ]))
    await choose_team_target(message, project, session, "chat")


async def choose_team_target(message, project, session, mode):
    agents = await enabled_agents(session, project.id)
    if not agents:
        await message.answer("Добавьте и включите хотя бы одного агента.")
        return
    prefix = f"team_target:{project.id}:{mode}:"
    rows = [[("Всем", prefix + "all")]]
    rows.extend([(agent.name, prefix + str(agent.id))] for agent in agents)
    await message.answer("Выберите адресата.", reply_markup=inline_menu(rows))


@router.callback_query(F.data.startswith("team_target:"))
async def team_target(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, mode, target = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    agents = await enabled_agents(session, project.id) if project is not None else []
    if not agents or (target != "all" and not any(str(agent.id) == target for agent in agents)):
        await callback.answer("Адресат недоступен.", show_alert=True)
        return
    await callback.answer()
    await select_project(callback.message, state, account, project.id)
    await state.update_data(target_id=None if target == "all" else int(target), plan_feedback=mode == "plan")
    await state.set_state(AgentInteraction.question)
    prompt = "Отправьте правки к плану." if mode == "plan" else "Отправьте сообщение или вложение."
    await callback.message.answer(prompt, reply_markup=cancel_keyboard())


async def show_plan(message, project):
    plan = Path(project.workspace_path or default_workspace(project)) / "PLAN.md"
    if plan.is_file():
        await answer_formatted(message, plan.read_text(encoding="utf-8").strip() or "План пуст.")
    rows = []
    action = f"project_action:{project.id}:"
    if project.status not in {"draft", "council"}:
        rows.append([("Перегенерировать план", action + "regenerate_plan")])
        if plan.is_file():
            rows.append([("Отправить правки к плану", f"plan_feedback:{project.id}")])
    if project.status == "awaiting_approval":
        rows.append([("Утвердить план и запустить", action + "approve")])
    if project.status in {"paused", "completed"}:
        rows.append([("Откатить последний Git-шаг", action + "rollback")])
    await message.answer(f"План и консилиум · {project.status}", reply_markup=inline_menu(rows) if rows else None)


@router.callback_query(F.data.startswith("plan_feedback:"))
async def plan_feedback(callback: CallbackQuery, session: AsyncSession, account: User | None):
    project = await owned_project(session, account, int(callback.data.split(":")[1]))
    await callback.answer()
    if project is not None:
        await choose_team_target(callback.message, project, session, "plan")


SETTING_OPTIONS = {
    "autonomy_mode": {"autonomous": "Автономный", "consultative": "С вопросами", "step_by_step": "Пошаговый"},
    "failover_mode": {"auto": "Авто", "manual": "С подтверждением"},
    "verbosity_level": {"concise": "Кратко", "balanced": "Сбалансированно", "detailed": "Подробно"},
    "commit_mode": {"sequential": "Последовательные коммиты", "single_preset": "Одним коммитом в конце"},
    "auto_push_mode": {"after_each": "После каждого коммита", "manual_finish": "Вручную по завершении"},
}


async def show_settings(message, project):
    prefix = f"settings:{project.id}:"
    rows = [
        [("Изменить название", prefix + "name")],
        [("Изменить папку на ПК", prefix + "workspace")],
        [("Настроить GitHub-репозиторий", prefix + "git")],
        [(f"Дедлайн · {project.deadline_minutes} мин.", prefix + "deadline")],
        *setting_rows(project, prefix),
        [("⚙️ Настройки аккаунта", "profile")],
    ]
    await message.answer(f"<b>Настройки проекта</b> · {escape(project.name)}\n{escape(workspace_details(project))}",
                         parse_mode="HTML", reply_markup=inline_menu(rows))


@router.callback_query(F.data.startswith("settings:"))
async def change_setting(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, field, *values = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None:
        return
    message = callback.message
    if field == "repo":
        current = await state.get_state()
        current_project = (await state.get_data()).get("project_id")
        expected = ProjectSettings.git_visibility if values in (["private"], ["public"]) else ProjectSettings.git
        if current_project != project.id or current != expected.state:
            return
    await select_project(message, state, account, project.id)
    prompts = {"name": "Введите название проекта.", "workspace": WORKSPACE_PROMPT, "deadline": DEADLINE_PROMPT}
    try:
        if field == "git":
            require_github(account)
            await state.set_state(ProjectSettings.git)
            prefix = f"settings:{project.id}:repo"
            await message.answer("Введите адрес существующего репозитория.", reply_markup=inline_menu(
                git_options(prefix) + [[("Отвязать репозиторий", f"{prefix}:unlink"), ("Отмена", "cancel")]],
            ))
            return
        if field == "repo":
            if values == ["create"]:
                require_github(account)
                await state.set_state(ProjectSettings.git_visibility)
                await message.answer("Выберите видимость репозитория.", reply_markup=repo_visibility_keyboard(
                    f"settings:{project.id}:repo",
                ))
                return
            if values in (["private"], ["public"], ["unlink"]):
                data = ProjectPatch(git_repo_url=None) if values == ["unlink"] else ProjectPatch(
                    github_visibility=values[0],
                )
                await update_project(data, project, session, account)
        elif field in prompts:
            await state.set_state(getattr(ProjectSettings, field))
            await message.answer(prompts[field], reply_markup=cancel_keyboard())
            return
        elif len(values) == 1 and values[0] in SETTING_OPTIONS.get(field, {}):
            await update_project(ProjectPatch.model_validate({field: values[0]}), project, session, account)
    except HTTPException as exc:
        await answer_error(message, exc)
        if field == "git":
            await message.answer("Откройте настройки аккаунта.", reply_markup=github_required_keyboard())
        return
    await show_settings(message, project)


@router.message(StateFilter(
    ProjectSettings.name, ProjectSettings.workspace, ProjectSettings.git, ProjectSettings.git_visibility,
    ProjectSettings.deadline,
))
async def save_project_setting(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    project = await owned_project(session, account, (await state.get_data()).get("project_id"))
    if project is None:
        await leave_project(message, state, account)
        return
    state_name = await state.get_state()
    fields = {
        ProjectSettings.name.state: ("name", "Введите название проекта длиной до 255 символов."),
        ProjectSettings.workspace.state: ("workspace_path", WORKSPACE_PROMPT),
        ProjectSettings.git.state: ("git_repo_url", "Введите адрес Git-репозитория длиной до 500 символов."),
        ProjectSettings.git_visibility.state: ("git_repo_url", "Введите адрес Git-репозитория длиной до 500 символов."),
        ProjectSettings.deadline.state: ("deadline_minutes", DEADLINE_PROMPT),
    }
    field, prompt = fields[state_name]
    value = (message.text or "").strip()
    if field == "workspace_path" and not Path(value).is_absolute():
        await message.answer(prompt)
        return
    try:
        data = ProjectPatch.model_validate({field: int(value) if field == "deadline_minutes" else value})
        await update_project(data, project, session, account)
    except ValueError:
        await message.answer(prompt)
        return
    except HTTPException as exc:
        await answer_error(message, exc)
        return
    await state.set_state(None)
    await show_settings(message, project)


async def answer_error(message, exc):
    await message.answer(escape(str(exc.detail)[:3800]), parse_mode="HTML")


async def send_generated_image(message, session, project_id, media_id):
    media = await session.get(ProjectMedia, media_id)
    if media and media.project_id == project_id and media.mime_type.startswith("image/"):
        await message.answer_photo(BufferedInputFile(media.data, Path(media.filename).name))


async def show_commits(message, project):
    try:
        commits = await project_commits(project)
    except HTTPException as exc:
        await answer_error(message, exc)
        return
    if not commits:
        await message.answer("Коммитов пока нет.")
    for commit in commits[:10]:
        short = commit["short_hash"]
        commit_ref = commit["hash"][:max(12, min(len(short), 32))]
        title = commit["message"].split("\n", 1)[0][:1000]
        stamp = commit["created_at"].astimezone().strftime("%d.%m.%Y %H:%M %Z")
        await message.answer(
            f"<code>{escape(short)}</code> · {escape(title)}\n{escape(commit['author'][:200])} · {escape(stamp)}",
            parse_mode="HTML", reply_markup=inline_menu([
                [(f"⏪ Откатиться до {short}", f"git_back:{project.id}:{commit_ref}")],
            ]),
        )


@router.callback_query(F.data.startswith("git_back:") | F.data.startswith("git_yes:"))
async def git_rollback(callback: CallbackQuery, session: AsyncSession, account: User | None):
    action, project_id, commit_hash = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None or not re.fullmatch(r"[0-9a-f]{7,32}", commit_hash):
        return
    message = callback.message
    if action == "git_back":
        await message.answer(
            f"Откатить файлы и контекст агентов до <code>{commit_hash}</code>? "
            "Незакоммиченные изменения будут потеряны.", parse_mode="HTML",
            reply_markup=inline_menu([
                [("Подтвердить откат", f"git_yes:{project.id}:{commit_hash}"), ("Отмена", "cancel")],
            ]),
        )
        return
    try:
        result = await rollback_commit(project, commit_hash)
    except HTTPException as exc:
        await answer_error(message, exc)
        return
    with suppress(TelegramAPIError):
        await message.edit_reply_markup(reply_markup=None)
    context = "Контекст агентов восстановлен." if result["context_restored"] else (
        "История восстановлена по дате коммита. Снимок состояния агентов отсутствует."
    )
    await message.answer(f"Выполнен откат до <code>{commit_hash}</code>. {context}", parse_mode="HTML")


async def send_push(message, project):
    try:
        await push_project(project)
    except HTTPException as exc:
        await answer_error(message, exc)
        return
    await message.answer("Изменения отправлены в Git.")


async def show_clear_menu(message, project):
    prefix = f"clear_chat:{project.id}:"
    await message.answer("Какую историю скрыть?", reply_markup=inline_menu([
        [("Общий чат", prefix + "team_chat"), ("Все личные чаты", prefix + "direct")],
    ]))


@router.callback_query(F.data.startswith("clear_chat:"))
async def clear_chat(callback: CallbackQuery, session: AsyncSession, account: User | None):
    _, project_id, channel, *values = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None or channel not in {"team_chat", "direct"}:
        return
    try:
        await clear_view(project, ClearView(channel=channel, agent_id=int(values[0]) if values else None), session)
    except HTTPException as exc:
        await answer_error(callback.message, exc)
        return
    await callback.message.answer(
        "История скрыта в Loomis. Контекст агентов и уже отправленные сообщения Telegram сохранены."
    )


async def model_ratings(session, keys):
    rows = await session.execute(select(ModelLeaderboard.model_key, ModelLeaderboard.reliability_score).where(
        ModelLeaderboard.model_key.in_(set(keys))
    ))
    return dict(rows.all())


def model_badges(role, score):
    image = " · [IMG]" if role == "image_gen" else ""
    return f"{image} · ★ {score:.0f}%" if score is not None else f"{image} · ★ —"


async def show_agents(message, project, session):
    agents = list(await session.scalars(select(AgentConfig).where(
        AgentConfig.project_id == project.id
    ).order_by(AgentConfig.id)))
    keys = {agent.id: model_key(agent.provider, agent.model, agent.settings) for agent in agents}
    ratings = await model_ratings(session, keys.values())
    rows = [[("Добавить из Хаба", f"hub_project:{project.id}")]]
    if project.status != "draft":
        rows.append([("Добавить CLI", f"agent_add:{project.id}:cli"), ("Добавить API", f"agent_add:{project.id}:api")])
    for agent in agents:
        prefix = f"agent_manage:{project.id}:{agent.id}:"
        status = agent.traffic_state if agent.enabled else "выключен"
        score = ratings.get(keys[agent.id])
        badges = model_badges(agent.agent_role_type, score)
        actions = [
            [("Выключить" if agent.enabled else "Включить", prefix + ("off" if agent.enabled else "on")),
             ("Проверить", prefix + "test")],
            [("Специализация модели", prefix + "role"), ("Удалить", prefix + "confirm")],
        ]
        if agent.traffic_state == "error":
            actions.append([("Перезапустить (с сохранением контекста)", prefix + "retry")])
        await message.answer(
            f"<b>{escape(agent.name)}</b> · {escape(agent.model)}{badges} · {escape(status)}",
            parse_mode="HTML", reply_markup=inline_menu(actions),
        )
    await message.answer("Агенты проекта", reply_markup=inline_menu(rows))


@router.callback_query(F.data.startswith("agent_manage:"))
async def manage_agent(callback: CallbackQuery, session: AsyncSession, account: User | None):
    _, project_id, agent_id, action = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    agent = await session.get(AgentConfig, int(agent_id)) if project is not None else None
    if agent is None or agent.project_id != project.id:
        await callback.answer("Агент недоступен.", show_alert=True)
        return
    await callback.answer()
    message = callback.message
    if action == "role":
        await message.answer("Выберите специализацию модели.", reply_markup=role_keyboard(
            f"agent_manage:{project.id}:{agent.id}", allow_images=agent.provider.lower() not in CLI_PROVIDERS,
        ))
        return
    if action == "confirm":
        await message.answer(f"Удалить агента <b>{escape(agent.name)}</b>?", parse_mode="HTML", reply_markup=inline_menu([
            [("Подтвердить удаление", f"agent_manage:{project.id}:{agent.id}:delete")]
        ]))
        return
    try:
        if action in ROLE_LABELS:
            await edit_agent(AgentPatch(agent_role_type=action), project, agent, session)
        elif action == "retry":
            await retry_agent(project, agent)
            await message.answer("Запрос на возобновление работы принят.")
        elif action in {"on", "off"}:
            await edit_agent(AgentPatch(enabled=action == "on"), project, agent, session)
        elif action == "test":
            await test_agent(ExistingAgentTest(agent_id=agent.id), project, session)
        elif action == "delete":
            await delete_agent(project, agent, session)
    except HTTPException as exc:
        await answer_formatted(message, str(exc.detail)[:3900])
        return
    await show_agents(message, project, session)


async def show_hub(message, session, account, project=None):
    presets = await list_presets(session, account)
    keys = {preset.id: PresetView.model_validate(preset).model_key for preset in presets}
    ratings = await model_ratings(session, keys.values())
    for preset in presets:
        badges = model_badges(preset.agent_role_type, ratings.get(keys[preset.id]))
        if project is None:
            rows = [
                [("Добавить в проект", f"hub_choose:{preset.id}"), ("Удалить", f"hub_delete:{preset.id}")],
                [("Специализация модели", f"hub_role_edit:{preset.id}")],
            ]
        else:
            rows = [[("Добавить в текущий проект", f"hub_import:{project.id}:{preset.id}")]]
        await message.answer(
            f"<b>{escape(preset.name)}</b> · {escape(preset.adapter_type)} · "
            f"{escape(preset.config_json['model'])}{badges}\nПотрачено токенов: {preset.total_tokens_used}",
            parse_mode="HTML", reply_markup=inline_menu(rows),
        )
    add = f"hub_new:{project.id}" if project is not None else "hub_new"
    await message.answer("Хаб нейросетей" if presets else "В Хабе пока нет моделей.",
                         reply_markup=inline_menu([[("Добавить модель", add)]]))


@router.callback_query(F.data.startswith("hub_role_edit:"))
async def edit_hub_role(callback: CallbackQuery, session: AsyncSession, account: User | None):
    await callback.answer()
    if account is None:
        return
    _, preset_id, *values = callback.data.split(":")
    if not values:
        preset = next((row for row in await list_presets(session, account) if row.id == int(preset_id)), None)
        if preset:
            await callback.message.answer("Выберите специализацию модели.", reply_markup=role_keyboard(
                f"hub_role_edit:{preset_id}", allow_images=preset.adapter_type.lower() not in CLI_PROVIDERS,
            ))
        return
    if values[0] in ROLE_LABELS:
        try:
            await edit_preset(int(preset_id), PresetPatch(agent_role_type=values[0]), session, account)
        except HTTPException as exc:
            await answer_error(callback.message, exc)
            return
        await show_hub(callback.message, session, account)


@router.callback_query(F.data == "hub")
async def hub(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    await callback.answer()
    if account is not None:
        await leave_project(callback.message, state, account)
        await show_hub(callback.message, session, account)


@router.callback_query(F.data.startswith("hub_project:"))
async def project_hub(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    project = await owned_project(session, account, int(callback.data.split(":")[1]))
    await callback.answer()
    if project is not None:
        await select_project(callback.message, state, account, project.id)
        await show_hub(callback.message, session, account, project)


@router.callback_query(F.data.startswith("hub_choose:"))
async def hub_choose_project(callback: CallbackQuery, session: AsyncSession, account: User | None):
    preset_id = int(callback.data.split(":")[1])
    await callback.answer()
    if account is None:
        return
    projects = list(await session.scalars(select(Project).where(Project.owner_id == account.id).order_by(Project.id)))
    rows = [[(project.name, f"hub_import:{project.id}:{preset_id}")] for project in projects]
    await callback.message.answer("Выберите проект." if rows else "Сначала создайте проект.",
                                  reply_markup=inline_menu(rows) if rows else main_menu(True))


@router.callback_query(F.data.startswith("hub_import:"))
async def hub_import(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, preset_id = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None:
        return
    try:
        await import_presets(HubImport(preset_ids=[int(preset_id)]), project, session)
    except HTTPException as exc:
        await answer_formatted(callback.message, str(exc.detail)[:3900])
        return
    await select_project(callback.message, state, account, project.id)
    await callback.message.answer("Модель добавлена в проект.", reply_markup=project_keyboard(project))


@router.callback_query(F.data.startswith("hub_delete:"))
async def hub_delete(callback: CallbackQuery, session: AsyncSession, account: User | None):
    await callback.answer()
    if account is None:
        return
    try:
        await delete_preset(int(callback.data.split(":")[1]), session, account)
    except HTTPException:
        await callback.message.answer("Модель не найдена.")
        return
    await show_hub(callback.message, session, account)


@router.callback_query((F.data == "hub_new") | F.data.startswith("hub_new:"))
async def hub_new(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    await callback.answer()
    if account is None:
        return
    _, _, project_id = callback.data.partition(":")
    if project_id:
        project = await owned_project(session, account, int(project_id))
        if project is None:
            return
        await select_project(callback.message, state, account, project.id)
    else:
        await leave_project(callback.message, state, account)
    await state.set_state(HubSetup.name)
    await callback.message.answer("Введите имя модели в Хабе.", reply_markup=cancel_keyboard())


@router.message(HubSetup.name)
async def hub_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 100 or "\n" in name or "\r" in name:
        await message.answer("Введите имя длиной от 2 до 100 символов в одной строке.")
        return
    await state.update_data(hub_name=name)
    await state.set_state(HubSetup.role)
    await message.answer("Выберите специализацию модели.", reply_markup=role_keyboard("hub_role"))


@router.callback_query(HubSetup.role, F.data.startswith("hub_role:"))
async def choose_hub_role(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    role = callback.data.split(":")[1]
    if role not in ROLE_LABELS:
        return
    await state.update_data(agent_role_type=role)
    if role == "image_gen":
        await state.update_data(hub_adapter="api")
        await state.set_state(HubSetup.base_url)
        await callback.message.answer("Введите Base URL с http:// или https://.")
        return
    await state.set_state(HubSetup.adapter)
    await callback.message.answer("Выберите тип подключения.", reply_markup=inline_menu([
        [("CLI (Codex)", "hub_adapter:codex"), ("API", "hub_adapter:api")],
        [("❌ Отмена", "cancel")],
    ]))


@router.callback_query(HubSetup.adapter, F.data.in_({"hub_adapter:codex", "hub_adapter:api"}))
async def hub_adapter(callback: CallbackQuery, state: FSMContext):
    adapter = callback.data.split(":")[1]
    await callback.answer()
    await state.update_data(hub_adapter=adapter)
    if adapter == "api":
        await state.set_state(HubSetup.base_url)
        await callback.message.answer("Введите Base URL с http:// или https://.")
    else:
        await state.set_state(HubSetup.model)
        await callback.message.answer("Введите идентификатор модели Codex.")


@router.message(HubSetup.base_url)
async def hub_url(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    try:
        PresetCreate(name="Проверка", adapter_type="api",
                     config_json={"model": "probe", "base_url": url, "api_key": "probe"}).agent_settings()
    except HTTPException:
        await message.answer("Введите корректный Base URL с http:// или https://.")
        return
    await state.update_data(hub_url=url)
    await state.set_state(HubSetup.api_key)
    await message.answer("Введите API Key.")


@router.message(HubSetup.api_key)
async def hub_key(message: Message, state: FSMContext):
    key = (message.text or "").strip()
    if not key:
        await message.answer("Введите API Key текстом.")
        return
    await state.update_data(hub_key=key)
    await state.set_state(HubSetup.model)
    await message.answer("Введите идентификатор модели.")


@router.message(HubSetup.model)
async def hub_model(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    if account is None:
        await state.clear()
        return
    data = await state.get_data()
    model = (message.text or "").strip()
    config = {"model": model, "base_url": data.get("hub_url"), "api_key": data.get("hub_key")}
    if data["hub_adapter"] == "codex":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", model):
            await message.answer("Введите идентификатор модели Codex без пробелов.")
            return
        config.update(
            command_template=f"codex exec --json --sandbox workspace-write --model {model} -",
            question_command=f"codex exec --json --skip-git-repo-check --sandbox read-only --model {model} -",
        )
    try:
        preset = PresetCreate(name=data["hub_name"], adapter_type=data["hub_adapter"],
                              agent_role_type=data["agent_role_type"], config_json=config)
        await create_preset(preset, session, account)
    except ValidationError:
        await message.answer("Введите идентификатор модели длиной до 255 символов.")
        return
    except HTTPException:
        await message.answer("Проверьте настройки подключения и начните добавление заново.",
                             reply_markup=cancel_keyboard())
        return
    project = await owned_project(session, account, data.get("project_id"))
    await state.set_state(None)
    await state.set_data({"project_id": project.id} if project is not None else {})
    await show_hub(message, session, account, project)


@router.message(StateFilter(None))
async def help_message(message: Message, account: User | None):
    await message.answer("Выберите действие в меню.", reply_markup=main_menu(account is not None))
