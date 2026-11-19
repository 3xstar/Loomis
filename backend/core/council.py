import asyncio
from pathlib import Path
from uuid import uuid4

from worker.sandbox import workspace_file


async def starting_council(workspace: Path, agents, feedback: str | None = None, *,
                           publish=None, roster=None, synthesize=None):
    spec = workspace_file(workspace, "SPEC.md").read_text(encoding="utf-8")
    file = workspace_file(workspace, "PLAN.md")
    previous = file.read_text(encoding="utf-8") if file.is_file() else ""
    roster = roster or [name for name, _ in agents]
    order = "\n".join(f"{index}. {name}" for index, name in enumerate(roster, 1))
    context = f"{spec}\n\nОчередь исполнителей:\n{order}"
    if previous:
        context += f"\n\nПредыдущий план:\n{previous}"
    if feedback:
        context += f"\n\nПравки пользователя:\n{feedback}"

    async def discuss(name, agent, number, history):
        peers = ", ".join(f"@{peer}" for peer, _ in agents if peer != name)
        if number == 1:
            request = ("Представься, спроси коллег об их сильных сторонах применительно к SPEC.md "
                       "и предложи порядок работ и свою роль.")
        else:
            request = ("Ответь на адресованные тебе вопросы из первого раунда. Поимённо согласуй "
                       "строгую очередность, участки ответственности и условия передачи работы.")
        result = await agent.ask_question(
            f"Раунд {number}. Ты {name}. Участники для обращения: {peers or 'ты один'}. "
            f"Обращайся через @Имя. {request}", f"{context}\n\n{history}",
        )
        if result.status != "success" or not result.message.strip():
            raise ValueError(f"Нет ответа в раунде {number} от агента {name}: {result.message}")
        if publish:
            await publish(agent, f"**Раунд {number}**\n\n{result.message}")
        return f"## {name}\n{result.message}"

    history = ""
    for number in (1, 2):
        outcomes = await asyncio.gather(
            *(discuss(name, agent, number, history) for name, agent in agents), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        history += f"\n\n# Раунд {number}\n" + "\n\n".join(outcomes)
    lead = agents[0][1]
    scope = "Сохрани не затронутые правками задачи и зоны ответственности остальных агентов." if previous else ""
    plan_prompt = (
        "Собери полный PLAN.md на основе двух раундов. Распредели задачи и файлы по именам агентов, "
        "устрани пересечения, придерживайся заданной очереди и укажи критерии проверки. "
        f"Учти правки пользователя. {scope} Верни только Markdown."
    )
    plan = await (synthesize(lead, plan_prompt, f"{context}\n\n{history}") if synthesize else
                  lead.ask_question(plan_prompt, f"{context}\n\n{history}"))
    if plan.status != "success" or not plan.message.strip():
        raise ValueError("Не удалось собрать единый PLAN.md.")
    if publish:
        await publish(lead, plan.message)
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
