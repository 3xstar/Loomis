import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from sqlalchemy import select

from backend.api.ws import manager, workers
from backend.database.db import async_session_factory
from backend.database.models import AgentConfig, Project, TaskLog, User
from worker.adapters import build_agent
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


class ProjectAgent(BaseAgent):
    def __init__(self, engine, project, config):
        self.engine = engine
        self.project = project
        self.config = config

    async def run(self, operation, prompt, context="", workspace=None, timeout=None):
        project, config = self.project, self.config
        timeout = timeout if timeout is not None else project.deadline_minutes * 60
        started = datetime.now(timezone.utc)
        clock = monotonic()
        result = AgentResult("error", "Операция прервана.")
        try:
            async with asyncio.timeout(timeout):
                node = config.settings.get("worker")
                if node:
                    result = await workers.dispatch(node, config.settings.get("worker_agent", config.name),
                                                    project.id, operation, prompt, context, timeout)
                else:
                    agent = build_agent(config.provider, config.model, config.settings,
                                        lambda command: self.engine.request_approval(project.id, command))
                    if operation == "execute":
                        result = await agent.execute_task(prompt, workspace, timeout)
                    elif operation == "meeting":
                        result = await agent.participate_in_meeting(context)
                    else:
                        result = await agent.ask_question(prompt, context)
        except TimeoutError:
            result = AgentResult("timeout", "Дедлайн истёк.")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            result = AgentResult("error", str(exc))
        finally:
            async with async_session_factory() as session:
                session.add(TaskLog(project_id=project.id, agent_id=config.id, operation=operation,
                                    started_at=started, duration_seconds=monotonic() - clock,
                                    status=result.status, tokens=result.tokens))
                if result.status == "rate_limit":
                    row = await session.get(AgentConfig, config.id)
                    if row is not None:
                        row.cooldown_until = datetime.now(timezone.utc) + timedelta(
                            minutes=max(1, int(config.settings.get("cooldown_minutes", 5)))
                        )
                await session.commit()
        return result

    async def execute_task(self, prompt, work_dir, timeout):
        return await self.run("execute", prompt, workspace=work_dir, timeout=timeout)

    async def ask_question(self, question, context):
        return await self.run("question", question, context)

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

    async def status(self, project_id, status):
        async with async_session_factory() as session:
            project = await session.get(Project, project_id)
            project.status = status
            await session.commit()
        await manager.publish_status(project_id, status)

    @staticmethod
    def workspace(project):
        if not project.workspace_path:
            raise ValueError("Рабочая папка не задана.")
        return Path(project.workspace_path).resolve(strict=True)

    async def notify(self, project_id, message, markup=None, event_type="log"):
        await manager.broadcast(project_id, {"type": event_type, "project_id": project_id, "message": message})
        if self.bot:
            async with async_session_factory() as session:
                chat_id = await session.scalar(select(User.telegram_id).join(
                    Project, Project.owner_id == User.id
                ).where(Project.id == project_id))
            if chat_id:
                try:
                    for offset in range(0, len(message), 3900):
                        await self.bot.send_message(chat_id, message[offset:offset + 3900],
                                                    reply_markup=markup if offset == 0 else None)
                except Exception:
                    logger.exception("Meeting or approval delivery failed for project %s", project_id)

    async def request_approval(self, project_id, command):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.approvals[key] = (project_id, future)
        try:
            await manager.broadcast(project_id, {"type": "approval", "id": key, "command": command})
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Разрешить", callback_data=f"permit:{key}:1"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"permit:{key}:0"),
            ]])
            await self.notify(project_id, f"Подтвердите команду\n{command}", markup)
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

    async def action(self, project_id, action):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            if action == "council" and project.status == "planning":
                if not configs:
                    raise ValueError("Добавьте хотя бы одного агента.")
                await self.status(project_id, "council")
                runtime.task = asyncio.create_task(self.plan(project_id, project, configs))
                return "council"
            if action == "approve" and project.status == "awaiting_approval":
                workspace_file(self.workspace(project), "PLAN.md").stat()
                if not configs:
                    raise ValueError("Добавьте хотя бы одного агента.")
            elif action == "resume" and project.status == "paused":
                if not configs:
                    raise ValueError("Добавьте хотя бы одного агента.")
            elif action == "pause" and project.status == "running":
                runtime.ready.clear()
                await self.status(project_id, "paused")
                return "paused"
            elif action == "rollback" and project.status in {"paused", "completed"}:
                bridge = GitBridge(self.workspace(project), project_id)
                await bridge.prepare()
                await bridge.rollback_last_step()
                if any(item.settings.get('worker') for item in configs):
                    await bridge.push()
                runtime.meeting_base = None
                if project.status == "completed":
                    await self.status(project_id, "paused")
                return "paused"
            else:
                raise ValueError("Действие недоступно в текущем статусе.")
            await self.status(project_id, "running")
            runtime.ready.set()
            if runtime.task is None or runtime.task.done():
                runtime.task = asyncio.create_task(self.loop(project_id, runtime))
            return "running"

    async def plan(self, project_id, project, configs):
        try:
            plan = await starting_council(
                self.workspace(project), [(item.name, ProjectAgent(self, project, item)) for item in configs]
            )
            await self.status(project_id, "awaiting_approval")
            from backend.bot.keyboards import project_keyboard

            project.status = "awaiting_approval"
            await self.notify(project_id, plan, project_keyboard(project))
        except Exception as exc:
            await self.status(project_id, "planning")
            await self.notify(project_id, f"Ошибка консилиума. {exc}")

    async def question(self, project_id, agent_id, question):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            if project.status in {"draft", "council"}:
                raise ValueError("Чат недоступен в текущем статусе.")
            config = next((item for item in configs if item.id == agent_id), None)
            if config is None:
                raise ValueError("Агент недоступен.")
            context = self.context(self.workspace(project), ("SPEC.md", "PLAN.md", "Project_context.md"))
            was_running = runtime.ready.is_set()
            runtime.ready.clear()
            try:
                return asdict(await ProjectAgent(self, project, config).ask_question(question, context))
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
                    if not configs:
                        runtime.ready.clear()
                        await self.status(project_id, "paused")
                        return
                    await self.turn(project, configs, runtime)
                await asyncio.sleep(0.1)
        except Exception as exc:
            runtime.ready.clear()
            await self.status(project_id, "paused")
            await self.notify(project_id, f"Разработка приостановлена. {exc}")

    async def turn(self, project, configs, runtime):
        now = datetime.now(timezone.utc)
        available = [item for item in configs if item.cooldown_until is None or
                     item.cooldown_until.replace(tzinfo=timezone.utc) <= now]
        config = next((item for item in available if item.id > runtime.last_agent),
                      available[0] if available else None)
        workspace = self.workspace(project)
        bridge = GitBridge(workspace, project.id)
        await bridge.prepare()
        interval = project.meeting_interval_minutes * 60
        if runtime.next_meeting is None:
            runtime.next_meeting = monotonic() + interval
        if monotonic() >= runtime.next_meeting:
            diff = await bridge.diff(runtime.meeting_base) if runtime.meeting_base else ""
            summary = await periodic_meeting(workspace, [
                (item.name, ProjectAgent(self, project, item)) for item in configs
            ], diff, config.name if config else "все агенты на паузе по лимиту")
            await self.notify(project.id, summary, event_type="meeting")
            runtime.next_meeting = monotonic() + interval
            runtime.meeting_base = None
        if config is None:
            await asyncio.sleep(1)
            return
        remote = config.settings.get("worker")
        if remote:
            await bridge.synchronize()
        checkpoint = await bridge.checkpoint()
        runtime.meeting_base = runtime.meeting_base or checkpoint
        clock = asyncio.get_running_loop()
        deadline = clock.time() + project.deadline_minutes * 60
        feedback = ""
        context = self.context(workspace, ("SPEC.md", "PLAN.md", "Project_context.md"))
        agent = ProjectAgent(self, project, config)
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    if remote:
                        await bridge.push()
                    correction = f"Исправь ошибки проверки перед передачей хода:\n{feedback[:15000]}" if feedback else ""
                    result = await agent.execute_task(
                        f"{context}\n\nТы {config.name}. Выполни следующий шаг своей части плана. "
                        f"Если все задачи плана завершены, верни отдельную строку LOOMIS_COMPLETE.\n"
                        f"{correction}",
                        workspace, max(0.01, deadline - clock.time()),
                    )
                    if result.status != "success":
                        if not feedback or result.status == "rate_limit":
                            await bridge.rollback_last_step()
                            if runtime.meeting_base == checkpoint:
                                runtime.meeting_base = None
                            if remote and feedback:
                                await bridge.push()
                            runtime.last_agent = config.id
                            await manager.publish_log(project.id, f"{config.name}: {result.status}\n{result.message[:5000]}")
                            return
                        raise RuntimeError(f"{config.name} ({result.status}) {result.message}")
                    if remote:
                        await bridge.pull()
                    feedback = await check_quality(
                        project.test_command, workspace, max(0.01, deadline - clock.time()),
                        lambda command: self.request_approval(project.id, command),
                    )
                    if feedback is None:
                        await bridge.commit()
                        break
                    await manager.publish_log(project.id, f"{config.name}, проверка проекта\n{feedback[:5000]}")
            runtime.last_agent = config.id
            await manager.publish_log(project.id, f"{config.name}\n{result.message[:5000]}")
            if "LOOMIS_COMPLETE" in result.message.splitlines():
                runtime.ready.clear()
                await self.status(project.id, "completed")
        except TimeoutError as exc:
            raise RuntimeError(f"{config.name}: дедлайн шага истёк.") from exc

    async def stop(self, project_id):
        runtime = self.runtimes.pop(project_id, None)
        if runtime and runtime.task and not runtime.task.done():
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)

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
