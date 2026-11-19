import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from backend.api.auth import user_for_token
from backend.database.db import async_session_factory
from backend.database.models import Project

router = APIRouter()
MAX_WORKER_IMAGE_BYTES = 20 * 1024 * 1024


class ConnectionManager:
    def __init__(self):
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, project_id: int, socket: WebSocket, subprotocol: str | None = None) -> None:
        await socket.accept(subprotocol=subprotocol)
        self.connections.setdefault(project_id, set()).add(socket)

    def disconnect(self, project_id: int, socket: WebSocket) -> None:
        sockets = self.connections.get(project_id)
        if sockets is not None:
            sockets.discard(socket)
            if not sockets:
                self.connections.pop(project_id)

    async def broadcast(self, project_id: int, event: dict) -> None:
        async def send(socket: WebSocket) -> None:
            try:
                await asyncio.wait_for(socket.send_json(event), timeout=5)
            except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
                self.disconnect(project_id, socket)
                try:
                    await asyncio.wait_for(socket.close(code=1011), timeout=5)
                except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
                    pass

        await asyncio.gather(*(send(socket) for socket in tuple(self.connections.get(project_id, ()))))

    async def publish_status(self, project_id: int, status: str) -> None:
        await self.broadcast(project_id, {"type": "status", "project_id": project_id, "status": status})

    async def publish_log(self, project_id: int, message: str) -> None:
        await self.broadcast(project_id, {"type": "log", "project_id": project_id, "message": message})

    async def close_project(self, project_id: int) -> None:
        await self.publish_status(project_id, "deleted")
        sockets = self.connections.pop(project_id, ())
        await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)


manager = ConnectionManager()


@router.websocket("/ws/{project_id:int}")
async def project_events(socket: WebSocket, project_id: int):
    scheme, _, token = socket.headers.get("authorization", "").partition(" ")
    protocols = socket.scope.get("subprotocols", [])
    subprotocol = None
    if not scheme and len(protocols) == 2 and protocols[0] == "loomis":
        scheme, token = "bearer", protocols[1]
        subprotocol = "loomis"
    async with async_session_factory() as session:
        user = await user_for_token(session, token if scheme.lower() == "bearer" else None)
        project = None if user is None else await session.scalar(
            select(Project).where(Project.id == project_id, Project.owner_id == user.id)
        )
    if project is None:
        await socket.close(code=1008)
        return

    await manager.connect(project_id, socket, subprotocol=subprotocol)
    try:
        await socket.send_json({"type": "status", "project_id": project_id, "status": project.status})
        while (await socket.receive())["type"] != "websocket.disconnect":
            pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(project_id, socket)



class WorkerPool:
    def __init__(self):
        self.nodes = {}

    async def dispatch(self, node, agent, project_id, operation, prompt, context, timeout, *, instruction="", images=()):
        from base64 import b64encode
        from uuid import uuid4

        from worker.adapters.base import AgentResult

        if sum(len(data) for _, data in images) > MAX_WORKER_IMAGE_BYTES:
            return AgentResult("error", "Общий размер изображений для воркера превышает 20 МБ.")
        peer = self.nodes.get(node)
        if peer is None or agent not in peer["agents"]:
            return AgentResult("error", "Воркер или агент недоступен.")
        async with peer["lock"]:
            if self.nodes.get(node) is not peer:
                return AgentResult("error", "Воркер отключился.")
            key = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            peer["job"] = (key, project_id, future)
            try:
                await peer["socket"].send_json({
                    "id": key, "project_id": project_id, "agent": agent,
                    "operation": operation, "prompt": prompt, "context": context, "timeout": timeout,
                    "instruction": instruction,
                    "images": [(mime, b64encode(data).decode("ascii")) for mime, data in images],
                })
                return await future
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(peer["socket"].send_json({"type": "cancel", "id": key}), 3)
                except (OSError, RuntimeError, WebSocketDisconnect, TimeoutError):
                    pass
                raise
            except (OSError, RuntimeError, WebSocketDisconnect):
                return AgentResult("error", "Соединение с воркером потеряно.")
            finally:
                peer["job"] = None
                if peer["approval"]:
                    peer["approval"].cancel()
                    await asyncio.gather(peer["approval"], return_exceptions=True)
                    peer["approval"] = None


workers = WorkerPool()


@router.websocket("/ws/workers")
async def worker_events(socket: WebSocket):
    import secrets

    from backend.settings import settings
    from worker.adapters.base import AgentResult

    expected = settings.loomis_worker_token
    supplied = socket.headers.get("authorization", "").encode()
    if not expected or not secrets.compare_digest(supplied, f"Bearer {expected}".encode()):
        await socket.close(code=1008)
        return
    await socket.accept()
    node, peer = None, None
    try:
        registration = await asyncio.wait_for(socket.receive_json(), 10)
        node = registration["node"]
        agents = registration["agents"]
        if (not isinstance(node, str) or not node or node in workers.nodes
                or not isinstance(agents, list) or not all(isinstance(name, str) for name in agents)):
            await socket.close(code=1008)
            return
        peer = {"socket": socket, "agents": set(agents), "lock": asyncio.Lock(), "job": None, "approval": None}
        workers.nodes[node] = peer

        async def approve(command, job):
            from backend.core.orchestrator import orchestrator

            approved = await orchestrator.request_approval(job[1], command)
            if peer["job"] is job:
                await socket.send_json({"type": "approval_result", "id": job[0], "approved": approved})

        while True:
            packet = await socket.receive_json()
            job = peer["job"]
            if job is None or packet.get("id") != job[0] or job[2].done():
                continue
            if packet.get("type") == "approval":
                if peer["approval"] is None or peer["approval"].done():
                    peer["approval"] = asyncio.create_task(approve(str(packet["command"])[:4000], job))
                continue
            status = packet["status"]
            if status not in {"success", "rate_limit", "timeout", "error"}:
                raise ValueError("Unknown worker status")
            job[2].set_result(AgentResult(status, str(packet["message"]), int(packet["tokens"]), packet.get("terminal")))
    except (WebSocketDisconnect, TimeoutError, KeyError, TypeError, ValueError, AttributeError):
        pass
    finally:
        if peer is not None:
            workers.nodes.pop(node, None)
            job = peer["job"]
            if job is not None and not job[2].done():
                job[2].set_result(AgentResult("error", "Воркер отключился."))
        try:
            await socket.close()
        except (OSError, RuntimeError, WebSocketDisconnect):
            pass
