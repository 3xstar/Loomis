import asyncio
from base64 import b64encode
import json
import os
from pathlib import Path

from openai import APITimeoutError, AsyncOpenAI, OpenAIError

from worker.sandbox import workspace_file
from .base import AgentResult, BaseAgent


def file_tools():
    for name in ("read_file", "write_file", "list_dir"):
        fields = {"path": {"type": "string"}}
        if name == "write_file":
            fields["content"] = {"type": "string"}
        yield {"type": "function", "function": {
            "name": name,
            "parameters": {"type": "object", "properties": fields,
                           "required": list(fields), "additionalProperties": False},
        }}


def run_file_tool(workspace, name, path, content=None):
    target = workspace_file(workspace, path)
    if name == "read_file":
        if not target.is_file() or target.stat().st_nlink > 1 or target.stat().st_size > 1_000_000:
            raise ValueError("Expected a regular file up to 1 MB")
        return target.read_text(encoding="utf-8")
    if name == "list_dir":
        return json.dumps(sorted(item.name for item in target.iterdir() if item.name.casefold() != ".git"))
    if name == "write_file":
        if target.exists() and (not target.is_file() or target.stat().st_nlink > 1):
            raise ValueError("Expected a regular file without hard links")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return "OK"
    raise ValueError("Unknown tool")


class ApiAgent(BaseAgent):
    @classmethod
    def from_config(cls, model: str, config: dict, **options):
        base_url = config.get("base_url") or config.get("baseUrl") or os.getenv("OPENAI_BASE_URL") or "https://openai.com"
        api_key = config.get("api_key") or config.get("apiKey") or os.getenv("OPENAI_API_KEY")
        return cls(model, base_url, config.get("api_key_env"), api_key=api_key, **options)

    def __init__(self, model: str, base_url: str, api_key_env: str | None = None, *, api_key: str | None = None,
                 instruction: str = "", images=()):
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.api_key = api_key
        self.instruction = instruction
        self.images = images

    async def _run(self, prompt, timeout, workspace=None):
        tokens = 0
        content = prompt
        if self.images:
            content = [{"type": "text", "text": prompt}]
            for mime_type, data in self.images:
                url = f"data:{mime_type};base64,{b64encode(data).decode('ascii')}"
                content.append({"type": "image_url", "image_url": {"url": url}})
        messages = [{"role": "user", "content": content}]
        if self.instruction:
            messages.insert(0, {"role": "system", "content": self.instruction})
        try:
            key = self.api_key or (os.environ[self.api_key_env] if self.api_key_env else "ollama")
            async with asyncio.timeout(timeout), AsyncOpenAI(
                base_url=self.base_url, api_key=key, max_retries=0
            ) as client:
                for _ in range(32):
                    options = {"tools": list(file_tools())} if workspace is not None else {}
                    response = await client.chat.completions.create(model=self.model, messages=messages, **options)
                    if response.usage:
                        tokens += response.usage.total_tokens
                    message = response.choices[0].message
                    if workspace is None or not message.tool_calls:
                        return AgentResult("success", message.content or "", tokens)
                    messages.append(message.model_dump(exclude_none=True, include={"role", "content", "tool_calls"}))
                    for call in message.tool_calls:
                        try:
                            output = run_file_tool(workspace, call.function.name, **json.loads(call.function.arguments))
                        except (OSError, TypeError, ValueError) as exc:
                            output = str(exc)
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                return AgentResult("error", "Tool round limit reached", tokens)
        except APITimeoutError as exc:
            return AgentResult("timeout", str(exc), tokens)
        except TimeoutError:
            return AgentResult("timeout", "Agent deadline expired", tokens)
        except OpenAIError as exc:
            status = "rate_limit" if getattr(exc, "status_code", None) == 429 else "error"
            message = str(exc)
            if key:
                message = message.replace(key, "[redacted]")
            return AgentResult(status, message, tokens)
        except (KeyError, ValueError, IndexError) as exc:
            return AgentResult("error", str(exc), tokens)

    async def execute_task(self, prompt: str, work_dir: Path, timeout: float):
        return await self._run(prompt, timeout, work_dir)

    async def ask_question(self, question, context):
        return await self._run(f"{context}\n\n{question}", 120)
