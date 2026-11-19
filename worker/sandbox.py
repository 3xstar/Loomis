import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path


Approval = Callable[[str], Awaitable[bool]]


def workspace_file(workspace: Path, path: str) -> Path:
    root = workspace.resolve(strict=True)
    if ":" in path or Path(path).is_absolute() or Path(path).anchor or any(
        part.casefold().rstrip(" .") == ".git" or Path(part).is_reserved()
        for part in Path(path).parts
    ):
        raise PermissionError("Expected a relative workspace path")
    target = (root / path).resolve()
    if not target.is_relative_to(root) or any(
        part.casefold().rstrip(' .') == '.git' for part in target.relative_to(root).parts
    ):
        raise PermissionError("Path is outside the allowed workspace")
    return target


async def authorize_command(command: str, workspace: Path, approval: Approval | None = None):
    workspace.resolve(strict=True)
    if not command.strip() or '^' in command or re.search(r'[;&|<>$`\r\n%!]|(?:^|[\s"\x27])(?:\.\.|[~/\\]|[a-z]:)', command, re.I):
        raise PermissionError("Shell operators and external paths are blocked")
    for argument in shlex.split(command):
        candidate = argument.split("=", 1)[-1]
        if candidate.startswith(("/", "\\", "~")) or re.match(r"^[a-z]:", candidate, re.I):
            raise PermissionError("Absolute and home paths are blocked")
        if "/" in candidate or "\\" in candidate or candidate in {".git", ".."}:
            workspace_file(workspace, candidate)
    if re.search(r'\b(rm|rmdir|rd|del|erase|format(?:\.com)?|mkfs|diskpart|remove-item)\b', command, re.I):
        if approval is None or not await approval(command):
            raise PermissionError("Master approval is required")
