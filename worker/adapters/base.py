from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class AgentResult:
    status: Literal["success", "timeout", "rate_limit", "error", "unsupported_media"]
    message: str
    tokens: int = 0
    terminal: str | None = None
    cacheable: bool = False


class BaseAgent(ABC):
    @abstractmethod
    async def execute_task(self, prompt: str, work_dir: Path, timeout: float) -> AgentResult:
        ...

    @abstractmethod
    async def ask_question(self, question: str, context: str) -> AgentResult:
        ...

    async def participate_in_meeting(self, history: str) -> AgentResult:
        return await self.ask_question("Summarize progress, blockers and your proposed next responsibility.", history)
