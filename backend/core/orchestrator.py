import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from sqlalchemy import func, or_, select, true, update
from sqlalchemy.exc import SQLAlchemyError
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image

from backend.api.ws import manager, workers
from backend.bot.formatting import telegram_html
from backend.bot.i18n import EN, STATUS, bot_language, localize_markup, translate
from backend.database.db import async_session_factory
from backend.database.models import AgentConfig, AgentMessage, CommitState, Project, ProjectMedia, ResponseCache, TaskLog, User, UserModelPreset
from worker.adapters import CLI_PROVIDERS, build_agent
from worker.adapters.api_adapter import REVIEW_INSTRUCTION, ApiAgent, api_system_prompt, run_workspace_command
from worker.adapters.base import AgentResult, BaseAgent
from worker.adapters.media import refuses_media
from worker.git_bridge import GitBridge, is_github_repo
from worker.quality_gate import check_quality
from worker.sandbox import workspace_file

from .council import (
    autonomy_instruction, is_compact_plan, periodic_meeting, scheduled_steps, starting_council, validate_steps,
)
from .github import github_token
from .model_performance import cache_response, model_key, prompt_digest, rank_configs, record_model_result

logger = logging.getLogger(__name__)
STATUS["review_pending"] = ("На проверке", "Pending review")
EN.update({
    "Результат готов к проверке.": "The result is ready for review.",
    "Подтвердить завершение": "Confirm completion",
    "Продолжить разработку": "Continue development",
    "Вернуться к проекту": "Return to project",
    "Проект больше не ожидает проверки.": "The project is no longer pending review.",
})

COMMIT_INSTRUCTION = (
    "COMMIT_MSG: <тип>: <совершённый факт в прошедшем времени>. "
    "Строго соблюдай Conventional Commits. Запрещены призывы к действию, "
    "инфинитивы («проверить», «подготовить»), повелительное наклонение и названия моделей ИИ или агентов. "
    "Примеры: COMMIT_MSG: docs: подготовлен и проверен паспорт проекта; "
    "COMMIT_MSG: feat: добавлена авторизация."
)

def message_event(row, agent_name):
    return {
        "type": row.channel, "channel": row.channel, "project_id": row.project_id, "id": row.id,
        "sender_name": row.sender_name, "target_name": row.target_name,
        "content": row.content, "message": row.content, "created_at": row.created_at.isoformat(),
        "agent_id": row.agent_id, "agent_name": agent_name, "media_id": row.media_id,
        "step_title": row.step_title,
    }


def history_visible(project, model):
    ranges = (project.history_state or {}).get(model.__tablename__, [])
    if not ranges:
        return true()
    return ~or_(*(model.id.between(start, end) for start, end in ranges))


def merge_ranges(ranges, start, end):
    merged = []
    if start <= end:
        ranges = [*ranges, [start, end]]
    for left, right in sorted(ranges):
        if merged and left <= merged[-1][1] + 1:
            merged[-1][1] = max(right, merged[-1][1])
        else:
            merged.append([left, right])
    return merged


STEP_TITLES = {
    "question": "Ответ на вопрос пользователя",
    "chat": "Ответ в общем чате",
    "plan": "Формирование плана",
    "meeting": "Промежуточное собрание команды",
    "execute": "Выполнение шага разработки",
    "handoff": "Приём задачи от агента",
}


@dataclass
class Runtime:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    boundary: asyncio.Lock = field(default_factory=asyncio.Lock)
    telegram_delivery: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task | None = None
    last_agent: int = 0
    next_meeting: float | None = None
    meeting_base: str | None = None
    council: asyncio.Task | None = None
    gate: asyncio.Task | None = None
    handoff: tuple[int, str] | None = None
    successful_agents: list[int] = field(default_factory=list)
    pending_question: int | None = None
    answer: str = ""
    pending_failover: str | None = None
    retry_agent: int | None = None
    plan_step: int = 0
    restored: bool = False

    def restore(self, project):
        if self.restored:
            return
        if state := project.turn_state:
            self.last_agent = state["last_agent"]
            self.handoff = tuple(state["handoff"]) if state["handoff"] else None
            self.successful_agents = state["successful_agents"]
            self.pending_question = state["pending_question"]
            self.pending_failover = state["pending_failover"]
            self.answer = state["answer"]
            self.retry_agent = state.get("retry_agent")
            self.plan_step = state.get("plan_step", 0)
        self.restored = True

    def snapshot(self):
        return {
            "last_agent": self.last_agent, "handoff": self.handoff,
            "successful_agents": self.successful_agents, "pending_question": self.pending_question,
            "pending_failover": self.pending_failover, "answer": self.answer,
            "retry_agent": self.retry_agent, "plan_step": self.plan_step,
        }


def image_agent(config, workspace):
    if config.provider.lower() in CLI_PROVIDERS or config.settings.get("worker"):
        raise ValueError("Генератор изображений требует прямое подключение к OpenAI-совместимому API.")
    return ApiAgent.from_config(config.model, config.settings, workspace_path=workspace)


async def probe_agent(project_id, config):
    prompt = "Ответь одним словом: OK"
    started = monotonic()
    success = False
    cancelled = False
    try:
        if config.agent_role_type == "image_gen":
            async with asyncio.timeout(90):
                with TemporaryDirectory() as directory:
                    await image_agent(config, Path(directory)).generate_image(
                        "Minimal black circle on a plain white background.", "test.png",
                    )
            success = True
            return 0
        async with asyncio.timeout(15):
            node = config.settings.get("worker")
            if node:
                result = await workers.dispatch(node, config.settings.get("worker_agent", config.name),
                                                project_id, "test", prompt, "", 15)
            else:
                agent = build_agent(config.provider, config.model, config.settings)
                result = await agent.ask_question(prompt, "")
        success = result.status == "success" and bool(result.message.strip())
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if not cancelled:
            async with async_session_factory() as session:
                await record_model_result(session, config, success, (monotonic() - started) * 1000)
                await session.commit()
    if result.status != "success":
        raise ValueError(result.message or result.status)
    if not result.message.strip():
        raise ValueError("Провайдер вернул пустой ответ.")
    return result.tokens


VERBOSITY = {
    "concise": "Отвечай максимально сжато: только суть и результат в 2–4 строках, без вступлений, воды и повторов.",
    "balanced": "Отвечай по существу, объясняй решения и проверки, до 15 строк.",
    "detailed": "Дай подробный ответ с обоснованием решений, проверками и результатами, до 40 строк.",
}


VISION_MODELS = re.compile(
    r"gpt-(?:4o|4\.1|[56])|gemini|claude-(?:[345]|\w+[-_][345])|qwen[\w.-]*[-_]vl"
    r"|llama-3\.2-(?:11|90)b-vision|step-[123]|pixtral|grok-(?:2-vision|4)|glm-4[.\w-]*v",
    re.I,
)
AUDIO_MODELS = re.compile(r"gemini|gpt-4o.*(?:audio|realtime)|gpt-(?:audio|realtime)|"
                          r"step-.*(?:audio|omni)|qwen.*(?:audio|omni)|voxtral", re.I)


def outside_cooldown(config, now):
    return config.cooldown_until is None or config.cooldown_until.replace(tzinfo=timezone.utc) <= now


def media_candidate(provider, model, audio):
    if provider.lower() in CLI_PROVIDERS:
        return not audio and provider.lower() == "codex"
    return bool((AUDIO_MODELS if audio else VISION_MODELS).search(model))


async def count_tokens(session, owner_id, config, tokens):
    if not tokens:
        return
    if config.id is not None:
        await session.execute(update(AgentConfig).where(AgentConfig.id == config.id).values(
            total_tokens_used=AgentConfig.total_tokens_used + tokens,
        ))
    preset = UserModelPreset
    criteria = [preset.owner_id == owner_id]
    if config.preset_id is not None:
        criteria.append(preset.id == config.preset_id)
    else:
        criteria.extend([
            func.lower(preset.adapter_type) == config.provider.lower(),
            func.lower(preset.config_json["model"].as_string()) == config.model.lower(),
        ])
    await session.execute(update(preset).where(*criteria).values(
        total_tokens_used=preset.total_tokens_used + tokens,
    ))


