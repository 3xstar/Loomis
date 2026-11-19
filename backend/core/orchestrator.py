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
from backend.database.db import async_session_factory
from backend.database.models import AgentConfig, AgentMessage, Project, TaskLog, User
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


class ProjectAgent(BaseAgent):
    def __init__(self, engine, project, config):
        self.engine = engine
        self.project = project
        self.config = config

    async def run(self, operation, prompt, context="", workspace=None, timeout=None):
        project, config = self.project, self.config
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
                    worker_operation = "question" if operation == "handoff" else operation
                    result = await workers.dispatch(node, config.settings.get("worker_agent", config.name),
                                                    project.id, worker_operation, prompt, context, timeout)
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
        except Exception as exc:
            result = AgentResult("error", str(exc) or type(exc).__name__)
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

    async def message(self, project_id, sender, target, channel, content, config=None):
        async with async_session_factory() as session:
            row = AgentMessage(project_id=project_id, sender_name=sender, target_name=target,
                               channel=channel, content=content)
            session.add(row)
            await session.commit()
        await manager.broadcast(project_id, {
            "type": channel, "channel": channel, "project_id": project_id, "id": row.id,
            "sender_name": sender, "target_name": target, "content": content, "message": content,
            "created_at": row.created_at.isoformat(), "agent_id": config.id if config else None,
            "agent_name": config.name if config else None,
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

    async def notify(self, project_id, message, markup=None, event_type="log", config=None):
        event = {"type": event_type, "project_id": project_id, "message": message}
        if config is not None:
            event.update(agent_id=config.id, agent_name=config.name)
        await manager.broadcast(project_id, event)
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

    async def action(self, project_id, action, *, feedback=None):
        runtime = self.runtime(project_id)
        async with runtime.boundary:
            project, configs = await self.load(project_id)
            runtime.restore(project)
            if action in {"council", "regenerate_plan"} and project.status not in {"draft", "council"}:
                if not configs:
                    raise ValueError("Добавьте хотя бы одного агента.")
                workspace = self.workspace(project)
                if feedback is not None:
                    if not workspace_file(workspace, "PLAN.md").is_file():
                        raise ValueError("Сначала создайте PLAN.md.")
                    await self.message(project_id, "User", "@All", "team_chat", feedback)
                await self.plan_invalidated(project, publish=False)
                await self.status(project_id, "council", plan_digest=None, approved_plan_digest=None, turn_state=None)
                runtime.council = asyncio.create_task(self.plan(project_id, project, configs, feedback))
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
                await self.notify(project_id, str(exc))
                return
            runtime.pending_failover = None
            await self.start_running(project_id, runtime, digest)

    async def plan(self, project_id, project, configs, feedback):
        try:
            plan = await starting_council(
                self.workspace(project), [(item.name, ProjectAgent(self, project, item)) for item in configs], feedback
            )
            await self.status(project_id, "awaiting_approval", plan_digest=self.plan_digest(project))
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
            runtime.restore(project)
            config = next((item for item in configs if item.id == agent_id), None)
            if config is None:
                raise ValueError("Агент недоступен.")
            target = f"@{config.name}"
            if runtime.pending_question == agent_id:
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
            await self.message(project_id, "User", target, "direct", question, config)
            context = self.context(self.workspace(project), ("SPEC.md", "PLAN.md", "Project_context.md"))
            was_running = runtime.ready.is_set()
            runtime.ready.clear()
            try:
                result = await ProjectAgent(self, project, config).ask_question(question, context)
                await self.message(project_id, config.name, "@User", "direct", result.message, config)
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
            await self.notify(project_id, f"Разработка приостановлена. {exc}")

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
            await self.notify(project.id, "Нет доступного исполнителя. Проверьте агентов перед возобновлением.")
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
                    result = await agent.execute_task(
                        f"{context}\n\nТы {config.name}. {task}\n"
                        f"{handoff}\n{runtime.answer}\n{correction}\n"
                        "В итоговом отчёте опиши результат и инструкции следующему исполнителю. "
                        "Сохраняй утверждённый PLAN.md. "
                        + ("При неоднозначных требованиях также используй LOOMIS_QUESTION. "
                           if project.autonomy_mode == "consultative" else "")
                        + "Если данных не хватает для продолжения, верни только LOOMIS_QUESTION: <вопрос>. "
                        "Если все задачи плана завершены, верни отдельную строку LOOMIS_COMPLETE.",
                        workspace, max(0.01, deadline - clock.time()),
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
                              event_type="question", config=config)
            return
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
            await self.notify(project.id, f"{broken.name}: {error}\nНет доступного агента для передачи задачи.")
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
