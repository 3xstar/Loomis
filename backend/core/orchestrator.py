import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from time import monotonic
from uuid import uuid4

from sqlalchemy import func, or_, select, true, update
from sqlalchemy.exc import SQLAlchemyError
from aiogram.exceptions import TelegramRetryAfter

from backend.api.ws import manager, workers
from backend.bot.formatting import telegram_html
from backend.database.db import async_session_factory
from backend.database.models import AgentConfig, AgentMessage, CommitState, Project, ProjectMedia, TaskLog, User, UserModelPreset
from worker.adapters import CLI_PROVIDERS, build_agent
from worker.adapters.api_adapter import ApiAgent, api_system_prompt
from worker.adapters.base import AgentResult, BaseAgent
from worker.adapters.media import refuses_media
from worker.git_bridge import GitBridge, is_github_repo
from worker.quality_gate import check_quality
from worker.sandbox import workspace_file

from .council import periodic_meeting, starting_council
from .github import github_token
from .model_performance import cache_response, cached_response, model_key, prompt_digest, rank_configs, record_model_result

logger = logging.getLogger(__name__)

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
    "plan": "Формирование файла PLAN.md",
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
        self.restored = True

    def snapshot(self):
        return {
            "last_agent": self.last_agent, "handoff": self.handoff,
            "successful_agents": self.successful_agents, "pending_question": self.pending_question,
            "pending_failover": self.pending_failover, "answer": self.answer, "retry_agent": self.retry_agent,
        }


