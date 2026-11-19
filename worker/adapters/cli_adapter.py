import asyncio
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from worker.sandbox import Approval, authorize_command
from .base import AgentResult, BaseAgent


IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


async def run_shell(command: str, directory: Path, timeout: float, prompt: str = ""):
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    process = await asyncio.create_subprocess_shell(
        command, cwd=directory, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(prompt.encode()), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class CliAgent(BaseAgent):
    def __init__(self, command: str, question_command: str | None = None, approval: Approval | None = None,
                 *, instruction="", images=()):
        self.command = command
        self.question_command = question_command
        self.approval = approval
        self.instruction = instruction
        self.images = images

    async def _run(self, command, prompt, directory, timeout):
        try:
            await authorize_command(command, directory, self.approval)
            if self.instruction:
                prompt = f"{self.instruction}\n\n{prompt}"
            if self.images:
                with TemporaryDirectory() as attachments:
                    paths = []
                    for index, (mime_type, data) in enumerate(self.images):
                        path = Path(attachments) / f"{index}{IMAGE_SUFFIXES[mime_type]}"
                        path.write_bytes(data)
                        paths.append(path.as_posix())
                    arguments = f' --image "{",".join(paths)}"'
                    code, out, err = await run_shell(command.rstrip() + arguments, directory, timeout, prompt)
            else:
                code, out, err = await run_shell(command, directory, timeout, prompt)
        except TimeoutError:
            return AgentResult("timeout", "Agent deadline expired")
        except (OSError, ValueError) as exc:
            return AgentResult("error", str(exc))
        tokens = 0
        messages = []
        for line in out.splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                    messages.append(event["item"]["text"])
                elif event.get("type") == "result" and isinstance(event.get("result"), str):
                    messages.append(event["result"])
                usage = event.get("usage", {})
                tokens = usage.get("total_tokens") or sum(
                    usage.get(key, 0) for key in ("input_tokens", "output_tokens")
                ) or tokens
            except (ValueError, AttributeError, TypeError, KeyError):
                continue
        if re.search(r"rate.limit|too many requests|\b429\b|quota exceeded", err or (out if code else ""), re.I):
            status = "rate_limit"
        elif code == 124 or (code and re.search(r"timed? out|timeout", err, re.I)):
            status = "timeout"
        else:
            status = "error" if code else "success"
        message = ("\n".join(messages) or out) if status == "success" else err or out or f"CLI exit code {code}"
        return AgentResult(status, message.strip(), tokens, "\n".join(filter(None, (out, err))))

    async def execute_task(self, prompt, work_dir, timeout):
        return await self._run(self.command, prompt, work_dir, timeout)

    async def ask_question(self, question, context):
        if not self.question_command:
            return AgentResult("error", "Configure a read-only CLI question_command")
        with TemporaryDirectory() as directory:
            return await self._run(
                self.question_command, f"{context}\n\n{question}", Path(directory), 120
            )
