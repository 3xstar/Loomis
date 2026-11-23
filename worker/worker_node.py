"""Run with python -m worker.worker_node ws://master:8000/ws/workers NODE config.json."""
import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from base64 import b64decode

from worker.adapters import CLI_PROVIDERS, build_agent
from worker.adapters.api_adapter import ApiAgent, api_model_key
from worker.adapters.base import AgentResult
from worker.git_bridge import GitBridge


async def save_worker_changes(bridge, transport_ref=None):
    async def publish():
        if await bridge.git("status", "--porcelain"):
            await bridge.commit()
            if transport_ref:
                await bridge.git("push", "origin", f"HEAD:{transport_ref}")
            else:
                await bridge.push()

    pending = asyncio.create_task(publish())
    try:
        await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise


async def serve(url, name, config):
    async with connect(url, additional_headers={
        "Authorization": f"Bearer {os.environ['LOOMIS_WORKER_TOKEN']}"
    }, max_size=32 * 1024 * 1024) as socket:
        agents = config["agents"]
        api_models = {
            alias: [api_model_key(agent["model"], agent["settings"]), agent["settings"].get("temperature", 0.2)]
            for alias, agent in agents.items() if agent["provider"].lower() not in CLI_PROVIDERS
        }
        await socket.send(json.dumps({"node": name, "agents": list(agents), "api_models": api_models}))
        active = None
        active_id = None
        pending_approval = None

        async def execute(request):
            nonlocal pending_approval
            agent = None

            async def approve(command):
                nonlocal pending_approval
                pending_approval = asyncio.get_running_loop().create_future()
                try:
                    await socket.send(json.dumps({"type": "approval", "id": request["id"], "command": command}))
                    return await pending_approval
                finally:
                    pending_approval = None

            try:
                operation = request["operation"]
                agent = build_agent(
                    **agents[request["agent"]], approval=None if operation == "test" else approve,
                    instruction=request.get("instruction", ""),
                    images=[(mime, b64decode(data)) for mime, data in request.get("images", [])],
                )
                async with asyncio.timeout(request["timeout"]):
                    if operation == "execute":
                        project_id = request["project_id"]
                        workspace = Path(config["workspaces"][str(project_id)]).resolve(strict=True)
                        bridge = GitBridge(workspace, project_id)
                        await bridge.initialize()
                        if await bridge.git("status", "--porcelain"):
                            raise RuntimeError("Worker checkout must be clean")
                        transport_ref = request.get("transport_ref")
                        if transport_ref:
                            head = await bridge.head()
                            branch = await bridge.git("symbolic-ref", "--short", "-q", "HEAD", allowed=(0, 1))
                            await bridge.fetch_transport(transport_ref)
                            await bridge.git("switch", "--detach", "FETCH_HEAD")
                        else:
                            await bridge.prepare()
                            await bridge.pull()
                        try:
                            result = await agent.execute_task(request["prompt"], workspace, request["timeout"])
                        finally:
                            try:
                                await save_worker_changes(bridge, transport_ref)
                            finally:
                                if transport_ref and head:
                                    if branch:
                                        await bridge.git("switch", branch)
                                    else:
                                        await bridge.git("switch", "--detach", head)
                    elif operation == "meeting":
                        result = await agent.participate_in_meeting(request["context"])
                    elif operation in {"question", "test"}:
                        result = await agent.ask_question(request["prompt"], request["context"])
                    else:
                        raise ValueError("Unknown worker operation")
            except TimeoutError:
                result = AgentResult("timeout", "Worker deadline expired", getattr(agent, "tokens_used", 0))
            except asyncio.CancelledError:
                await socket.send(json.dumps({
                    "id": request["id"], **asdict(AgentResult(
                        "timeout", "Worker execution interrupted", getattr(agent, "tokens_used", 0),
                    )),
                }))
                raise
            except (KeyError, ValueError, OSError, RuntimeError) as exc:
                result = AgentResult("error", str(exc), getattr(agent, "tokens_used", 0))
            if isinstance(agent, ApiAgent):
                result.cacheable = agent.used_requested_temperature
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
