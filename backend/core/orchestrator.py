import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import uuid4

from sqlalchemy import select

from backend.api.ws import manager, workers
from backend.bot.formatting import telegram_html
from backend.database.db import async_session_factory
from backend.database.models import AgentConfig, AgentMessage, Project, ProjectMedia, TaskLog, User
from worker.adapters import CLI_PROVIDERS, build_agent
from worker.adapters.base import AgentResult, BaseAgent
from worker.git_bridge import GitBridge
from worker.quality_gate import check_quality
from worker.sandbox import workspace_file

from .council import periodic_meeting, starting_council

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    boundary: asyncio.Lock = field(default_factory=asyncio.Lock)
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
        self.restored = True

    def snapshot(self):
        return {
            "last_agent": self.last_agent, "handoff": self.handoff,
            "successful_agents": self.successful_agents, "pending_question": self.pending_question,
            "pending_failover": self.pending_failover, "answer": self.answer,
        }


async def probe_agent(project_id, config):
    prompt = "Ответь одним словом: OK"
    async with asyncio.timeout(15):
        node = config.settings.get("worker")
        if node:
            result = await workers.dispatch(node, config.settings.get("worker_agent", config.name),
                                            project_id, "test", prompt, "", 15)
        else:
            agent = build_agent(config.provider, config.model, config.settings)
            result = await agent.ask_question(prompt, "")
    if result.status != "success":
        raise ValueError(result.message or result.status)
    if not result.message.strip():
        raise ValueError("Провайдер вернул пустой ответ.")


VERBOSITY = {
    "concise": "Отвечай максимально сжато, только факты, код и итог, без воды, до 5–7 строк.",
    "balanced": "Отвечай по существу, объясняй решения и проверки, до 15 строк.",
    "detailed": "Дай подробный ответ с обоснованием решений, проверками и результатами, до 40 строк.",
}


def media_error(config, mime_type):
    provider = config.provider.lower()
    if mime_type.startswith("audio/"):
        if not config.settings.get("supports_audio"):
            return f"{config.name}: поддержка аудио отключена в настройках агента."
    elif not config.settings.get("supports_vision") or (provider in CLI_PROVIDERS and provider != "codex"):
        return f"{config.name}: модель не поддерживает изображения."
    return None


class ProjectAgent(BaseAgent):
    def __init__(self, engine, project, config):
        self.engine = engine
        self.project = project
        self.config = config

    async def run(self, operation, prompt, context="", workspace=None, timeout=None, images=()):
        project, config = self.project, self.config
        instruction = VERBOSITY[project.verbosity_level or "concise"]
        if operation == "plan":
            instruction += " Для PLAN.md верни полный документ со всеми задачами."
        manage_traffic = operation in {"question", "meeting"}
        timeout = timeout if timeout is not None else project.deadline_minutes * 60
        started = datetime.now(timezone.utc)
        clock = monotonic()
        result = AgentResult("error", "Операция прервана.")
        try:
            if manage_traffic:
                await self.engine.traffic(config, "working")
            async with asyncio.timeout(timeout):
                node = config.settings.get("worker")
                if node:
                    worker_operation = "question" if operation in {"handoff", "plan"} else operation
                    result = await workers.dispatch(node, config.settings.get("worker_agent", config.name),
                                                    project.id, worker_operation, prompt, context, timeout,
                                                    instruction=instruction, images=images)
                else:
                    agent = build_agent(config.provider, config.model, config.settings,
                                        lambda command: self.engine.request_approval(project.id, command),
                                        instruction=instruction, images=images)
                    if operation == "execute":
                        result = await agent.execute_task(prompt, workspace, timeout)
                    elif operation == "meeting":
                        result = await agent.participate_in_meeting(context)
                    else:
                        result = await agent.ask_question(prompt, context)
        except TimeoutError:
            result = AgentResult("timeout", "Дедлайн истёк.")
        except Exception as exc:
            result = AgentResult("error", str(exc) or type(exc).__name__)
        finally:
            async with async_session_factory() as session:
                session.add(TaskLog(project_id=project.id, agent_id=config.id,
                                    operation="execute_attempt" if operation == "execute" else operation,
                                    sender_name=config.name, started_at=started, duration_seconds=monotonic() - clock,
                                    status=result.status, tokens=result.tokens))
                if result.status == "rate_limit":
                    row = await session.get(AgentConfig, config.id)
                    if row is not None:
                        row.cooldown_until = datetime.now(timezone.utc) + timedelta(
                            minutes=max(1, int(config.settings.get("cooldown_minutes", 5)))
                        )
                await session.commit()
        await self.engine.message(
            project.id, config.name, "@All", "terminal", result.terminal or result.message, config
        )
        if manage_traffic:
            await self.engine.traffic(
                config, "waiting" if result.status == "success" else "error",
                None if result.status == "success" else result.message,
            )
        return result

    async def execute_task(self, prompt, work_dir, timeout):
        return await self.run("execute", prompt, workspace=work_dir, timeout=timeout)

    async def ask_question(self, question, context):
        images = await self.engine.spec_images(self.project.id, self.config)
        return await self.run("question", question, context, images=images)

    async def participate_in_meeting(self, history):
        return await self.run("meeting", "", history)


