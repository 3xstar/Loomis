"""Run with python -m worker.worker_node ws://master:8000/ws/workers NODE config.json."""
import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from worker.adapters import build_agent
from worker.adapters.base import AgentResult
from worker.git_bridge import GitBridge


async def serve(url, name, config):
    async with connect(url, additional_headers={
        "Authorization": f"Bearer {os.environ['LOOMIS_WORKER_TOKEN']}"
    }, max_size=4_000_000) as socket:
        agents = config["agents"]
        await socket.send(json.dumps({"node": name, "agents": list(agents)}))
        active = None
        active_id = None
        pending_approval = None

        async def execute(request):
            nonlocal pending_approval

            async def approve(command):
                nonlocal pending_approval
                pending_approval = asyncio.get_running_loop().create_future()
                try:
                    await socket.send(json.dumps({"type": "approval", "id": request["id"], "command": command}))
                    return await pending_approval
                finally:
                    pending_approval = None

            try:
                agent = build_agent(**agents[request["agent"]], approval=approve)
                operation = request["operation"]
                async with asyncio.timeout(request["timeout"]):
                    if operation == "execute":
                        project_id = request["project_id"]
                        workspace = Path(config["workspaces"][str(project_id)]).resolve(strict=True)
                        bridge = GitBridge(workspace, project_id)
                        await bridge.prepare()
                        if await bridge.git("status", "--porcelain"):
                            raise RuntimeError("Worker checkout must be clean")
                        await bridge.pull()
                        try:
                            result = await agent.execute_task(request["prompt"], workspace, request["timeout"])
                            if result.status == "success":
                                await bridge.commit()
                                await bridge.push()
                        finally:
                            await bridge.restore("HEAD")
                    elif operation == "meeting":
                        result = await agent.participate_in_meeting(request["context"])
                    elif operation == "question":
                        result = await agent.ask_question(request["prompt"], request["context"])
                    else:
                        raise ValueError("Unknown worker operation")
            except TimeoutError:
                result = AgentResult("timeout", "Worker deadline expired")
            except (KeyError, ValueError, OSError, RuntimeError) as exc:
                result = AgentResult("error", str(exc))
            await socket.send(json.dumps({"id": request["id"], **asdict(result)}))

        try:
            async for packet in socket:
                request = json.loads(packet)
                if request.get("type") == "approval_result":
                    if request["id"] == active_id and pending_approval and not pending_approval.done():
                        pending_approval.set_result(request["approved"] is True)
                elif request.get("type") == "cancel":
                    if request["id"] == active_id and active and not active.done():
                        active.cancel()
                        await asyncio.gather(active, return_exceptions=True)
                        active = None
                elif active is None or active.done():
                    if active is not None:
                        await active
                    active_id = request["id"]
                    active = asyncio.create_task(execute(request))
        finally:
            if active is not None:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)


async def reconnect(url, name, config):
    while True:
        try:
            await serve(url, name, config)
        except (ConnectionClosed, OSError):
            pass
        await asyncio.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("node")
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    asyncio.run(reconnect(args.url, args.node, json.loads(args.config.read_text(encoding="utf-8"))))