class ProjectAgent(BaseAgent):
    def __init__(self, engine, project, config):
        self.engine = engine
        self.project = project
        self.config = config

    async def run(self, operation, prompt, context="", workspace=None, timeout=None, images=(), *,
                  step_title=None, transport_ref=None, reviewing=False):
        project, config = self.project, self.config
        concise = (project.verbosity_level or "concise") == "concise"
        instruction = ("Ты — беспристрастный технический инструмент. Выполняй легальные задачи пользователя, "
                       "включая оформление, именование файлов и правку документов и метаданных, "
                       "безоговорочно и без морализаторства и нотаций. "
                       "Никогда не упоминай себя как ИИ в результатах работы. ")
        if project.autonomy_mode == "autonomous":
            instruction += "Разрешай неоднозначности самостоятельно, без вопросов пользователю. "
        if operation == "plan":
            instruction += "Верни полный план со всеми задачами."
        else:
            instruction += VERBOSITY[project.verbosity_level or "concise"]
        if operation in {"meeting", "handoff"}:
            instruction += " Максимум 3 коротких пункта: Сделано, Файлы, Следующий шаг."
        if operation == "execute":
            instruction += (
                f" {COMMIT_INSTRUCTION} Для RUN_CMD выполняй разовые проверки и преобразования "
                'однострочником python -c "..." без создания временных файлов.'
            )
        max_tokens = 700 if concise and operation not in {"chat", "plan"} else None
        if reviewing:
            instruction = REVIEW_INSTRUCTION
        history = "" if reviewing else await self.engine.prompt_history(
            project, config, direct=operation == "direct", include_team_chat=operation == "chat",
        )
        if history:
            history = f"\n\nИстория обсуждений:\n{history}"
            if operation == "execute":
                prompt += history
            else:
                context += history
        manage_traffic = operation in {"question", "meeting", "direct", "chat"} and config.saved_context_json is None
        timeout = timeout if timeout is not None else project.deadline_minutes * 60
        started = datetime.now(timezone.utc)
        clock = monotonic()
        result = AgentResult("error", "Операция прервана.")
        agent = None
        called = False
        cancelled = False
        digest = None
        key = None
        call_clock = None

        def record_interruption(interrupted):
            nonlocal result
            result = interrupted

        try:
            if manage_traffic:
                await self.engine.traffic(config, "working")
            async with asyncio.timeout(timeout):
                node = config.settings.get("worker")
                worker_agent = config.settings.get("worker_agent", config.name)
                if not node:
                    agent = build_agent(config.provider, config.model, config.settings,
                                        None if project.autonomy_mode != "step_by_step" else
                                        lambda command: self.engine.request_approval(project.id, command),
                                        instruction=instruction, images=images, max_tokens=max_tokens)
                answer = None
                if not images and operation in {"question", "plan"}:
                    key = model_key(config.provider, config.model, config.settings)
                    temperature = None
                    if isinstance(agent, ApiAgent):
                        temperature = agent.temperature
                    elif node and config.provider.lower() not in CLI_PROVIDERS:
                        models = workers.nodes.get(node, {}).get("api_models", {})
                        advertised = models.get(worker_agent)
                        if isinstance(advertised, list) and len(advertised) == 2 and advertised[0] == key:
                            temperature = advertised[1]
                    if type(temperature) in {int, float} and 0 <= temperature <= 0.2:
                        digest = prompt_digest(key, api_system_prompt(instruction), f"{context}\n\n{prompt}")
                        async with async_session_factory() as session:
                            cached = (await session.execute(update(ResponseCache).where(
                                ResponseCache.prompt_hash == digest,
                                ResponseCache.created_at > datetime.now(timezone.utc) - timedelta(hours=24),
                            ).values(tokens_saved=ResponseCache.tokens_saved + ResponseCache.response_tokens)
                                .returning(ResponseCache.response_text, ResponseCache.response_tokens))).one_or_none()
                            if cached is not None:
                                answer, tokens_saved = cached
                                session.add(TaskLog(project_id=project.id, agent_id=config.id,
                                                    operation="cache_tokens_saved", duration_seconds=0,
                                                    status="success", tokens=tokens_saved))
                            await session.commit()
                if answer is not None:
                    result = AgentResult("success", answer)
                else:
                    called = True
                    call_clock = monotonic()
                    if node:
                        worker_operation = {"handoff": "question", "chat": "question",
                                            "direct": "execute"}.get(operation, operation)
                        if operation == "direct":
                            prompt = f"{context}\n\n{prompt}"
                        result = await workers.dispatch(node, worker_agent,
                                                        project.id, worker_operation, prompt, context, timeout,
                                                        instruction=instruction, images=images, max_tokens=max_tokens,
                                                        on_interrupted=record_interruption, transport_ref=transport_ref)
                    elif operation == "direct":
                        result = await agent.execute_task(f"{context}\n\n{prompt}", workspace, timeout)
                    elif operation == "execute":
                        result = await agent.execute_task(prompt, workspace, timeout)
                    elif operation == "meeting":
                        result = await agent.participate_in_meeting(context)
                    elif operation == "plan" and isinstance(agent, ApiAgent):
                        result = await agent.draft_plan(prompt, context, timeout)
                    else:
                        result = await agent.ask_question(prompt, context)
        except TimeoutError:
            result = AgentResult("timeout", "Дедлайн истёк.")
        except asyncio.CancelledError:
            cancelled = True
            if agent is not None:
                result.tokens = getattr(agent, "tokens_used", 0)
            if result.status == "error":
                result.status = "timeout"
            raise
        except Exception as exc:
            result = AgentResult("error", str(exc) or type(exc).__name__)
        finally:
            duration = monotonic() - (call_clock if call_clock is not None else clock)
            async with async_session_factory() as session:
                session.add(TaskLog(project_id=project.id, agent_id=config.id,
                                    operation="execute_attempt" if operation == "execute" else operation,
                                    sender_name=config.name, started_at=started, duration_seconds=duration,
                                    status=result.status, tokens=result.tokens))
                await count_tokens(session, project.owner_id, config, result.tokens)
                if called and not cancelled:
                    success = result.status == "success" and bool(result.message.strip())
                    await record_model_result(session, config, success, duration * 1000)
                    if digest and success and (result.cacheable or (
                        isinstance(agent, ApiAgent) and agent.used_requested_temperature
                    )):
                        await cache_response(session, digest, key, result)
                if result.status == "rate_limit":
                    row = await session.get(AgentConfig, config.id)
                    if row is not None:
                        row.cooldown_until = datetime.now(timezone.utc) + timedelta(
                            minutes=max(1, int(config.settings.get("cooldown_minutes", 5)))
                        )
                await session.commit()
            terminal = result.terminal or result.message
            if operation != "chat" and not (
                project.autonomy_mode == "autonomous" and "LOOMIS_QUESTION" in terminal
            ):
                await self.engine.message(
                    project.id, config.name, "@All", "terminal", terminal, config,
                    step_title=step_title or STEP_TITLES[operation],
                )
        if manage_traffic:
            await self.engine.traffic(
                config, "waiting" if result.status in {"success", "unsupported_media"} else "error",
                None if result.status in {"success", "unsupported_media"} else result.message,
            )
        return result

    async def execute_task(self, prompt, work_dir, timeout):
        return await self.run("execute", prompt, workspace=work_dir, timeout=timeout)

    async def ask_question(self, question, context, *, step_title=None):
        images = await self.engine.spec_images(self.project.id)
        return await self.run_with_spec_images("question", question, context, images=images, step_title=step_title)

    async def run_with_spec_images(self, operation, prompt, context="", *, images, **options):
        result = await self.run(operation, prompt, context, images=images, **options)
        if images and result.status == "unsupported_media":
            retry = await self.run(operation, prompt, context, **options)
            retry.tokens += result.tokens
            return retry
        return result

    async def participate_in_meeting(self, history):
        return await self.run("meeting", "", history)