class Orchestrator:
    def __init__(self):
        self.runtimes = {}
        self.approvals = {}
        self.bot = None
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
                      media=None, mirror=None):
        agent_id = config.id if config else None
        deliveries = ((channel, target), (mirror, "@User")) if mirror else ((channel, target),)
        rows = [AgentMessage(project_id=project_id, sender_name=sender, target_name=recipient,
                             channel=destination, content=content, agent_id=agent_id)
                for destination, recipient in deliveries]
        async with async_session_factory() as session:
            if media is not None:
                session.add(media)
                await session.flush()
                rows[0].media_id = media.id
            session.add_all(rows)
            await session.commit()
        for row in rows:
            await manager.broadcast(project_id, {
                "type": row.channel, "channel": row.channel, "project_id": project_id, "id": row.id,
                "sender_name": sender, "target_name": row.target_name, "content": content, "message": content,
                "created_at": row.created_at.isoformat(), "agent_id": agent_id,
                "agent_name": config.name if config else None,
                "media_id": row.media_id,
            })

    async def publish_traffic(self, config):
        await manager.broadcast(config.project_id, {
            "type": "traffic_state", "project_id": config.project_id,
            "agent_id": config.id, "agent_name": config.name,
            "traffic_state": config.traffic_state, "last_error": config.last_error,
        })

    async def traffic(self, config, state, error=None):
        async with async_session_factory() as session:
            row = await session.get(AgentConfig, config.id)
            if row is None or (row.traffic_state == state and (error is None or row.last_error == error)):
                return
            row.traffic_state = state
            if error is not None:
                row.last_error = error
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

    async def notify(self, project_id, message, markup=None, event_type="log", config=None, *, critical=False):
        event = {"type": event_type, "project_id": project_id, "message": message}
        if config is not None:
            event.update(agent_id=config.id, agent_name=config.name)
        if event_type != "step_log":
            await manager.broadcast(project_id, event)
        if self.bot:
            async with async_session_factory() as session:
                owner = await session.scalar(select(User).join(
                    Project, Project.owner_id == User.id
                ).where(Project.id == project_id))
            if owner and owner.telegram_id and (owner.tg_logs_enabled or markup or critical):
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

    def decide(self, project_id, key, approved):
        item = self.approvals.get(key)
        if item is None or item[0] != project_id or item[1].done():
            raise ValueError("Запрос подтверждения уже закрыт.")
        item[1].set_result(approved)

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
                            await probe_agent(project_id, config)
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
                if any(item.settings.get('worker') for item in configs):
                    await bridge.push()
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
        runtime.ready.set()
        if runtime.task is None or runtime.task.done():
            runtime.task = asyncio.create_task(self.loop(project_id, runtime))
        return "running"

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
        async def publish(agent, content):
            config = agent.config
            await self.message(project_id, config.name, "@All", "team_chat", content, config,
                               mirror="direct" if targeted else None)

        async def synthesize(agent, prompt, context):
            images = await self.spec_images(project_id, agent.config)
            return await agent.run("plan", prompt, context, images=images)

        try:
            agents = [(item.name, ProjectAgent(self, project, item)) for item in selected]
            plan = await starting_council(
                self.workspace(project), agents, feedback, publish=publish,
                roster=[item.name for item in configs], synthesize=synthesize,
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
            await self.message(project_id, "User", target, "team_chat", question, media=media)
            context = self.context(self.workspace(project), ("SPEC.md", "PLAN.md", "Project_context.md"))
            audio = None
            if (media and media.mime_type.startswith("audio/") and
                    any(not media_error(config, media.mime_type) for config in selected)):
                audio = asyncio.create_task(self.transcribe_question(media))

            async def respond(config):
                response = await self.ask(project, config, question, context, media, audio=audio)
                await self.message(project_id, config.name, "@User", "team_chat", response.message, config,
                                   mirror="direct" if target_agent_ids is not None else None)
                return {"agent_id": config.id, "status": response.status,
                        "message": response.message, "tokens": response.tokens}

            try:
                return await asyncio.gather(*(respond(config) for config in selected))
            finally:
                if audio is not None and not audio.done():
                    audio.cancel()
                    await asyncio.gather(audio, return_exceptions=True)


    @staticmethod
    async def transcribe_question(media):
        from .media import transcribe_media

        try:
            return await transcribe_media(media)
        except Exception:
            logger.exception("Audio transcription failed for project %s", media.project_id)
            return None

    async def ask(self, project, config, question, context, media=None, *, audio=None):
        if media and (error := media_error(config, media.mime_type)):
            return AgentResult("unsupported_media", error)
        images = await self.spec_images(project.id, config)
        if media:
            if media.mime_type.startswith("audio/"):
                transcript = await audio if audio is not None else await self.transcribe_question(media)
                if transcript is None:
                    return AgentResult("error", "Не удалось распознать аудиофайл.")
                question += f"\n\nРасшифровка аудио:\n{transcript}"
            else:
                images.append((media.mime_type, media.data))
        return await ProjectAgent(self, project, config).run("question", question, context, images=images)

    async def spec_images(self, project_id, config):
        if media_error(config, "image/png"):
            return []
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
                                   mirror="team_chat" if mirror else None)
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
            await self.status(project_id, "paused")
            await self.notify(project_id, f"Разработка приостановлена. {exc}", critical=True)

    async def turn(self, project, configs, runtime):
        now = datetime.now(timezone.utc)
        available = [
            item for item in configs if item.traffic_state != "error" and (
                item.cooldown_until is None or item.cooldown_until.replace(tzinfo=timezone.utc) <= now
            )
        ]
        if runtime.handoff:
            config = next((item for item in available if item.id == runtime.handoff[0]), None)
        else:
            config = next((item for item in available if item.id > runtime.last_agent),
                          available[0] if available else None)
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
        if monotonic() >= runtime.next_meeting:
            diff = await bridge.diff(runtime.meeting_base) if runtime.meeting_base else ""
            summary = await periodic_meeting(workspace, [
                (item.name, ProjectAgent(self, project, item)) for item in available
            ], diff, config.name)
            await self.notify(project.id, summary, event_type="meeting")
            runtime.next_meeting = monotonic() + interval
            runtime.meeting_base = None
        remote = config.settings.get("worker")
        if remote:
            await bridge.synchronize()
        checkpoint = await bridge.checkpoint()
        runtime.meeting_base = runtime.meeting_base or checkpoint
        clock = asyncio.get_running_loop()
        deadline = clock.time() + project.deadline_minutes * 60
        feedback = ""
        context = self.context(workspace, ("SPEC.md", "PLAN.md", "Project_context.md"))
        handoff = runtime.handoff[1] if runtime.handoff else ""
        task = f"Выполни следующий шаг части плана, назначенной агенту {config.name}."
        await self.traffic(config, "working")
        agent = ProjectAgent(self, project, config)
        clarification = ""
        images = await self.spec_images(project.id, config)
        try:
            async with asyncio.timeout_at(deadline):
                if handoff:
                    receipt = await agent.run(
                        "handoff", f"Ты {config.name}. Кратко сообщи команде, какую работу начнёшь. {task}",
                        f"{handoff}\n{runtime.answer}", timeout=max(0.01, deadline - clock.time()),
                    )
                    if receipt.status != "success" or not receipt.message.strip():
                        raise RuntimeError(receipt.message.strip() or "Агент не подтвердил передачу задачи.")
                    await self.message(project.id, config.name, "@All", "team_chat",
                                       f"Принял передачу.\n{receipt.message}", config)
                while True:
                    if remote:
                        await bridge.push()
                    correction = f"Исправь ошибки проверки перед передачей хода:\n{feedback[:15000]}" if feedback else ""
                    result = await agent.run(
                        "execute", f"{context}\n\nТы {config.name}. {task}\n"
                        f"{handoff}\n{runtime.answer}\n{correction}\n"
                        "В итоговом отчёте опиши результат и инструкции следующему исполнителю. "
                        "Сохраняй утверждённый PLAN.md. "
                        + ("При неоднозначных требованиях также используй LOOMIS_QUESTION. "
                           if project.autonomy_mode == "consultative" else "")
                        + "Если данных не хватает для продолжения, верни только LOOMIS_QUESTION: <вопрос>. "
                        "Если все задачи плана завершены, верни отдельную строку LOOMIS_COMPLETE.",
                        workspace=workspace, timeout=max(0.01, deadline - clock.time()), images=images,
                    )
                    if result.status != "success":
                        raise RuntimeError(result.message or result.status)
                    if remote:
                        await bridge.pull()
                    response = result.message.strip()
                    if response.startswith("LOOMIS_QUESTION:"):
                        clarification = response.removeprefix("LOOMIS_QUESTION:").strip()
                        if clarification:
                            break
                    self.require_plan(project, configs, approved=True)
                    feedback = await check_quality(
                        project.test_command, workspace, max(0.01, deadline - clock.time()),
                        lambda command: self.request_approval(project.id, command),
                    )
                    if feedback is None:
                        await bridge.commit()
                        break
                    await self.message(project.id, config.name, "@All", "terminal", feedback, config)
        except (TimeoutError, OSError, ValueError, RuntimeError) as exc:
            error = str(exc) or "Дедлайн шага истёк."
            await self.traffic(config, "error", error)
            await bridge.rollback_last_step()
            if remote:
                await bridge.push()
            if runtime.meeting_base == checkpoint:
                runtime.meeting_base = None
            await self.failover(project, configs, available, runtime, config, task, handoff, error)
            return
        if clarification:
            await bridge.rollback_last_step()
            if remote:
                await bridge.push()
            if runtime.meeting_base == checkpoint:
                runtime.meeting_base = None
            runtime.handoff = (config.id, handoff)
            runtime.pending_question = config.id
            runtime.ready.clear()
            await self.status(project.id, "paused", turn_state=runtime.snapshot())
            await self.traffic(config, "asking")
            await self.message(project.id, config.name, "@User", "direct", clarification, config)
            await self.notify(project.id, f"{config.name} спрашивает: {clarification}",
                              event_type="question", config=config, critical=True)
            return
        async with async_session_factory() as session:
            session.add(TaskLog(project_id=project.id, agent_id=config.id, sender_name=config.name,
                                operation="completed_step", duration_seconds=0, status="success"))
            await session.commit()
        changes = await bridge.git("diff", "--name-only", checkpoint, "HEAD")
        runtime.answer = ""
        runtime.handoff = None
        runtime.last_agent = config.id
        if config.id in runtime.successful_agents:
            runtime.successful_agents.remove(config.id)
        runtime.successful_agents.append(config.id)
        await self.traffic(config, "waiting")
        complete = "LOOMIS_COMPLETE" in result.message.splitlines()
        next_config = next((item for item in available if item.id > config.id), available[0])
        target = "@All" if complete else f"@{next_config.name}"
        report = (
            f"От: {config.name}\nКому: {target}\n"
            f"Изменённые файлы:\n{changes or 'Нет изменений'}\n"
            f"Результат и инструкции:\n{result.message}"
        )
        await self.message(project.id, config.name, target, "team_chat", report, config)
        await self.notify(project.id, report, event_type="step_log", config=config)
        if complete:
            runtime.ready.clear()
            await self.status(project.id, "completed", turn_state=runtime.snapshot())
            return
        runtime.handoff = (next_config.id, report)
        if project.autonomy_mode == "step_by_step":
            await self.pause_for_approval(project, runtime, f"Следующий шаг: @{next_config.name}.", "step", next_config)
        else:
            await self.save_turn(project.id, runtime)

    async def failover(self, project, configs, available, runtime, broken, task, handoff, error):
        candidates = {item.id: item for item in available if item.id != broken.id}
        successor = next((item for item in configs if item.id > broken.id and item.id in candidates), None)
        if successor is None:
            successor = next((candidates[key] for key in reversed(runtime.successful_agents)
                              if key < broken.id and key in candidates), None)
        runtime.handoff = None
        runtime.last_agent = broken.id
        if successor is None:
            runtime.ready.clear()
            await self.status(project.id, "paused", turn_state=runtime.snapshot())
            await self.notify(project.id, f"{broken.name}: {error}\nНет доступного агента для передачи задачи.", critical=True)
            return
        instruction = (
            f"Агент {broken.name} сломался на задаче: {task} "
            "Сначала проанализируй и доделай его незаконченную часть, затем приступай к своей"
        )
        report = (
            f"От: {broken.name}\nКому: @{successor.name}\nОшибка: {error}\n"
            f"Инструкции: {instruction}\nИсходный контекст передачи:\n{handoff}"
        )
        await self.message(project.id, broken.name, f"@{successor.name}", "team_chat", report, broken)
        runtime.handoff = (successor.id, report)
        if project.failover_mode == "manual":
            runtime.pending_failover = f"{broken.name}: {error}\nПередать задачу @{successor.name}?"
            await self.pause_for_approval(project, runtime, runtime.pending_failover, "failover", broken)
        else:
            await self.save_turn(project.id, runtime)

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