async def probe_agent(project_id, config):
    prompt = "Ответь одним словом: OK"
    started = monotonic()
    success = False
    cancelled = False
    try:
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
    "concise": "Отвечай максимально сжато, только факты, код и итог, без воды, до 5–7 строк.",
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

    async def run(self, operation, prompt, context="", workspace=None, timeout=None, images=(), *, step_title=None, transport_ref=None):
        project, config = self.project, self.config
        instruction = VERBOSITY[project.verbosity_level or "concise"]
        if operation == "plan":
            instruction += " Для PLAN.md верни полный документ со всеми задачами."
        history = await self.engine.prompt_history(project, config)
        if history:
            history = f"\n\nИстория обсуждений:\n{history}"
            if operation == "execute":
                prompt += history
            else:
                context += history
        manage_traffic = operation in {"question", "meeting"} and config.saved_context_json is None
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
                                        lambda command: self.engine.request_approval(project.id, command),
                                        instruction=instruction, images=images)
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
                        answer = await cached_response(digest)
                if answer is not None:
                    result = AgentResult("success", answer)
                else:
                    called = True
                    call_clock = monotonic()
                    if node:
                        worker_operation = "question" if operation in {"handoff", "plan"} else operation
                        result = await workers.dispatch(node, worker_agent,
                                                        project.id, worker_operation, prompt, context, timeout,
                                                        instruction=instruction, images=images,
                                                        on_interrupted=record_interruption, transport_ref=transport_ref)
                    elif operation == "execute":
                        result = await agent.execute_task(prompt, workspace, timeout)
                    elif operation == "meeting":
                        result = await agent.participate_in_meeting(context)
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
            await self.engine.message(
                project.id, config.name, "@All", "terminal", result.terminal or result.message, config,
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
                rows[0].media_id = media.id
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
                self.background_reply(self.send_team_chat(project_id, owner.id, owner.telegram_id, sender, target, content))

    async def prompt_history(self, project, config):
        async with async_session_factory() as session:
            rows = list(await session.scalars(select(AgentMessage).where(
                AgentMessage.project_id == project.id, history_visible(project, AgentMessage),
                or_(AgentMessage.channel == "team_chat",
                    (AgentMessage.channel == "direct") & (AgentMessage.agent_id == config.id)),
            ).order_by(AgentMessage.id.desc()).limit(40)))
        return "\n".join(f"{row.sender_name} → {row.target_name}: {row.content[-4000:]}"
                         for row in reversed(rows))[-16000:]

    async def send_team_chat(self, project_id, owner_id, chat_id, sender, target, content):
        heading = escape(f"{sender} → {target}"[:200])
        async with self.runtime(project_id).telegram_delivery:
            for part in telegram_html(content or "—"):
                while self.telegram_projects.get(owner_id) == project_id:
                    try:
                        await self.bot.send_message(chat_id, f"<b>{heading}</b>\n{part}", parse_mode="HTML")
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
        runtime.restored = True
        if runtime.gate and not runtime.gate.done():
            runtime.gate.cancel()
            await asyncio.gather(runtime.gate, return_exceptions=True)
        if publish:
            await manager.publish_status(project.id, project.status)

    def plan_digest(self, project):
        return sha256(workspace_file(self.workspace(project), "PLAN.md").read_bytes()).hexdigest()

    def require_plan(self, project, configs, *, approved):
        if not configs:
            raise ValueError("Добавьте хотя бы одного агента.")
        expected = project.approved_plan_digest if approved else project.plan_digest
        if not expected or expected != project.plan_digest or self.plan_digest(project) != expected:
            raise ValueError("Создайте и утвердите актуальный PLAN.md.")
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
                try:
                    for index, part in enumerate(telegram_html(message)):
                        await self.bot.send_message(owner.telegram_id, part, parse_mode="HTML",
                                                    reply_markup=markup if index == 0 else None)
                except Exception:
                    logger.exception("Meeting or approval delivery failed for project %s", project_id)

    async def request_approval(self, project_id, command, *, kind="command", config=None):
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
            if not saved or config.traffic_state != "error" or project.status not in {"paused", "running"}:
                raise ValueError("У агента нет прерванного шага для повтора.")
            digest = self.require_plan(project, configs, approved=True)
            if saved["plan_digest"] != digest:
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
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            runtime.restore(project)
            if action in {"council", "regenerate_plan"} and project.status not in {"draft", "council"}:
                if not configs:
                    raise ValueError("Добавьте хотя бы одного агента.")
                selected = self.select_agents(configs, target_agent_ids)
                workspace = self.workspace(project)
                if feedback is not None:
                    if not workspace_file(workspace, "PLAN.md").is_file():
                        raise ValueError("Сначала создайте PLAN.md.")
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
                            error = "Таймаут проверки агента (15 секунд)."
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
                if runtime.pending_question is not None:
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
                    await bridge.commit("wip: rollback")
                runtime.meeting_base = None
                runtime.handoff = None
                runtime.answer = ""
                if project.status == "completed":
                    await self.status(project_id, "paused", turn_state=runtime.snapshot())
                else:
                    await self.save_turn(project_id, runtime)
                return "paused"
            else:
                raise ValueError("Действие недоступно в текущем статусе.")
            if runtime.pending_failover and project.failover_mode == "manual":
                return await self.pause_for_approval(
                    project, runtime, runtime.pending_failover, "failover"
                )
            runtime.pending_failover = None
            if project.autonomy_mode == "step_by_step":
                return await self.pause_for_approval(
                    project, runtime, "Выполнить следующий шаг плана?", "step", digest=digest
                )
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
        async def publish(agent, content, step_title):
            config = agent.config
            await self.message(project_id, config.name, "@All", "team_chat", content, config,
                               mirror="direct" if targeted else None, step_title=step_title)
            await self.notify(project_id, f"{config.name}\n\n{content}", event_type="step_log", config=config,
                              in_team_chat=True)

        async def ask(agent, prompt, context, step_title):
            return await agent.ask_question(prompt, context, step_title=step_title)

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
                self.workspace(project), agents, feedback, publish=publish,
                roster=[item.name for item in configs], synthesize=synthesize, rank=rank_members, ask=ask,
            )
            await self.status(project_id, "awaiting_approval", plan_digest=self.plan_digest(project))
            from backend.bot.keyboards import project_keyboard

            project.status = "awaiting_approval"
            await self.notify(project_id, plan, project_keyboard(project))
        except Exception as exc:
            await self.status(project_id, "planning")
            await self.notify(project_id, f"Ошибка консилиума. {exc}", critical=True)

    async def chat(self, project_id, question, target_agent_ids=None, *, media=None):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            if project.status in {"draft", "council"}:
                raise ValueError("Чат недоступен в текущем статусе.")
            selected = self.select_agents(configs, target_agent_ids)
            target = " ".join(f"@{item.name}" for item in selected) if target_agent_ids is not None else "@All"
            addressed = selected[0] if target_agent_ids is not None and len(selected) == 1 else None
            await self.message(project_id, "User", target, "team_chat", question, addressed, media=media)
            context = self.context(self.workspace(project), ("SPEC.md", "PLAN.md", "Project_context.md"))
            async def respond(config):
                response = await self.ask(project, config, question, context, media)
                await self.message(project_id, config.name, "@User", "team_chat", response.message, config,
                                   mirror="direct" if target_agent_ids is not None else None,
                                   step_title=STEP_TITLES["question"])
                return {"agent_id": config.id, "status": response.status,
                        "message": response.message, "tokens": response.tokens}

            return await asyncio.gather(*(respond(config) for config in selected))

    async def ask(self, project, config, question, context, media=None):
        agent = ProjectAgent(self, project, config)
        if media is None:
            return await agent.ask_question(question, context)
        result = await agent.run("question", question, context, images=[(media.mime_type, media.data)])
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
            names = [f"@{item.name}" for item in agents if media_candidate(item.provider, item.model, audio)]
            names += [item.name for item in presets
                      if (item.adapter_type.lower(), item.config_json.get("model", "").lower()) !=
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

    async def question(self, project_id, agent_id, question, *, media=None, mirror=False):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            if project.status in {"draft", "council"}:
                raise ValueError("Чат недоступен в текущем статусе.")
            runtime.restore(project)
            config = next((item for item in configs if item.id == agent_id), None)
            if config is None:
                raise ValueError("Агент недоступен.")
            target = f"@{config.name}"
            if runtime.pending_question == agent_id and media is None:
                digest = self.require_plan(project, configs, approved=True)
                await self.message(project_id, "User", target, "direct", question, config)
                runtime.answer += f"\nОтвет пользователя: {question}"
                runtime.pending_question = None
                await self.traffic(config, "waiting")
                if project.autonomy_mode == "step_by_step":
                    await self.pause_for_approval(project, runtime, "Продолжить шаг с ответом пользователя?", "step", config)
                else:
                    await self.start_running(project_id, runtime, digest)
                return {"status": "success", "message": "Ответ передан агенту.", "tokens": 0}
            await self.message(project_id, "User", target, "direct", question, config, media=media)
            context = self.context(self.workspace(project), ("SPEC.md", "PLAN.md", "Project_context.md"))
            was_running = runtime.ready.is_set()
            runtime.ready.clear()
            try:
                result = await self.ask(project, config, question, context, media)
                await self.message(project_id, config.name, "@User", "direct", result.message, config,
                                   mirror="team_chat" if mirror else None, step_title=STEP_TITLES["question"])
                return {"status": result.status, "message": result.message, "tokens": result.tokens}
            finally:
                if was_running:
                    runtime.ready.set()

    @staticmethod
    def context(workspace, names):
        parts = []
        for name in names:
            path = workspace_file(workspace, name)
            if path.is_file():
                parts.append(f"# {name}\n{path.read_text(encoding='utf-8')[:50000]}")
        return "\n\n".join(parts)

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
            await self.status(project_id, "paused", turn_state=runtime.snapshot())
            await self.message(project_id, "Loomis", "@All", "team_chat", f"Разработка приостановлена. {exc}")
            await self.notify(project_id, f"Разработка приостановлена. {exc}", critical=True, in_team_chat=True)

    async def turn(self, project, configs, runtime):
        now = datetime.now(timezone.utc)
        available = await rank_configs([
            item for item in configs if item.traffic_state != "error" and outside_cooldown(item, now)
        ])
        if runtime.handoff:
            intended = next((item for item in configs if item.id == runtime.handoff[0]), None)
            config = next((item for item in available if item.id == runtime.handoff[0]), None)
            if config is None and intended is not None and available:
                await self.failover(project, available, runtime, intended,
                                    "Принять следующий шаг по PLAN.md.", runtime.handoff[1],
                                    intended.last_error or "Агент недоступен.")
                return
        else:
            config = available[0] if available else None
        if config is None:
            runtime.ready.clear()
            await self.status(project.id, "paused", turn_state=runtime.snapshot())
            await self.notify(project.id, "Нет доступного исполнителя. Проверьте агентов перед возобновлением.", critical=True)
            return
        workspace = self.workspace(project)
        bridge = GitBridge(workspace, project.id)
        await bridge.prepare()
        interval = project.meeting_interval_minutes * 60
        if runtime.next_meeting is None:
            runtime.next_meeting = monotonic() + interval
        if runtime.retry_agent is None and monotonic() >= runtime.next_meeting:
            diff = await bridge.diff(runtime.meeting_base) if runtime.meeting_base else ""
            summary = await periodic_meeting(workspace, [
                (item.name, ProjectAgent(self, project, item)) for item in available
            ], diff, config.name)
            await self.notify(project.id, summary, event_type="meeting")
            runtime.next_meeting = monotonic() + interval
            runtime.meeting_base = None
            _, current = await self.load(project.id)
            working_ids = {item.id for item in current if item.traffic_state != "error" and
                           outside_cooldown(item, datetime.now(timezone.utc))}
            available = [item for item in available if item.id in working_ids]
            if config.id not in working_ids:
                failed = next(item for item in current if item.id == config.id)
                task = f"Выполни следующий шаг части плана, назначенной агенту {failed.name}."
                handoff = runtime.handoff[1] if runtime.handoff else ""
                error = failed.last_error or "Сбой на собрании."
                await self.traffic(failed, "error", error, saved_context={
                    "plan_digest": project.approved_plan_digest, "task": task, "handoff": handoff,
                    "context": self.context(workspace, ("SPEC.md", "PLAN.md", "Project_context.md")),
                    "answer": runtime.answer, "feedback": "", "diagnostic": error,
                    "partial_diff": "", "receipt_done": False,
                })
                await self.failover(project, available, runtime, failed, task, handoff, error)
                return
        remote = config.settings.get("worker")
        saved = config.saved_context_json if runtime.retry_agent == config.id else None
        checkpoint = (saved or {}).get("checkpoint") or await bridge.checkpoint()
        runtime.meeting_base = runtime.meeting_base or checkpoint
        clock = asyncio.get_running_loop()
        deadline = clock.time() + project.deadline_minutes * 60
        feedback = saved["feedback"] if saved else ""
        context = saved["context"] if saved else self.context(workspace, ("SPEC.md", "PLAN.md", "Project_context.md"))
        handoff = runtime.handoff[1] if runtime.handoff else ""
        task = saved["task"] if saved else f"Выполни следующий шаг части плана, назначенной агенту {config.name}."
        retry_notes = f"\nПродолжи прерванный шаг.\n{saved['diagnostic']}\n{saved['partial_diff']}" if saved else ""
        receipt_done = saved["receipt_done"] if saved else False
        pending_result = saved.get("pending_result") if saved else None
        await self.traffic(config, "working")
        agent = ProjectAgent(self, project, config)
        clarification = ""
        images = await self.spec_images(project.id)
        stage = "подтверждение передачи задачи" if handoff and not receipt_done else "исполнение задачи"
        recent = ""
        complete = False
        committed = False
        try:
            async with asyncio.timeout_at(deadline):
                if handoff and not receipt_done:
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
                    if pending_result is not None:
                        result = AgentResult("success", pending_result)
                    else:
                        transport_ref = await bridge.open_transport() if remote else None
                        try:
                            result = await agent.run_with_spec_images(
                                "execute", f"{context}\n\nТы {config.name}. {task}\n"
                                f"{handoff}\n{runtime.answer}\n{correction}{retry_notes}\n"
                                "В итоговом отчёте опиши результат и инструкции следующему исполнителю. "
                                "Сохраняй утверждённый PLAN.md. "
                                + ("При неоднозначных требованиях также используй LOOMIS_QUESTION. "
                                   if project.autonomy_mode == "consultative" else "")
                                + "Если данных не хватает для продолжения, верни только LOOMIS_QUESTION: <вопрос>. "
                                "Если все задачи плана завершены, верни отдельную строку LOOMIS_COMPLETE.",
                                workspace=workspace, timeout=max(0.01, deadline - clock.time()), images=images,
                                transport_ref=transport_ref, step_title="Исправление ошибок проверки" if feedback else None,
                            )
                        finally:
                            if transport_ref:
                                await bridge.close_transport(transport_ref)
                    recent = result.message
                    if result.status == "timeout":
                        raise TimeoutError(result.message)
                    if result.status != "success":
                        raise RuntimeError(result.message or result.status)
                    response = result.message.strip()
                    if response.startswith("LOOMIS_QUESTION:"):
                        clarification = response.removeprefix("LOOMIS_QUESTION:").strip()
                        if clarification:
                            break
                    self.require_plan(project, configs, approved=True)
                    stage = "проверка результата"
                    pending_result = result.message
                    feedback = await check_quality(
                        project.test_command, workspace, max(0.01, deadline - clock.time()),
                        lambda command: self.request_approval(project.id, command),
                    )
                    if feedback is None:
                        stage = "сохранение результата"
                        await bridge.git("add", "-A")
                        changes = await bridge.git("diff", "--name-only", checkpoint, "--", ".")
                        complete = "LOOMIS_COMPLETE" in result.message.splitlines()
                        if project.commit_mode == "sequential" or complete:
                            label = "Итог проекта" if project.commit_mode == "single_preset" else "Шаг разработки"
                            await bridge.commit(f"{label}: {config.name}", allow_empty=True, author=config.name)
                            committed = True
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
            })
            await self.failover(project, available, runtime, config, task, handoff, error,
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
        ranked = await self.rank_successors(project.id, available, config.id)
        next_config = next((item for item in ranked if item.id not in runtime.successful_agents),
                           ranked[0] if ranked else config)
        target = "@All" if complete else f"@{next_config.name}"
        report = (
            f"От: {config.name}\nКому: {target}\n"
            f"Изменённые файлы:\n{changes or 'Нет изменений'}\n"
            f"Результат и инструкции:\n{result.message}"
        )
        await self.message(project.id, config.name, target, "team_chat", report, config,
                           step_title=STEP_TITLES["execute"])
        await self.notify(project.id, report, event_type="step_log", config=config, in_team_chat=True)
        if not complete:
            runtime.handoff = (next_config.id, report)
        if committed:
            await self.save_commit_state(project, bridge, runtime, complete)
            if project.auto_push_mode == "after_each":
                await self.publish_github_step(project, bridge, complete)
        if complete:
            runtime.ready.clear()
            await self.status(project.id, "completed", turn_state=runtime.snapshot())
            return
        if project.autonomy_mode == "step_by_step":
            await self.pause_for_approval(project, runtime, f"Следующий шаг: @{next_config.name}.", "step", next_config)
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
                "turn": runtime.snapshot(), "status": "completed" if complete else "paused",
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
                row.status = "completed"
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
            # The successor must work against the approved plan.
            if await bridge.git("diff", "--name-only", checkpoint, "--", "PLAN.md"):
                await bridge.git("restore", f"--source={checkpoint}", "--staged", "--worktree", "--", "PLAN.md")
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
            plan = self.context(self.workspace(project), ("PLAN.md",))
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
            "файлов в репозитории, доделай его незаконченную часть задачи по PLAN.md и затем выполни свою часть. "
            f"Незавершённая задача: {task}"
        )
        if successor is None:
            runtime.handoff = (broken.id, f"{instruction}\n{diagnostic}\n{handoff}\n{partial_diff}")
            runtime.ready.clear()
            await self.status(project.id, "paused", turn_state=runtime.snapshot())
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
        if project.failover_mode == "manual":
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
