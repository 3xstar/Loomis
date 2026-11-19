import json
import logging
import re
import secrets
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import Credentials, create_account, link_telegram
from backend.api.projects import AgentCreate, ProjectCreate, create_project, default_workspace, save_agent
from backend.api.ws import manager
from backend.core.context_manager import build_spec
from backend.core.doc_parser import parse_document
from backend.core.voice import VoiceTranscriber
from backend.core.orchestrator import orchestrator
from backend.database.models import AgentConfig, Project, SpecItem, User
from backend.settings import ROOT

from .keyboards import (agent_chat_keyboard, cancel_keyboard, first_project_keyboard, main_menu,
                        project_keyboard, projects_keyboard, spec_keyboard)
from .middlewares import AccountMiddleware
from .states import AccountSetup, ProjectSetup

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


async def prompt_username(message: Message, state: FSMContext):
    await state.set_state(AccountSetup.username)
    await message.answer("Введите желаемый логин (никнейм) без пробелов:", reply_markup=cancel_keyboard())


async def prompt_code(message: Message, state: FSMContext):
    await state.set_state(AccountSetup.link_code)
    await message.answer("Введите 6-значный код из десктопного приложения Loomis:", reply_markup=cancel_keyboard())


@router.message(CommandStart())
async def start(message: Message, account: User | None, state: FSMContext):
    if await state.get_state() == ProjectSetup.spec_buffer.state:
        project_id = (await state.get_data()).get("project_id")
        await message.answer("Буфер ТЗ открыт. Отправьте текст, аудио или документ.", reply_markup=spec_keyboard(project_id))
    else:
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


async def link_account(message: Message, code: str, session: AsyncSession, state: FSMContext):
    if not re.fullmatch(r"[0-9]{6}", code):
        await message.answer("Код должен состоять из 6 цифр. Введите код ещё раз:")
        await state.set_state(AccountSetup.link_code)
        return
    if await link_telegram(session, code, message.from_user.id):
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
async def projects(message: Message, session: AsyncSession, account: User | None):
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
async def cancel(message: Message, state: FSMContext, account: User | None):
    await state.clear()
    await message.answer("Текущее действие отменено.", reply_markup=main_menu(account is not None))


@router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext, account: User | None):
    await state.clear()
    await callback.answer("Действие отменено")
    await callback.message.answer("Текущее действие отменено.", reply_markup=main_menu(account is not None))


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
async def projects_callback(callback: CallbackQuery, session: AsyncSession, account: User | None):
    await callback.answer()
    await show_projects(callback.message, session, account)


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery, account: User | None):
    await callback.answer()
    await callback.message.answer(f"Профиль: {account.username}\nTelegram привязан.")


@router.callback_query(F.data.startswith("project:"))
async def project_card(callback: CallbackQuery, session: AsyncSession, account: User | None):
    project_id = int(callback.data.split(":", 1)[1])
    project = await owned_project(session, account, project_id)
    await callback.answer()
    if project is None:
        await callback.message.answer("Проект не найден.")
        return
    count = await session.scalar(select(func.count(SpecItem.id)).where(SpecItem.project_id == project.id))
    created = project.created_at.strftime("%Y-%m-%d %H:%M") if project.created_at else "—"
    updated = project.updated_at.strftime("%Y-%m-%d %H:%M") if getattr(project, "updated_at", None) else "—"
    await callback.message.answer(f"{project.name}\nСтатус: {project.status}\nСоздан: {created}\nОбновлён: {updated}\nЗаписей ТЗ: {count}", reply_markup=project_keyboard(project))


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
        await state.set_data({"project_id": project.id})
        await state.set_state(ProjectSetup.spec_buffer)
        count = await session.scalar(select(func.count(SpecItem.id)).where(SpecItem.project_id == project.id))
        await callback.message.answer(f"Буфер ТЗ — {count} записей. Отправьте текст, аудио или документ.", reply_markup=spec_keyboard(project.id))


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
        await callback.message.answer(content[:4000] or "SPEC.md пуст.")


@router.callback_query(F.data.startswith("delete_project:"))
async def delete_project_callback(callback: CallbackQuery, session: AsyncSession, account: User | None):
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
    await callback.message.answer("Проект удалён.")
    await show_projects(callback.message, session, account)


@router.message(ProjectSetup.name)
async def project_name(message: Message, state: FSMContext, session: AsyncSession, account: User | None):
    if account is None:
        await message.answer(LINK_HELP, reply_markup=main_menu(False))
        return
    try:
        data = ProjectCreate(name=message.text or "")
    except ValidationError:
        await message.answer("Введите название проекта текстом, до 255 символов.")
        return
    project = await create_project(session, account.id, data)
    await state.set_data({"project_id": project.id})
    await state.set_state(ProjectSetup.spec_buffer)
    await message.answer("Буфер ТЗ — 0 записей. Отправьте текст, аудио или документ.", reply_markup=spec_keyboard(project.id))


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


