from .api_adapter import ApiAgent
from .cli_adapter import CliAgent


def build_agent(provider, model, settings, approval=None):
    if provider.lower() == "codex":
        return CliAgent(
            settings.get("command", "codex exec --json --sandbox workspace-write -"),
            settings.get("question_command", "codex exec --json --skip-git-repo-check --sandbox read-only -"),
            approval,
        )
    if provider.lower() in {"cli", "opencode", "qwen", "claude"}:
        return CliAgent(settings.get("command", model), settings.get("question_command"), approval)
    return ApiAgent(model, settings["base_url"], settings.get("api_key_env"))