class Orchestrator:
    def __init__(self):
        self.runtimes = {}
        self.approvals = {}
        self.bot = None
        self.telegram_projects: dict[int, int] = {}
        self.replies = set()

    def runtime(self, project_id):
        return self.runtimes.setdefault(project_id, Runtime())

    async def load(self, project_id):
        async with async_session_factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise ValueError("Проект не найден.")
            configs = list(await session.scalars(select(AgentConfig).where(
                AgentConfig.project_id == project_id, AgentConfig.enabled.is_(True)
            ).order_by(AgentConfig.id)))
        return project, configs

    async def status(self, project_id, status, **updates):
        async with async_session_factory() as session:
            project = await session.get(Project, project_id)
            project.status = status
            for field, value in updates.items():
                setattr(project, field, value)
            await session.commit()
        await manager.publish_status(project_id, status)

    async def save_turn(self, project_id, runtime):
        async with async_session_factory() as session:
            project = await session.get(Project, project_id)
            project.turn_state = runtime.snapshot()
            await session.commit()

    async def message(self, project_id, sender, target, channel, content, config=None, *,
                      media=None, mirror=None, step_title=None):
        agent_id = config.id if config else None
        deliveries = ((channel, target), (mirror, "@User")) if mirror else ((channel, target),)
        rows = [AgentMessage(project_id=project_id, sender_name=sender, target_name=recipient,
                             channel=destination, content=content, agent_id=agent_id, step_title=step_title)
                for destination, recipient in deliveries]
        async with async_session_factory() as session:
            if media is not None:
                session.add(media)
                await session.flush()
                for row in rows:
                    row.media_id = media.id
            session.add_all(rows)
            await session.commit()
        for row in rows:
            await manager.broadcast(project_id, message_event(row, config.name if config else None))
        if self.bot and any(row.channel == "team_chat" for row in rows) and self.telegram_projects:
            async with async_session_factory() as session:
                owner = await session.scalar(select(User).join(
                    Project, Project.owner_id == User.id
                ).where(Project.id == project_id))
            if owner and owner.telegram_id and self.telegram_projects.get(owner.id) == project_id:
                self.background_reply(self.send_team_chat(
                    project_id, owner.id, owner.telegram_id, sender, target, content,
                    media if sender != "User" else None, owner.language,
                ))
        return rows[0].media_id

    async def prompt_history(self, project, config, *, direct=False, include_team_chat=False):
        query = select(AgentMessage).where(
            AgentMessage.project_id == project.id, history_visible(project, AgentMessage),
        )
        if direct:
            query = query.where(AgentMessage.channel == "direct", AgentMessage.agent_id == config.id)
        else:
            query = query.where(AgentMessage.channel == "team_chat").limit(40)
            if not include_team_chat:
                query = query.where(or_(AgentMessage.step_title.is_(None),
                                        AgentMessage.step_title != STEP_TITLES["chat"]))
        async with async_session_factory() as session:
            rows = list(await session.scalars(query.order_by(AgentMessage.id.desc())))
        if direct:
            return "\n".join(f"{row.sender_name} → {row.target_name}: {row.content}" for row in reversed(rows))
        return "\n".join(f"{row.sender_name} → {row.target_name}: {row.content[-4000:]}"
                         for row in reversed(rows))[-16000:]

    async def send_team_chat(self, project_id, owner_id, chat_id, sender, target, content, media=None,
                             language="ru"):
        if sender == "Loomis" or (content or "").startswith(f"От: {sender}\nКому: "):
            content = translate(content, language)
        heading = escape(f"{sender} → {target}"[:200])
        async with self.runtime(project_id).telegram_delivery:
            for part in telegram_html(content or "—"):
                while self.telegram_projects.get(owner_id) == project_id:
                    try:
                        locale = bot_language.set("ru")
                        try:
                            await self.bot.send_message(chat_id, f"<b>{heading}</b>\n{part}", parse_mode="HTML")
                        finally:
                            bot_language.reset(locale)
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(exc.retry_after)
                    else:
                        break
            if media is not None:
                while self.telegram_projects.get(owner_id) == project_id:
                    try:
                        await self.bot.send_photo(chat_id, BufferedInputFile(media.data, Path(media.filename).name))
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(exc.retry_after)
                    else:
                        break

    async def publish_traffic(self, config):
        await manager.broadcast(config.project_id, {
            "type": "traffic_state", "project_id": config.project_id,
            "agent_id": config.id, "agent_name": config.name,
            "traffic_state": config.traffic_state, "last_error": config.last_error, "can_retry": config.can_retry,
        })

    async def traffic(self, config, state, error=None, *, saved_context=None, clear_context=False):
        async with async_session_factory() as session:
            row = await session.get(AgentConfig, config.id)
            if row is None:
                return
            row.traffic_state = state
            row.last_error = error
            if saved_context is not None or clear_context:
                row.saved_context_json = saved_context
            await session.commit()
        await self.publish_traffic(row)

    @staticmethod
    def invalidate_plan(project):
        project.plan_digest = None
        project.approved_plan_digest = None
        project.turn_state = None
        if project.status not in {"draft", "planning"}:
            project.status = "planning"

    async def plan_invalidated(self, project, *, publish=True):
        runtime = self.runtime(project.id)
        runtime.ready.clear()
        runtime.handoff = None
        async with async_session_factory() as session:
            asking = list(await session.scalars(select(AgentConfig).where(
                AgentConfig.project_id == project.id, AgentConfig.traffic_state == "asking"
            )))
        for config in asking:
            await self.traffic(config, "waiting")
        runtime.pending_question = None
        runtime.pending_failover = None
        runtime.retry_agent = None
        runtime.answer = ""
        runtime.last_agent = 0
        runtime.successful_agents.clear()
        runtime.plan_step = 0
        runtime.restored = True
        if runtime.gate and not runtime.gate.done():
            runtime.gate.cancel()
            await asyncio.gather(runtime.gate, return_exceptions=True)
        if publish:
            await manager.publish_status(project.id, project.status)

    def plan_digest(self, project):
        return sha256(project.plan_content.encode("utf-8")).hexdigest() if project.plan_content else None

    def require_plan(self, project, configs, *, approved):
        if not configs:
            raise ValueError("Добавьте хотя бы одного агента.")
        expected = project.approved_plan_digest if approved else project.plan_digest
        if not expected or expected != project.plan_digest or self.plan_digest(project) != expected:
            raise ValueError("Создайте и утвердите актуальный план.")
        return expected

    @staticmethod
    def workspace(project):
        if not project.workspace_path:
            raise ValueError("Рабочая папка не задана.")
        return Path(project.workspace_path).resolve(strict=True)

    async def notify(self, project_id, message, markup=None, event_type="log", config=None, *,
                     critical=False, in_team_chat=False):
        event = {"type": event_type, "project_id": project_id, "message": message}
        if config is not None:
            event.update(agent_id=config.id, agent_name=config.name)
        if event_type == "meeting":
            await self.message(project_id, "Loomis", "@All", "team_chat", message,
                               step_title=STEP_TITLES["meeting"])
            in_team_chat = True
        if event_type != "step_log":
            await manager.broadcast(project_id, event)
        if self.bot:
            async with async_session_factory() as session:
                owner = await session.scalar(select(User).join(
                    Project, Project.owner_id == User.id
                ).where(Project.id == project_id))
            if owner and owner.telegram_id and (owner.tg_logs_enabled or markup or critical):
                if in_team_chat and self.telegram_projects.get(owner.id) == project_id:
                    return
                locale = bot_language.set("ru")
                try:
                    localized = translate(message, owner.language)
                    for index, part in enumerate(telegram_html(localized)):
                        await self.bot.send_message(owner.telegram_id, part, parse_mode="HTML",
                                                    reply_markup=localize_markup(markup, owner.language) if index == 0 else None)
                except Exception:
                    logger.exception("Meeting or approval delivery failed for project %s", project_id)
                finally:
                    bot_language.reset(locale)

    async def request_approval(self, project_id, command, *, kind="command", config=None):
        if kind == "command":
            project, _ = await self.load(project_id)
            if project.autonomy_mode != "step_by_step":
                return False
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.approvals[key] = (project_id, future)
        try:
            await manager.broadcast(project_id, {
                "type": "approval", "project_id": project_id, "id": key, "command": command, "kind": kind,
                "agent_id": config.id if config else None, "agent_name": config.name if config else None,
            })
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Разрешить", callback_data=f"permit:{key}:1"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"permit:{key}:0"),
            ]])
            await self.notify(project_id, f"Требуется подтверждение\n{command}", markup)
            return await asyncio.wait_for(future, 90)
        except TimeoutError:
            return False
        finally:
            self.approvals.pop(key, None)
            await manager.broadcast(project_id, {"type": "approval_closed", "project_id": project_id, "id": key})

    def decide(self, project_id, key, approved):
        item = self.approvals.get(key)
        if item is None or item[0] != project_id or item[1].done():
            raise ValueError("Запрос подтверждения уже закрыт.")
        item[1].set_result(approved)

    async def cancel_gate(self, runtime):
        if runtime.gate and not runtime.gate.done():
            runtime.gate.cancel()
            await asyncio.gather(runtime.gate, return_exceptions=True)
        runtime.gate = None

    async def retry(self, project_id, agent_id):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            runtime.restore(project)
            config = next((item for item in configs if item.id == agent_id), None)
            saved = config.saved_context_json if config else None
            if not saved or config.traffic_state != "error" or project.status not in {"paused", "running", "error"}:
                raise ValueError("У агента нет прерванного шага для повтора.")
            digest = self.require_plan(project, configs, approved=True)
            if saved["plan_digest"] != digest or saved.get("plan_step", runtime.plan_step) != runtime.plan_step:
                raise ValueError("Прерванный шаг относится к другой версии плана.")
            if runtime.retry_agent is not None or runtime.pending_question is not None:
                raise ValueError("Сначала завершите текущий повтор или ответьте на вопрос агента.")
            if any(item.tested_at is None for item in configs):
                raise ValueError("Проверьте всех активных агентов.")
            await self.cancel_gate(runtime)
            runtime.pending_failover = None
            runtime.handoff = (agent_id, saved["handoff"])
            runtime.answer = saved["answer"]
            runtime.retry_agent = agent_id
            async with async_session_factory() as session:
                row = await session.get(AgentConfig, agent_id)
                row.cooldown_until = None
                row.traffic_state = "waiting"
                row.last_error = None
                project_row = await session.get(Project, project_id)
                project_row.status = "running"
                project_row.turn_state = runtime.snapshot()
                await session.commit()
            self.wake(project_id, runtime)
            await self.publish_traffic(row)
            await manager.publish_status(project_id, "running")
            return "running"

    async def action(self, project_id, action, *, feedback=None, target_agent_ids=None):
        if action == "regenerate_plan" and feedback is not None:
            await self.chat(project_id, feedback)
            project, _ = await self.load(project_id)
            return project.status
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            runtime.restore(project)
            transitions = {
                "confirm_completion": ("review_pending", "completed"),
                "reopen_project": ("completed", "review_pending"),
            }
            if action in transitions:
                source, target = transitions[action]
                if project.status == target:
                    return target
                if project.status != source:
                    raise ValueError("Действие недоступно в текущем статусе.")
                await self.status(project_id, target)
                return target
            if action in {"council", "regenerate_plan"} and project.status not in {"draft", "council"}:
                if not configs:
                    raise ValueError("Добавьте хотя бы одного агента.")
                selected = [item for item in self.select_agents(configs, target_agent_ids)
                            if item.agent_role_type != "image_gen"]
                if not selected:
                    raise ValueError("Для консилиума выберите хотя бы одну универсальную модель.")
                if feedback is not None:
                    if not project.plan_content:
                        raise ValueError("Сначала создайте план.")
                    target = " ".join(f"@{item.name}" for item in selected) if target_agent_ids is not None else "@All"
                    await self.message(project_id, "User", target, "team_chat", feedback)
                await self.plan_invalidated(project, publish=False)
                await self.status(project_id, "council", plan_digest=None, approved_plan_digest=None, turn_state=None)
                runtime.council = asyncio.create_task(self.plan(project_id, project, configs, selected, feedback, target_agent_ids is not None))
                return "council"
            if action == "approve" and project.status == "awaiting_approval":
                digest = self.require_plan(project, configs, approved=False)
                for config in configs:
                    if config.tested_at is None:
                        try:
                            tokens = await probe_agent(project_id, config)
                        except TimeoutError as exc:
                            seconds = 90 if config.agent_role_type == "image_gen" else 15
                            error = f"Таймаут проверки агента ({seconds} секунд)."
                            await self.traffic(config, "error", error)
                            raise ValueError(f"{config.name}: {error}") from exc
                        except Exception as exc:
                            await self.traffic(config, "error", str(exc))
                            raise ValueError(f"{config.name}: {exc}") from exc
                        async with async_session_factory() as session:
                            row = await session.get(AgentConfig, config.id)
                            row.tested_at = datetime.now(timezone.utc)
                            await count_tokens(session, project.owner_id, config, tokens)
                            await session.commit()
                        await self.traffic(config, "waiting")
                self.require_plan(project, configs, approved=False)
            elif action == "resume" and project.status == "paused":
                if project.autonomy_mode == "autonomous":
                    runtime.pending_question = None
                    await self.cancel_gate(runtime)
                elif runtime.pending_question is not None:
                    raise ValueError("Ответьте агенту в личном чате.")
                if runtime.gate and not runtime.gate.done():
                    raise ValueError("Подтвердите действие в чате.")
                digest = self.require_plan(project, configs, approved=True)
                if any(config.tested_at is None for config in configs):
                    raise ValueError("Проверьте всех активных агентов.")
            elif action == "pause" and project.status == "running":
                runtime.ready.clear()
                await self.status(project_id, "paused")
                return "paused"
            elif action == "rollback" and project.status in {"paused", "completed"}:
                if runtime.pending_failover or runtime.pending_question is not None or (
                    runtime.gate and not runtime.gate.done()
                ):
                    raise ValueError("Сначала завершите открытое обсуждение или подтверждение.")
                bridge = GitBridge(self.workspace(project), project_id)
                await bridge.prepare()
                await bridge.rollback_last_step()
                if project.commit_mode == "sequential":
                    await bridge.commit()
                runtime.meeting_base = None
                runtime.plan_step = max(0, runtime.plan_step - 1)
                runtime.handoff = None
                runtime.answer = ""
                if project.status == "completed":
                    await self.status(project_id, "paused", turn_state=runtime.snapshot())
                else:
                    await self.save_turn(project_id, runtime)
                return "paused"
            else:
                raise ValueError("Действие недоступно в текущем статусе.")
            if (runtime.pending_failover and project.failover_mode == "manual"
                    and project.autonomy_mode == "step_by_step"):
                return await self.pause_for_approval(
                    project, runtime, runtime.pending_failover, "failover"
                )
            runtime.pending_failover = None
            return await self.start_running(project_id, runtime, digest)

    async def start_running(self, project_id, runtime, digest):
        await self.status(project_id, "running", approved_plan_digest=digest, turn_state=runtime.snapshot())
        self.wake(project_id, runtime)
        return "running"

    def wake(self, project_id, runtime):
        runtime.ready.set()
        if runtime.task is None or runtime.task.done():
            runtime.task = asyncio.create_task(self.loop(project_id, runtime))

    async def pause_for_approval(self, project, runtime, prompt, kind, config=None, *, digest=None):
        runtime.ready.clear()
        updates = {"approved_plan_digest": digest} if digest is not None else {}
        await self.status(project.id, "paused", turn_state=runtime.snapshot(), **updates)
        runtime.gate = asyncio.create_task(self.confirm_step(project.id, runtime, prompt, kind, config))
        return "paused"

    async def confirm_step(self, project_id, runtime, prompt, kind, config):
        if not await self.request_approval(project_id, prompt, kind=kind, config=config):
            return
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            try:
                digest = self.require_plan(project, configs, approved=True)
                if any(item.tested_at is None for item in configs):
                    raise ValueError("Проверьте всех активных агентов.")
            except (ValueError, OSError) as exc:
                await self.notify(project_id, str(exc), critical=True)
                return
            runtime.pending_failover = None
            await self.start_running(project_id, runtime, digest)

    @staticmethod
    def select_agents(configs, target_agent_ids):
        if target_agent_ids is None:
            if not configs:
                raise ValueError("Добавьте хотя бы одного агента.")
            return configs
        ids = set(target_agent_ids)
        selected = [item for item in configs if item.id in ids]
        if not ids or len(selected) != len(ids):
            raise ValueError("Выберите доступных агентов этого проекта.")
        return selected

    async def plan(self, project_id, project, configs, selected, feedback, targeted):
        lead = None

        async def publish(agent, content, step_title):
            nonlocal lead
            if project.autonomy_mode == "autonomous" and "LOOMIS_QUESTION" in content:
                return
            config = agent.config
            lead = config.id
            await self.message(project_id, config.name, "@All", "team_chat", content, config,
                               mirror="direct" if targeted else None, step_title=step_title)
            await self.notify(project_id, f"{config.name}\n\n{content}", event_type="step_log", config=config,
                              in_team_chat=True)

        async def synthesize(agent, prompt, context, step_title):
            images = await self.spec_images(project_id)
            return await agent.run_with_spec_images("plan", prompt, context, images=images, step_title=step_title)

        async def rank_members(members):
            _, current = await self.load(project_id)
            by_id = {agent.config.id: (name, agent) for name, agent in members}
            now = datetime.now(timezone.utc)
            ranked = await rank_configs([
                item for item in current if item.id in by_id and outside_cooldown(item, now)
            ])
            return [by_id[item.id] for item in ranked]

        try:
            agents = [(item.name, ProjectAgent(self, project, item)) for item in selected]
            plan = await starting_council(
                self.workspace(project), agents, feedback, spec=project.spec_content or "",
                previous=project.plan_content or "", publish=publish,
                roster=[item.name for item in configs], synthesize=synthesize, rank=rank_members,
                artists=[item.name for item in configs if item.agent_role_type == "image_gen"],
                autonomy_mode=project.autonomy_mode,
            )
            project.plan_content = plan
            runtime = self.runtime(project_id)
            runtime.last_agent = lead or 0
            await self.status(project_id, "awaiting_approval", plan_content=plan, plan_digest=self.plan_digest(project),
                              turn_state=runtime.snapshot())
            if project.autonomy_mode == "autonomous":
                await self.action(project_id, "approve")
                return
            from backend.bot.keyboards import project_keyboard

            project.status = "awaiting_approval"
            await self.notify(project_id, plan, project_keyboard(project))
        except Exception as exc:
            await self.status(project_id, "planning")
            await self.notify(project_id, f"Ошибка консилиума. {exc}", critical=True)

    async def chat(self, project_id, question, target_agent_ids=None, *, media=None):
        runtime = self.runtime(project_id)
        auto_approve = False
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            if project.status in {"draft", "council"}:
                raise ValueError("Чат недоступен в текущем статусе.")
            runtime.restore(project)
            candidates = [item for item in configs if item.agent_role_type != "image_gen"]
            if not candidates:
                raise ValueError("Добавьте универсального агента для общего чата.")
            config = next((item for item in candidates if item.id == runtime.last_agent), candidates[0])
            await self.message(project_id, "User", f"@{config.name}", "team_chat", question, config, media=media,
                               step_title=STEP_TITLES["chat"])
            context = self.context(project)
            context += (
                "\n\nОтветь пользователю по существу максимально кратко. Обычный вопрос требует "
                "только ответа. Если сообщение меняет ТЗ, формат или задачи, после ответа добавь "
                "отдельную строку LOOMIS_PLAN, затем полный обновлённый план Markdown, затем строку "
                "LOOMIS_HANDOFF и краткую сводку для ближайшего исполнителя. "
                f"Сохрани первые {runtime.plan_step} уже выполненных шагов очереди буквально. "
                "Сохрани формат очереди и используй только доступных исполнителей: "
                + ", ".join(item.name for item in configs)
            )
            if project.autonomy_mode == "autonomous":
                context += "\nПри обсуждении проекта не запрашивай у пользователя решений о ходе работ."
            response = await self.ask(project, config, question, context, media, team_chat=True)
            reply, marker, update_text = response.message.replace("\r\n", "\n").partition("\nLOOMIS_PLAN\n")
            if marker and response.status == "success":
                revised, separator, summary = update_text.partition("\nLOOMIS_HANDOFF\n")
                revised, summary = revised.strip(), summary.strip()
                old_plan = project.plan_content or ""
                old_steps = scheduled_steps(old_plan)
                steps = scheduled_steps(revised)
                by_name = {item.name: item for item in configs}
                complete_prefix = runtime.plan_step
                try:
                    validate_steps(self.workspace(project), steps, by_name, {
                        item.name for item in configs if item.agent_role_type == "image_gen"
                    })
                    valid = True
                except (ValueError, OSError):
                    valid = False
                if (not separator or not revised or not summary or revised == old_plan.strip()
                        or ((old_steps or not old_plan) and not steps)
                        or (steps and (len(steps) <= complete_prefix
                                       or steps[:complete_prefix] != old_steps[:complete_prefix]))
                        or not valid):
                    reply = "Не удалось применить правки к плану. Сообщение сохранено в чате."
                else:
                    previously_approved = (
                        project.approved_plan_digest is not None
                        and project.approved_plan_digest == project.plan_digest == self.plan_digest(project)
                    )
                    project.plan_content = revised
                    digest = self.plan_digest(project)
                    runtime.answer = f"Уточнение пользователя: {question}\nСледующее действие: {summary}"
                    runtime.retry_agent = None
                    if steps:
                        runtime.handoff = (by_name[steps[complete_prefix][0]].id, "")
                    elif runtime.handoff is None:
                        runtime.handoff = (config.id, "")
                    status = project.status
                    reviewing = status in {"completed", "review_pending"}
                    if status == "planning" or (reviewing and not previously_approved):
                        status = "awaiting_approval"
                        auto_approve = project.autonomy_mode == "autonomous"
                    elif reviewing:
                        status = "paused"
                    resume = previously_approved and (
                        reviewing and project.autonomy_mode != "step_by_step"
                        or runtime.pending_question is not None
                    )
                    if runtime.pending_question is not None:
                        asking = next((item for item in configs if item.id == runtime.pending_question), None)
                        if asking:
                            await self.traffic(asking, "waiting")
                        runtime.pending_question = None
                    await self.status(project_id, status, plan_content=revised, plan_digest=digest,
                                      approved_plan_digest=digest if previously_approved else None,
                                      turn_state=runtime.snapshot())
                    if resume:
                        await self.start_running(project_id, runtime, digest)
            response.message = re.sub(r"(?m)^.*LOOMIS_QUESTION.*$", "", reply).strip()
            media_id = await self.message(project_id, config.name, "@User", "team_chat", response.message, config,
                                          step_title=STEP_TITLES["chat"])
            responses = [{"media_id": media_id, "agent_id": config.id, "status": response.status,
                          "message": response.message, "tokens": response.tokens}]
        if auto_approve:
            await self.action(project_id, "approve")
        return responses

    async def generate_image(self, project, config, prompt, path):
        await image_agent(config, self.workspace(project)).generate_image(prompt, path)
        return self.image_media(project, path)

    def image_media(self, project, path):
        target = workspace_file(self.workspace(project), path)
        preview = BytesIO()
        with Image.open(target) as image:
            thumbnail = image.convert("RGBA")
            thumbnail.thumbnail((480, 480))
            thumbnail.save(preview, format="PNG")
        return ProjectMedia(project_id=project.id, filename=path, mime_type="image/png", data=preview.getvalue())

    async def chat_image(self, project, config, prompt):
        try:
            async with asyncio.timeout(project.deadline_minutes * 60):
                path = f"assets/generated/{uuid4().hex}.png"
                media = await self.generate_image(project, config, prompt, path)
                return AgentResult("success", f"Изображение сохранено в `{path}`."), media
        except TimeoutError:
            return AgentResult("timeout", "Истёк дедлайн генерации изображения."), None
        except (OSError, ValueError, RuntimeError) as exc:
            return AgentResult("error", str(exc)), None

    async def ask(self, project, config, question, context, media=None, *, workspace=None, transport_ref=None,
                  team_chat=False):
        agent = ProjectAgent(self, project, config)
        operation = "chat" if team_chat else ("direct" if workspace is not None else "question")
        images = [(media.mime_type, media.data)] if media else ()
        result = await agent.run(operation, question, context, workspace=workspace, images=images,
                                 transport_ref=transport_ref)
        if media is None:
            return result
        if result.status != "unsupported_media" and not (
            result.status == "success" and refuses_media(result.message)
        ):
            return result
        audio = media.mime_type.startswith("audio/")
        async with async_session_factory() as session:
            agents = await session.scalars(select(AgentConfig).where(
                AgentConfig.project_id == project.id, AgentConfig.id != config.id,
                AgentConfig.enabled.is_(True), AgentConfig.traffic_state != "error",
            ).order_by(AgentConfig.id))
            presets = await session.scalars(select(UserModelPreset).where(
                UserModelPreset.owner_id == project.owner_id,
            ).order_by(UserModelPreset.id))
            names = [f"@{item.name}" for item in agents if item.agent_role_type != "image_gen"
                     and media_candidate(item.provider, item.model, audio)]
            names += [item.name for item in presets
                      if item.agent_role_type != "image_gen" and (item.adapter_type.lower(), item.config_json.get("model", "").lower()) !=
                      (config.provider.lower(), config.model.lower()) and
                      media_candidate(item.adapter_type, item.config_json.get("model", ""), audio)]
        kind = "аудио" if audio else "изображений"
        message = f"Эта модель не поддерживает анализ {kind}."
        if names:
            message += f" Рекомендую подключить или обратиться к {names[0]}."
        else:
            message += f" Рекомендую подключить модель с поддержкой {kind}."
        return AgentResult("unsupported_media", message, result.tokens)

    async def spec_images(self, project_id):
        async with async_session_factory() as session:
            rows = (await session.execute(select(ProjectMedia.mime_type, ProjectMedia.data).where(
                ProjectMedia.project_id == project_id, ProjectMedia.spec_item_id.is_not(None),
                ProjectMedia.mime_type.like("image/%"),
            ).order_by(ProjectMedia.id))).all()
        return [tuple(row) for row in rows]

    async def question(self, project_id, agent_id, question, *, media=None):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            if project.status == "council":
                raise ValueError("Чат недоступен в текущем статусе.")
            runtime.restore(project)
            config = next((item for item in configs if item.id == agent_id), None)
            if config is None:
                raise ValueError("Агент недоступен.")
            target = f"@{config.name}"
            await self.message(project_id, "User", target, "direct", question, config, media=media)
            context = self.context(project)
            was_running = runtime.ready.is_set()
            runtime.ready.clear()
            try:
                if config.agent_role_type == "image_gen":
                    result, generated = await self.chat_image(project, config, question)
                else:
                    workspace = self.workspace(project)
                    bridge = GitBridge(workspace, project.id)
                    await bridge.initialize()
                    await bridge.clean_project_metadata()
                    transport_ref = await bridge.open_transport() if config.settings.get("worker") else None
                    try:
                        result = await self.ask(project, config, question, context, media,
                                                workspace=workspace, transport_ref=transport_ref)
                        generated = None
                    finally:
                        if transport_ref:
                            await bridge.close_transport(transport_ref)
                        await bridge.clean_project_metadata()
                if runtime.pending_question == agent_id and media is None and result.status == "success":
                    runtime.answer += f"\nОтвет пользователя: {question}"
                    runtime.pending_question = None
                    await self.traffic(config, "waiting")
                    if project.autonomy_mode in {"with_questions", "consultative"}:
                        digest = self.require_plan(project, configs, approved=True)
                        await self.start_running(project_id, runtime, digest)
                    else:
                        await self.save_turn(project_id, runtime)
                media_id = await self.message(project_id, config.name, "@User", "direct", result.message, config,
                                   step_title=STEP_TITLES["question"], media=generated)
                return {"status": result.status, "message": result.message, "tokens": result.tokens, "media_id": media_id}
            finally:
                if was_running:
                    runtime.ready.set()

    @staticmethod
    def context(project):
        return "\n\n".join(f"# {title}\n{value}" for title, value in (
            ("Техническое задание", project.spec_content),
            ("План", project.plan_content),
            ("Контекст проекта", project.context_content),
        ) if value)

    @staticmethod
    def sole_deliverable(spec, steps):
        spec = spec or ""
        matches = re.findall(
            r"(?im)(?:единственн\w*|только\s+один|один)"
            r"\s+(?:(?:итогов|сдаточн)\w*\s+)?файл\s*[:—-]?\s*"
            r'(?:`([^`\r\n]+)`|«([^»\r\n]+)»|"([^"\r\n]+)"|([^\s,;:]+))',
            spec,
        )
        paths = {next(filter(None, match)).rstrip(".") for match in matches if any(match)}
        paths = {path for path in paths if Path(path).suffix}
        if paths:
            return next(iter(paths)) if len(paths) == 1 else None
        planned = {path for _, path, _ in steps if path != "-"}
        if len(planned) == 1:
            path = next(iter(planned))
            mentioned = re.search(rf"(?<![\w./\\-]){re.escape(path)}(?![\w/\\-]|\.\w)", spec, re.I)
            filenames = set(re.findall(r"(?<![\w.])[\w.-]+\.[a-zA-Z][\w-]*\b", spec))
            if mentioned and filenames == {Path(path).name}:
                return path
        return None

    async def loop(self, project_id, runtime):
        try:
            while True:
                await runtime.ready.wait()
                async with runtime.boundary:
                    if not runtime.ready.is_set():
                        continue
                    project, configs = await self.load(project_id)
                    self.require_plan(project, configs, approved=True)
                    if any(config.tested_at is None for config in configs):
                        raise ValueError("Проверьте всех активных агентов.")
                    await self.turn(project, configs, runtime)
                await asyncio.sleep(0.1)
        except Exception as exc:
            runtime.ready.clear()
            project, _ = await self.load(project_id)
            autonomous = project.autonomy_mode == "autonomous"
            await self.status(project_id, "error" if autonomous else "paused", turn_state=runtime.snapshot())
            report = f"{'Ошибка разработки' if autonomous else 'Разработка приостановлена'}. {exc}"
            await self.message(project_id, "Loomis", "@All", "team_chat", report)
            await self.notify(project_id, report, critical=True, in_team_chat=True)

    async def turn(self, project, configs, runtime):
        now = datetime.now(timezone.utc)
        available = await rank_configs([
            item for item in configs if item.traffic_state != "error" and outside_cooldown(item, now)
        ])
        workspace = self.workspace(project)
        steps = scheduled_steps(project.plan_content or "")
        planned = steps[runtime.plan_step] if steps else None
        compact = bool(planned and is_compact_plan(project.plan_content or ""))
        final_step = planned and runtime.plan_step == len(steps) - 1
        reviewing = bool(final_step and (
            (compact and runtime.plan_step == 1)
            or (
                re.match(r"(?i)^(?:финальная\s+)?(?:проверить|проверь|проверка|сверить|сверка|ревью)\b", planned[2])
                and not re.search(
                    r"(?i)\b(?:исправить|доработать|обновить|изменить|создать|добавить|удалить|поправить)\b",
                    planned[2],
                )
            )
            or re.search(r"(?i)\b(?:файлы\s+не\s+править|без\s+(?:правок|изменения)\s+файлов)\b", planned[2])
        ))
        expected_path = planned[1] if planned else "-"
        if reviewing and expected_path == "-" and runtime.plan_step > 0:
            expected_path = steps[runtime.plan_step - 1][1]
        mode = project.autonomy_mode
        if planned and runtime.handoff is None:
            intended = next(item for item in configs if item.name == planned[0])
            runtime.handoff = (intended.id, "")
        text_agents = [item for item in available if item.agent_role_type != "image_gen"]
        if runtime.handoff:
            intended = next((item for item in configs if item.id == runtime.handoff[0]), None)
            config = next((item for item in available if item.id == runtime.handoff[0]), None)
            if config is None and intended is not None and text_agents and intended.agent_role_type != "image_gen":
                await self.failover(project, text_agents, runtime, intended,
                                    "Принять следующий шаг по плану.", runtime.handoff[1],
                                    intended.last_error or "Агент недоступен.")
                return
        else:
            config = text_agents[0] if text_agents else None
        if config is None:
            runtime.ready.clear()
            await self.status(project.id, "error" if project.autonomy_mode == "autonomous" else "paused",
                              turn_state=runtime.snapshot())
            await self.notify(project.id, "Нет доступного исполнителя.", critical=True)
            return
        is_artist = config.agent_role_type == "image_gen"
        if is_artist and (not planned or planned[0] != config.name):
            raise ValueError(f"Для @{config.name} нет текущего шага генерации в плане.")
        bridge = GitBridge(workspace, project.id)
        await bridge.prepare()
        interval = project.meeting_interval_minutes * 60
        if runtime.next_meeting is None:
            runtime.next_meeting = monotonic() + interval
        if not compact and text_agents and runtime.retry_agent is None and monotonic() >= runtime.next_meeting:
            diff = await bridge.diff(runtime.meeting_base) if runtime.meeting_base else ""
            summary = await periodic_meeting(project.context_content or "", [
                (item.name, ProjectAgent(self, project, item)) for item in text_agents
            ], diff, config.name, autonomous=project.autonomy_mode == "autonomous")
            project.context_content = summary
            async with async_session_factory() as session:
                await session.execute(update(Project).where(Project.id == project.id).values(context_content=summary))
                await session.commit()
            await self.notify(project.id, summary, event_type="meeting")
            runtime.next_meeting = monotonic() + interval
            runtime.meeting_base = None
            _, current = await self.load(project.id)
            working_ids = {item.id for item in current if item.traffic_state != "error" and
                           outside_cooldown(item, datetime.now(timezone.utc))}
            available = [item for item in available if item.id in working_ids]
            if config.id not in working_ids:
                failed = next(item for item in current if item.id == config.id)
                task = planned[2] if planned else f"Выполни следующий шаг части плана, назначенной агенту {failed.name}."
                handoff = runtime.handoff[1] if runtime.handoff else ""
                error = failed.last_error or "Сбой на собрании."
                await self.traffic(failed, "error", error, saved_context={
                    "plan_digest": project.approved_plan_digest, "task": task, "handoff": handoff,
                    "context": self.context(project),
                    "answer": runtime.answer, "feedback": "", "diagnostic": error,
                    "partial_diff": "", "receipt_done": False, "plan_step": runtime.plan_step,
                })
                await self.failover(project, [item for item in text_agents if item.id in working_ids],
                                    runtime, failed, task, handoff, error)
                return
        remote = config.settings.get("worker")
        saved = config.saved_context_json if runtime.retry_agent == config.id else None
        checkpoint = (saved or {}).get("checkpoint") or await bridge.checkpoint()
        runtime.meeting_base = runtime.meeting_base or checkpoint
        clock = asyncio.get_running_loop()
        deadline = clock.time() + project.deadline_minutes * 60
        feedback = saved["feedback"] if saved else ""
        context = saved["context"] if saved else self.context(project)
        handoff = runtime.handoff[1] if runtime.handoff else ""
        task = saved["task"] if saved else (
            planned[2] if planned else f"Выполни следующий шаг части плана, назначенной агенту {config.name}."
        )
        retry_notes = f"\nПродолжи прерванный шаг.\n{saved['diagnostic']}\n{saved['partial_diff']}" if saved else ""
        receipt_done = saved["receipt_done"] if saved else False
        pending_result = saved.get("pending_result") if saved else None
        await self.traffic(config, "working")
        agent = ProjectAgent(self, project, config)
        clarification = ""
        images = () if is_artist or reviewing else await self.spec_images(project.id)
        generated = None
        confirm_handoff = handoff and not receipt_done and not is_artist and not compact and mode != "autonomous"
        stage = "подтверждение передачи задачи" if confirm_handoff else "исполнение задачи"
        recent = ""
        complete = False
        committed = False
        rejected_question = False
        question_allowed = mode in {"consultative", "with_questions"}
        try:
            async with asyncio.timeout_at(deadline):
                if confirm_handoff:
                    receipt = await agent.run(
                        "handoff", f"Ты {config.name}. Кратко сообщи команде, какую работу начнёшь. {task}",
                        f"{handoff}\n{runtime.answer}", timeout=max(0.01, deadline - clock.time()),
                    )
                    if receipt.status == "timeout":
                        raise TimeoutError(receipt.message)
                    if receipt.status != "success" or not receipt.message.strip():
                        raise RuntimeError(receipt.message.strip() or "Агент не подтвердил передачу задачи.")
                    await self.message(project.id, config.name, "@All", "team_chat",
                                       f"Принял передачу.\n{receipt.message}", config,
                                       step_title=STEP_TITLES["handoff"])
                    receipt_done = True
                while True:
                    stage = "исполнение задачи"
                    correction = f"Исправь ошибки проверки перед передачей хода:\n{feedback[:15000]}" if feedback else ""
                    if is_artist:
                        path = planned[1]
                        if pending_result is None:
                            generated = await self.generate_image(project, config, task, path)
                        else:
                            generated = self.image_media(project, path)
                        result = AgentResult("success", f"Изображение сохранено в `{path}`.")
                    elif pending_result is not None:
                        result = AgentResult("success", pending_result)
                    else:
                        transport_ref = await bridge.open_transport() if remote else None
                        try:
                            if reviewing:
                                acceptance = re.search(
                                    r"(?ims)^#{1,6}[ \t]+Критерии[^\n]*\n(.*?)(?=^#{1,6}[ \t]|\Z)",
                                    project.plan_content or "",
                                )
                                criteria = re.sub(r"(?m)^- \[loomis\].*\n?", "", acceptance[1]).strip() if acceptance else ""
                                criteria = criteria or project.spec_content or ""
                                paths = ", ".join(dict.fromkeys(path for _, path, _ in steps if path != "-"))
                                prompt = (
                                    f"Критерии приёмки:\n{task}\n{criteria}\n\n"
                                    f"Проверяемый результат: {paths or 'файлы проекта'}\n{handoff}"
                                )
                            else:
                                prompt = (
                                    f"{context}\n\nТы {config.name}. {task}\n"
                                    f"{handoff}\n{runtime.answer}\n{correction}{retry_notes}\n"
                                    "Отчёт: до трёх коротких пунктов — Сделано, Файлы, Следующий шаг. "
                                    "Используй утверждённый план из контекста и учитывай более свежие правки пользователя. "
                                    + ("Выполни только текущий шаг очереди. " if planned else "")
                                    + (f" Проверь итоговый результат в `{expected_path}`. " if expected_path != "-" else "")
                                    + autonomy_instruction(mode)
                                    + " В конце отчёта верни отдельную строку COMMIT_MSG по системной инструкции. "
                                    "Если из-за технической ошибки невозможно собрать требуемый результат, "
                                    "верни отдельную строку LOOMIS_BLOCKED: <причина>. "
                                    "Если все задачи плана завершены, верни отдельную строку LOOMIS_COMPLETE."
                                )
                            result = await agent.run_with_spec_images(
                                "execute", prompt,
                                workspace=workspace, timeout=max(0.01, deadline - clock.time()), images=images,
                                transport_ref=transport_ref, reviewing=reviewing,
                                step_title="Исправление ошибок проверки" if feedback else None,
                            )
                        finally:
                            if transport_ref:
                                await bridge.close_transport(transport_ref)
                            await bridge.clean_project_metadata()
                    recent = result.message
                    if result.status == "timeout":
                        raise TimeoutError(result.message)
                    if result.status != "success":
                        raise RuntimeError(result.message or result.status)
                    response = result.message.strip()
                    if not response:
                        raise RuntimeError("Исполнитель не вернул результат.")
                    blocked = re.search(r"(?m)^LOOMIS_BLOCKED:[ \t]*(.+)$", response)
                    if blocked:
                        raise RuntimeError(blocked[1])
                    if "LOOMIS_QUESTION" in response:
                        question = re.fullmatch(r"LOOMIS_QUESTION:[ \t]*(\S[^\r\n]*)", response)
                        if question_allowed and question:
                            clarification = question[1]
                            break
                        recent = ""
                        if rejected_question and mode != "autonomous":
                            raise RuntimeError("Исполнитель повторно нарушил протокол текущего режима.")
                        rejected_question = True
                        feedback = "Продолжи текущую задачу с разумным допущением и подготовь файлы."
                        pending_result = None
                        continue
                    self.require_plan(project, configs, approved=True)
                    stage = "проверка результата"
                    pending_result = result.message
                    if expected_path != "-" and not workspace_file(workspace, expected_path).is_file():
                        if reviewing:
                            raise RuntimeError(f"Итоговый файл {expected_path} отсутствует.")
                        feedback = f"Итоговый файл {expected_path} отсутствует. Создай его и проверь результат."
                    elif reviewing:
                        feedback = None
                    elif project.test_command and not is_artist and config.provider.lower() not in CLI_PROVIDERS:
                        output = await run_workspace_command(workspace, project.test_command)
                        feedback = None if output.startswith("Exit code 0\n") else output
                    else:
                        feedback = None if is_artist else await check_quality(
                            project.test_command, workspace, max(0.01, deadline - clock.time()),
                            None if mode != "step_by_step" else lambda command: self.request_approval(project.id, command),
                        )
                    if feedback is None:
                        stage = "сохранение результата"
                        await bridge.clean_project_metadata()
                        complete = (runtime.plan_step + 1 == len(steps) if planned else
                                    "LOOMIS_COMPLETE" in response.splitlines()
                                    or bool(re.search(r"(?mi)^Следующий шаг:[ \t]*Завершено[.!]?[ \t]*$", response)))
                        if complete:
                            deliverable = self.sole_deliverable(project.spec_content, steps)
                            committed = await bridge.commit(final=True, deliverable=deliverable)
                        elif project.commit_mode == "sequential":
                            subject = re.search(r"(?m)^COMMIT_MSG:[ \t]*([^\r\n]*)", response)
                            names = {name for item in configs for name in (item.name, item.model, item.provider)}
                            committed = await bridge.commit(subject[1] if subject else None, forbidden_names=names)
                        else:
                            await bridge.git("add", "-A")
                        changes = await bridge.git("diff", "--name-only", checkpoint, "--", ".")
                        break
                    pending_result = None
                    await self.message(project.id, config.name, "@All", "terminal", feedback, config,
                                       step_title="Результат проверки проекта")
        except (TimeoutError, OSError, ValueError, RuntimeError) as exc:
            error = str(exc) or f"Истёк дедлайн задачи ({project.deadline_minutes} мин.)."
            diagnostic, partial_diff = await self.diagnose_failure(
                bridge, checkpoint, config, stage, feedback or recent,
            )
            runtime.retry_agent = None
            await self.traffic(config, "error", error, saved_context={
                "plan_digest": project.approved_plan_digest, "task": task, "context": context,
                "handoff": handoff, "answer": runtime.answer, "feedback": feedback,
                "diagnostic": diagnostic, "partial_diff": partial_diff,
                "receipt_done": receipt_done, "pending_result": pending_result, "checkpoint": checkpoint,
                "plan_step": runtime.plan_step,
            })
            await self.failover(project, [] if is_artist else text_agents, runtime, config, task, handoff, error,
                                diagnostic=diagnostic, partial_diff=partial_diff)
            return
        runtime.retry_agent = None
        if clarification:
            await bridge.restore(checkpoint)
            await bridge.git("tag", "-d", checkpoint)
            if runtime.meeting_base == checkpoint:
                runtime.meeting_base = None
            runtime.handoff = (config.id, handoff)
            runtime.pending_question = config.id
            runtime.ready.clear()
            await self.status(project.id, "paused", turn_state=runtime.snapshot())
            await self.traffic(config, "asking", clear_context=True)
            await self.message(project.id, config.name, "@User", "direct", clarification, config)
            await self.notify(project.id, f"{config.name} спрашивает: {clarification}",
                              event_type="question", config=config, critical=True)
            return
        async with async_session_factory() as session:
            session.add(TaskLog(project_id=project.id, agent_id=config.id, sender_name=config.name,
                                operation="completed_step", duration_seconds=0, status="success"))
            await session.commit()
        runtime.answer = ""
        runtime.handoff = None
        runtime.last_agent = config.id
        if config.id in runtime.successful_agents:
            runtime.successful_agents.remove(config.id)
        runtime.successful_agents.append(config.id)
        await self.traffic(config, "waiting", clear_context=True)
        if planned:
            runtime.plan_step += 1
            next_config = None if complete else next(item for item in configs if item.name == steps[runtime.plan_step][0])
        elif complete:
            next_config = None
        else:
            ranked = await self.rank_successors(project.id, text_agents, config.id)
            next_config = next((item for item in ranked if item.id not in runtime.successful_agents),
                               ranked[0] if ranked else config)
        target = "@All" if complete else f"@{next_config.name}"
        visible_result = re.sub(r"(?m)^(?:COMMIT_MSG:.*|LOOMIS_(?:COMMIT:.*|COMPLETE))[ \t]*$", "", result.message)
        visible_result = re.sub(r"^(?:\s*Сделано:)+\s*", "", visible_result)
        report = (
            f"Сделано: {' '.join(visible_result.split())[:240]}\n"
            f"Файлы: {', '.join(changes.splitlines()[:6]) or 'Нет изменений'}\n"
            f"Следующий шаг: {'Завершено' if complete else target}"
        )
        await self.message(project.id, config.name, target, "team_chat", report, config,
                           step_title=STEP_TITLES["execute"], media=generated)
        if complete:
            runtime.ready.clear()
        else:
            runtime.handoff = (next_config.id, report)
        if committed:
            await self.save_commit_state(project, bridge, runtime, complete)
            if complete:
                await manager.publish_status(project.id, "review_pending")
            await manager.broadcast(project.id, {"type": "git_commit", "project_id": project.id})
        await self.notify(project.id, report, event_type="step_log", config=config, in_team_chat=True)
        if committed:
            if project.auto_push_mode == "after_each":
                await self.publish_github_step(project, bridge, complete)
        if complete:
            if not committed:
                await self.status(project.id, "review_pending", turn_state=runtime.snapshot())
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Подтвердить завершение",
                                     callback_data=f"project_action:{project.id}:confirm_completion"),
                InlineKeyboardButton(text="Продолжить разработку", callback_data=f"review_continue:{project.id}"),
            ]])
            await self.notify(project.id, "Результат готов к проверке.", markup, event_type="review")
            return
        if project.autonomy_mode == "step_by_step":
            runtime.ready.clear()
            await self.status(project.id, "paused", turn_state=runtime.snapshot())
        else:
            await self.save_turn(project.id, runtime)

    async def history_bounds(self, session, project_id):
        return {
            model.__tablename__: await session.scalar(select(func.coalesce(func.max(model.id), 0))
                                                       .where(model.project_id == project_id))
            for model in (AgentMessage, TaskLog)
        }

    async def save_commit_state(self, project, bridge, runtime, complete):
        head = await bridge.head()
        async with async_session_factory() as session:
            agents = await session.scalars(select(AgentConfig).where(AgentConfig.project_id == project.id))
            state = {
                "turn": runtime.snapshot(), "status": "review_pending" if complete else "paused",
                "plan_digest": project.plan_digest, "approved_plan_digest": project.approved_plan_digest,
                "history_state": project.history_state, "bounds": await self.history_bounds(session, project.id),
                "agents": {str(agent.id): {
                    "created_at": agent.created_at.isoformat(),
                    "traffic_state": agent.traffic_state, "last_error": agent.last_error,
                    "saved_context_json": agent.saved_context_json,
                    "cooldown_until": agent.cooldown_until.isoformat() if agent.cooldown_until else None,
                } for agent in agents},
            }
            row = await session.get(Project, project.id)
            row.turn_state = state["turn"]
            if complete:
                row.status = "review_pending"
            await session.merge(CommitState(project_id=project.id, commit_hash=head, state=state))
            await session.commit()

    async def commits(self, project_id):
        project, _ = await self.load(project_id)
        return await GitBridge(self.workspace(project), project.id).commits()

    async def rollback_commit(self, project_id, commit_hash):
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_hash):
            raise ValueError("Некорректный хэш коммита.")
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, _ = await self.load(project_id)
            if project.status in {"running", "council"}:
                raise ValueError("Сначала приостановите проект.")
            bridge = GitBridge(self.workspace(project), project.id)
            matches = [entry for entry in await bridge.commits() if entry["hash"].startswith(commit_hash.lower())]
            if len(matches) != 1:
                raise ValueError("Коммит отсутствует в истории проекта или хэш неоднозначен.")
            entry = matches[0]
            target = entry["hash"]
            await self.cancel_gate(runtime)
            runtime.ready.clear()
            async with async_session_factory() as session:
                row = await session.get(Project, project.id)
                snapshot = await session.get(CommitState, (project.id, target))
                agents = list(await session.scalars(select(AgentConfig).where(AgentConfig.project_id == project.id)))
                bounds = await self.history_bounds(session, project.id)
                if snapshot is None:
                    # Imported commits predate Loomis snapshots. Recover history using the Git timestamp.
                    state = {"bounds": {}, "agents": {}}
                    for model, date in ((AgentMessage, AgentMessage.created_at), (TaskLog, TaskLog.started_at)):
                        state["bounds"][model.__tablename__] = await session.scalar(
                            select(func.coalesce(func.max(model.id), 0)).where(
                                model.project_id == project.id,
                                date <= entry["created_at"].astimezone(timezone.utc),
                            )
                        )
                else:
                    state = snapshot.state
                row.history_state = {
                    name: merge_ranges((state.get("history_state") or {}).get(name, []),
                                       state["bounds"][name] + 1, end)
                    for name, end in bounds.items()
                }
                row.turn_state = state.get("turn")
                row.plan_digest = state.get("plan_digest")
                row.approved_plan_digest = state.get("approved_plan_digest")
                row.status = state.get("status") or ("planning" if project.status != "draft" else "draft")
                if row.plan_digest and row.plan_digest != self.plan_digest(row):
                    row.plan_digest = row.approved_plan_digest = None
                    row.turn_state = None
                    row.status = "planning"
                for agent in agents:
                    saved = state["agents"].get(str(agent.id), {})
                    if saved.get("created_at") != agent.created_at.isoformat():
                        saved = {}
                    agent.traffic_state = saved.get("traffic_state", "waiting")
                    agent.last_error = saved.get("last_error")
                    agent.saved_context_json = saved.get("saved_context_json")
                    until = saved.get("cooldown_until")
                    agent.cooldown_until = datetime.fromisoformat(until) if until else None
                notification = AgentMessage(project_id=project.id, sender_name="Loomis", target_name="@All",
                                            channel="team_chat", content=f"Проект откачен к коммиту {target[:12]}.",
                                            step_title="Git")
                session.add(notification)
                await session.flush()
                previous_head = await bridge.head()
                workspace_state = await bridge.snapshot()
                try:
                    await bridge.reset(target)
                    await session.commit()
                except (OSError, RuntimeError, TimeoutError, SQLAlchemyError, asyncio.CancelledError):
                    await bridge.reset(previous_head)
                    await bridge.restore(workspace_state)
                    raise
            fresh = Runtime()
            fresh.restore(row)
            runtime.last_agent = fresh.last_agent
            runtime.handoff = fresh.handoff
            runtime.successful_agents = fresh.successful_agents
            runtime.pending_question = fresh.pending_question
            runtime.pending_failover = fresh.pending_failover
            runtime.retry_agent = fresh.retry_agent
            runtime.plan_step = fresh.plan_step
            runtime.answer = fresh.answer
            runtime.restored = True
            runtime.meeting_base = None
            runtime.next_meeting = None
            await manager.broadcast(project_id, {"type": "history_changed", "project_id": project_id})
            await manager.broadcast(project_id, message_event(notification, None))
            await manager.publish_status(project_id, row.status)
            for agent in agents:
                await self.publish_traffic(agent)
            return {"status": row.status, "commit_hash": target, "context_restored": snapshot is not None}

    async def push_project(self, project_id):
        async with self.runtime(project_id).boundary:
            project, _ = await self.load(project_id)
            if project.status in {"running", "council"}:
                raise ValueError("Сначала приостановите проект.")
            finish_only = project.auto_push_mode == "manual_finish" or project.commit_mode == "single_preset"
            if finish_only and project.status != "completed":
                raise ValueError("Сначала завершите проект.")
            bridge = GitBridge(self.workspace(project), project.id)
            await bridge.clean_project_metadata()
            await bridge.commit_metadata_cleanup()
            await self.push_linked_repo(project, bridge)
            await self.message(project_id, "Loomis", "@All", "team_chat", "Git · коммит отправлен.",
                               step_title="Git")

    async def push_linked_repo(self, project, bridge):
        if not project.git_repo_url:
            raise ValueError("Привяжите удалённый репозиторий.")
        if not await bridge.head():
            raise ValueError("В проекте пока нет коммитов.")
        if not is_github_repo(project.git_repo_url):
            await bridge.push_main(project.git_repo_url)
            return
        async with async_session_factory() as session:
            encrypted_token = await session.scalar(select(User.github_token).where(User.id == project.owner_id))
        if not encrypted_token:
            raise ValueError("Подключите GitHub.")
        await bridge.push_github_main(project.git_repo_url, github_token(encrypted_token))

    async def publish_github_step(self, project, bridge, complete):
        if not project.git_repo_url:
            return
        try:
            await bridge.clean_project_metadata()
            await bridge.commit_metadata_cleanup()
            await self.push_linked_repo(project, bridge)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            status = f"Git · ошибка отправки в main. {exc}"
        else:
            status = "Git · итоговый коммит отправлен в main." if complete else "Git · коммит шага отправлен в main."
        await self.message(project.id, "Loomis", "@All", "team_chat", status, step_title="Git")
        await self.notify(project.id, status, event_type="step_log", critical=True, in_team_chat=True)

    async def diagnose_failure(self, bridge, checkpoint, config, stage, last_output):
        notes = [f"Этап: {stage}."]
        diff = ""
        try:
            status = await bridge.git("status", "--short", "--untracked-files=all")
            changes = await bridge.git("diff", "--stat", checkpoint, "--", ".")
            diff = await bridge.diff(checkpoint)
            notes.append(f"Изменения в репозитории:\n{(changes or status or 'Нет изменений')[:900]}")
            if status and status != changes:
                notes.append(status[:500])
        except (RuntimeError, OSError, TimeoutError):
            notes.append("Не удалось собрать полный git diff.")
        if not last_output:
            async with async_session_factory() as session:
                last_output = await session.scalar(select(AgentMessage.content).where(
                    AgentMessage.project_id == config.project_id, AgentMessage.agent_id == config.id,
                    AgentMessage.channel == "terminal",
                ).order_by(AgentMessage.id.desc()).limit(1))
        if last_output:
            notes.append(f"Последний вывод: {last_output.strip()[-600:]}")
        return "\n".join(notes), diff[:20000]

    async def rank_successors(self, project_id, available, excluded_id):
        _, current = await self.load(project_id)
        now = datetime.now(timezone.utc)
        eligible = {
            item.id: item for item in current if item.id != excluded_id
            and item.traffic_state != "error" and outside_cooldown(item, now)
        }
        return await rank_configs([eligible[item.id] for item in available if item.id in eligible])

    async def failover(self, project, available, runtime, broken, task, handoff, error, *,
                       diagnostic="", partial_diff=""):
        candidates = [item for item in available if item.id != broken.id]
        if candidates:
            plan = project.plan_content or ""
            terms = set(re.findall(r"[\w.-]{4,}", f"{error}\n{diagnostic}".lower()))
            sections = re.split(r"(?m)(?=^#{1,6}\s)", plan.lower())
            lines = plan.lower().splitlines()

            def relevance(item):
                name = re.compile(rf"(?<!\w){re.escape(item.name.lower())}(?!\w)")
                role = " ".join([section for section in sections if name.search(section.partition("\n")[0])]
                                + [line for line in lines if name.search(line)])
                words = set(re.findall(r"[\w.-]{4,}", role))
                return len(terms & words), item.id in runtime.successful_agents

            ranked = await self.rank_successors(
                project.id, sorted(candidates, key=relevance, reverse=True), broken.id,
            )
            successor = ranked[0] if ranked else None
        else:
            successor = None
        runtime.last_agent = broken.id
        instruction = (
            f"Агент {broken.name} выбыл из строя ({error}). Проанализируй текущее состояние "
            "файлов в репозитории, доделай его незаконченную часть задачи по плану и затем выполни свою часть. "
            f"Незавершённая задача: {task}"
        )
        if successor is None:
            runtime.handoff = (broken.id, f"{instruction}\n{diagnostic}\n{handoff}\n{partial_diff}")
            runtime.ready.clear()
            await self.status(project.id, "error" if project.autonomy_mode == "autonomous" else "paused",
                              turn_state=runtime.snapshot())
            report = f"{broken.name}: {error}\n{diagnostic}\nНет доступного агента для передачи задачи."
            await self.message(project.id, broken.name, "@All", "team_chat", report, broken)
            await self.notify(project.id, report, event_type="step_log", config=broken, critical=True,
                              in_team_chat=True)
            return
        report = (
            f"Передача эстафеты @{successor.name}\n{instruction}\n{diagnostic}"
        )
        context = f"{report}\n\nИсходный контекст передачи:\n{handoff}"
        if partial_diff:
            context += f"\n\nGit diff на момент сбоя:\n{partial_diff}"
        runtime.handoff = (successor.id, context)
        if project.failover_mode == "manual" and project.autonomy_mode == "step_by_step":
            runtime.pending_failover = f"{broken.name}: {error}\nПередать задачу @{successor.name}?"
            await self.pause_for_approval(project, runtime, runtime.pending_failover, "failover", broken)
        else:
            await self.save_turn(project.id, runtime)
        await self.message(project.id, broken.name, f"@{successor.name}", "team_chat", report, broken)
        await self.notify(project.id, report, event_type="step_log", config=broken, critical=True,
                          in_team_chat=True)

    async def stop(self, project_id):
        runtime = self.runtimes.pop(project_id, None)
        if runtime:
            tasks = [task for task in (runtime.task, runtime.council, runtime.gate) if task and not task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def background_reply(self, reply):
        task = asyncio.create_task(reply)
        self.replies.add(task)

        def finished(task):
            self.replies.discard(task)
            if not task.cancelled() and task.exception():
                logger.error("Telegram reply failed", exc_info=task.exception())

        task.add_done_callback(finished)

    async def shutdown(self):
        replies = tuple(self.replies)
        for task in replies:
            task.cancel()
        await asyncio.gather(*replies, return_exceptions=True)
        await asyncio.gather(*(self.stop(key) for key in tuple(self.runtimes)))


orchestrator = Orchestrator()