@router.message(ProjectSetup.spec_buffer, F.text | F.voice | F.audio | F.document)
async def spec_item(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, account: User | None):
    project = await buffer_project(session, account, (await state.get_data()).get("project_id"))
    if project is None:
        await state.clear()
        await message.answer("Буфер ТЗ недоступен. Создайте новый проект кнопкой меню.", reply_markup=main_menu(account is not None))
        return
    if message.text is not None:
        title, content = "Текст", message.text.strip()
    else:
        try:
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
    await state.clear()
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
    await state.clear()
    await callback.message.answer("Черновик проекта и его буфер ТЗ удалены.", reply_markup=main_menu(True))


@router.message(ProjectSetup.spec_buffer)
async def unsupported_spec_item(message: Message):
    await message.answer("Отправьте текст, голосовое сообщение, аудио или документ.")


class AgentInteraction(StatesGroup):
    configure = State()
    question = State()


@router.callback_query(F.data.startswith("project_action:"))
async def project_action(callback: CallbackQuery, session: AsyncSession, account: User | None):
    _, project_id, action = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None:
        return
    async def reply():
        try:
            project.status = await orchestrator.action(project.id, action)
        except (OSError, ValueError, RuntimeError) as exc:
            await callback.message.answer(str(exc))
        else:
            text = "Последний шаг откачен." if action == "rollback" else f"Статус проекта — {project.status}."
            await callback.message.answer(text, reply_markup=project_keyboard(project))

    orchestrator.background_reply(reply())


@router.callback_query(F.data.startswith("agent_add:"))
async def configure_agent(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, provider = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None or project.status == "draft" or provider not in {"api", "cli"}:
        return
    await state.set_data({"project_id": project.id, "provider": provider})
    await state.set_state(AgentInteraction.configure)
    examples = {
        "cli": '{"name":"Codex","model":"codex","settings":{"command":"codex exec --json --sandbox workspace-write -","question_command":"codex exec --json --skip-git-repo-check --sandbox read-only -"}}',
        "api": '{"name":"Ollama","model":"qwen3","settings":{"base_url":"http://localhost:11434/v1"}}',
    }
    await callback.message.answer(f"Отправьте конфигурацию агента в JSON.\n{examples[provider]}",
                                  reply_markup=cancel_keyboard())


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
        agent = AgentCreate.model_validate(values)
        session.add(AgentConfig(project_id=project.id, **agent.model_dump()))
        await save_agent(session)
    except (ValueError, HTTPException):
        await message.answer("Проверьте JSON, модель и уникальность имени агента.")
        return
    await state.clear()
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
    agents = await enabled_agents(session, project.id)
    text = "Выберите агента." if agents else "Добавьте агента в карточке проекта."
    await callback.message.answer(text, reply_markup=agent_chat_keyboard(project.id, agents))


@router.callback_query(F.data.startswith("agent_chat:"))
async def open_agent_chat(callback: CallbackQuery, state: FSMContext, session: AsyncSession, account: User | None):
    _, project_id, agent_id = callback.data.split(":")
    project = await owned_project(session, account, int(project_id))
    await callback.answer()
    if project is None or not any(item.id == int(agent_id) for item in await enabled_agents(session, project.id)):
        return
    await state.set_data({"project_id": project.id, "agent_id": int(agent_id)})
    await state.set_state(AgentInteraction.question)
    await callback.message.answer("Отправьте вопрос текстом или голосовым сообщением.",
                                  reply_markup=cancel_keyboard())


@router.message(AgentInteraction.question, F.text | F.voice)
async def agent_chat_question(message: Message, bot: Bot, state: FSMContext,
                              session: AsyncSession, account: User | None):
    data = await state.get_data()
    project = await owned_project(session, account, data["project_id"])
    if project is None:
        await state.clear()
        return
    try:
        question = message.text
        if question is None:
            try:
                _, question = await read_attachment(message, bot)
            except Exception:
                logger.exception("Voice question transcription failed")
                await message.answer("Не удалось распознать голосовое сообщение.")
                return
        if not question.strip():
            await message.answer("В сообщении нет вопроса.")
            return
    except (OSError, ValueError) as exc:
        await message.answer(str(exc))
        return

    async def reply():
        try:
            response = await orchestrator.question(project.id, data["agent_id"], question)
        except (OSError, ValueError) as exc:
            await message.answer(str(exc))
            return
        text = response["message"] or response["status"]
        for offset in range(0, len(text), 3900):
            await message.answer(text[offset:offset + 3900])

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


@router.message(StateFilter(None))
async def help_message(message: Message, account: User | None):
    await message.answer("Выберите действие в меню.", reply_markup=main_menu(account is not None))
