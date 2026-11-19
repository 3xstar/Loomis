import asyncio
from pathlib import Path

from worker.sandbox import workspace_file


async def starting_council(workspace: Path, agents):
    spec = workspace_file(workspace, "SPEC.md").read_text(encoding="utf-8")
    proposals = await asyncio.gather(*(agent.ask_question(
        "Предложи свою роль, конкретные файлы и последовательность работ.", spec
    ) for _, agent in agents))
    if any(result.status != "success" or not result.message.strip() for result in proposals):
        failures = ", ".join(name for (name, _), result in zip(agents, proposals)
                             if result.status != "success" or not result.message.strip())
        raise ValueError(f"Нет предложений от агентов: {failures}")
    contributions = "\n\n".join(f"## {name}\n{result.message}" for (name, _), result in zip(agents, proposals))
    lead = agents[0][1]
    plan = await lead.ask_question(
        "Собери единый PLAN.md. Распредели задачи и файлы по именам агентов, устрани пересечения "
        "и укажи очередность и критерии проверки. Верни только Markdown.",
        f"{spec}\n\n{contributions}",
    )
    if plan.status != "success" or not plan.message.strip():
        raise ValueError("Не удалось собрать единый PLAN.md.")
    workspace_file(workspace, "PLAN.md").write_text(plan.message, encoding="utf-8")
    return plan.message


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
