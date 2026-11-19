import asyncio
from pathlib import Path
from uuid import uuid4

from worker.sandbox import workspace_file


async def starting_council(workspace: Path, agents, feedback: str | None = None):
    spec = workspace_file(workspace, "SPEC.md").read_text(encoding="utf-8")
    file = workspace_file(workspace, "PLAN.md")
    previous = file.read_text(encoding="utf-8") if file.is_file() else ""
    order = "\n".join(f"{index}. {name}" for index, (name, _) in enumerate(agents, 1))
    context = f"{spec}\n\nОчередь исполнителей:\n{order}"
    if previous:
        context += f"\n\nПредыдущий план:\n{previous}"
    if feedback:
        context += f"\n\nПравки пользователя:\n{feedback}"
    proposals = await asyncio.gather(*(agent.ask_question(
        "Предложи свою роль, конкретные файлы и шаги. Обсуди правки пользователя, если они есть.", context
    ) for _, agent in agents))
    if any(result.status != "success" or not result.message.strip() for result in proposals):
        failures = ", ".join(name for (name, _), result in zip(agents, proposals)
                             if result.status != "success" or not result.message.strip())
        raise ValueError(f"Нет предложений от агентов: {failures}")
    contributions = "\n\n".join(f"## {name}\n{result.message}" for (name, _), result in zip(agents, proposals))
    lead = agents[0][1]
    plan = await lead.ask_question(
        "Собери единый PLAN.md. Распредели задачи и файлы по именам агентов, устрани пересечения "
        "и согласуй работы с заданной очередью. Учти правки пользователя и укажи критерии проверки. "
        "Верни только Markdown.",
        f"{context}\n\n{contributions}",
    )
    if plan.status != "success" or not plan.message.strip():
        raise ValueError("Не удалось собрать единый PLAN.md.")
    content = f"# Очередь исполнителей\n\n{order}\n\n{plan.message}"
    pending = file.with_name(f".PLAN.{uuid4().hex}.tmp")
    try:
        pending.write_text(content, encoding="utf-8")
        pending.replace(file)
    finally:
        pending.unlink(missing_ok=True)
    return content


async def periodic_meeting(workspace: Path, agents, diff: str, next_agent: str):
    file = workspace_file(workspace, "Project_context.md")
    previous = file.read_text(encoding="utf-8") if file.exists() else ""
    history = f"{previous[-16000:]}\n\nGit diff:\n{diff[:50000]}"
    reports = await asyncio.gather(*(agent.participate_in_meeting(history) for _, agent in agents))
    summary = "\n\n".join(f"## {name} ({result.status})\n{result.message}"
                          for (name, _), result in zip(agents, reports))
    summary = f"# Собрание\n\nСледующий исполнитель — {next_agent}.\n\n{summary}"
    file.write_text(summary, encoding="utf-8")
    return summary
