from pathlib import Path

from worker.adapters.cli_adapter import run_shell
from worker.sandbox import Approval, authorize_command


async def check_quality(command: str | None, workspace: Path, timeout: float, approval: Approval | None = None):
    if not command:
        return None
    try:
        await authorize_command(command, workspace, approval)
        code, out, err = await run_shell(command, workspace, timeout)
    except TimeoutError:
        return "Проверка проекта превысила дедлайн."
    if code:
        return (out + "\n" + err).strip() or f"Проверка завершилась с кодом {code}."
    return None
