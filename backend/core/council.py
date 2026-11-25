import asyncio
import re
from pathlib import Path

from worker.sandbox import workspace_file


def autonomy_instruction(mode):
    if mode == "autonomous":
        return ("Работай автономно до готового результата. Не задавай вопросов пользователю "
                "и не выводи LOOMIS_QUESTION. Самостоятельно решай неоднозначности, выполняй "
                "команды, проверяй результат и собирай требуемые итоговые файлы, включая DOCX "
                "через python-docx или pandoc, если этого требует задание.")
    if mode in {"consultative", "with_questions"}:
        return ("Используй отдельную строку LOOMIS_QUESTION: <вопрос> только на критической "
                "развилке ТЗ, где ни одно разумное допущение не позволяет продолжить.")
    return "Выполни текущий шаг целиком и передай результат для паузы между шагами."


def scheduled_steps(plan):
    return [tuple(part.strip() for part in match.groups()) for match in re.finditer(
        r"(?m)^- \[loomis\] @([^|\n]+)\|([^|\n]+)\|([^\n]+)$", plan,
    )]


def validate_steps(workspace, steps, roster, artists):
    paths = set()
    for executor, path, description in steps:
        if executor not in roster or not description:
            raise ValueError(f"Некорректный шаг плана для @{executor}.")
        if executor in artists:
            target = workspace_file(workspace, path)
            if target.suffix.lower() != ".png" or target in paths:
                raise ValueError(f"Для @{executor} требуется уникальный путь к PNG: {path}")
            paths.add(target)
        elif path != "-":
            raise ValueError(f"Для текстового шага @{executor} укажи «-» вместо пути.")


async def starting_council(workspace: Path, agents, feedback: str | None = None, *,
                           spec, previous="", publish=None, roster=None, synthesize=None, rank=None, ask=None,
                           artists=(), autonomy_mode="autonomous"):
    roster = roster or [name for name, _ in agents]
    order = "\n".join(f"{index}. {name}" for index, name in enumerate(roster, 1))
    context = f"{spec}\n\nОчередь исполнителей:\n{order}"
    if autonomy_mode == "autonomous":
        context += "\n\nДля плана разрешай неоднозначности самостоятельно. Не обращайся с вопросами к пользователю."
    if artists:
        context += "\n\nГенераторы изображений: " + ", ".join(f"@{name}" for name in artists)
        context += ("\nОни создают PNG по описанию. Если для проекта нужны иконки, логотипы или "
                    "иллюстрации, запланируй их создание до шагов, которые используют эти файлы.")
    if previous:
        context += f"\n\nПредыдущий план:\n{previous}"
    if feedback:
        context += f"\n\nПравки пользователя:\n{feedback}"

    async def discuss(name, agent, number, history, step_title):
        peers = ", ".join(f"@{peer}" for peer, _ in agents if peer != name)
        if number == 1:
            request = ("Предложи порядок работ и свою зону ответственности по техническому заданию.")
        else:
            request = ("Ответь на адресованные тебе вопросы из первого раунда. Поимённо согласуй "
                       "строгую очередность, участки ответственности и условия передачи работы.")
        prompt = (
            f"Раунд {number}. Ты {name}. Участники для обращения: {peers or 'ты один'}. "
            f"Обращайся через @Имя. {request} "
            "Максимум 3 коротких пункта: Сделано, Файлы, Следующий шаг."
        )
        round_context = f"{context}\n\n{history}"
        result = await (ask(agent, prompt, round_context, step_title) if ask else
                        agent.ask_question(prompt, round_context))
        if autonomy_mode == "autonomous" and "LOOMIS_QUESTION" in result.message:
            raise ValueError("Ответ нарушил правила автономного режима.")
        if result.status != "success" or not result.message.strip():
            raise ValueError(f"Нет ответа в раунде {number} от агента {name}: {result.message}")
        if publish:
            await publish(agent, f"**Раунд {number}**\n\n{result.message}", step_title)
        return f"## {name}\n{result.message}"

    history = ""
    active = list(agents)
    failed = set()
    for number in (1, 2):
        step_title = f"Стартовый консилиум: раунд {number}"
        if rank:
            ranked = await rank(active)
            failed.update({name for name, _ in active} - {name for name, _ in ranked})
            active = ranked
        outcomes = await asyncio.gather(
            *(discuss(name, agent, number, history, step_title) for name, agent in active), return_exceptions=True
        )
        replies = []
        for (name, agent), outcome in zip(active, outcomes):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                failed.add(name)
                if publish:
                    await publish(agent, f"Агент выбыл из консилиума. {outcome}", step_title)
            else:
                replies.append(outcome)
        active = [(name, agent) for name, agent in active if name not in failed]
        if not active:
            raise ValueError("Ни один агент не завершил раунд консилиума.")
        history += f"\n\n# Раунд {number}\n" + "\n\n".join(replies)
    scope = "Сохрани не затронутые правками задачи и зоны ответственности остальных агентов." if previous else ""
    plan_prompt = (
        "Собери полный план на основе двух раундов. Распредели задачи и файлы по именам агентов, "
        "устрани пересечения, придерживайся заданной очереди и укажи критерии проверки. "
        f"Учти правки пользователя. {scope} Верни только Markdown."
    )
    plan_prompt += (
        "\nДобавь упорядоченный список всех исполняемых шагов "
        "в формате `- [loomis] @Имя | путь | описание`, по одному шагу на строку. "
        "Для текстового шага укажи `-` вместо пути, для генератора — уникальный относительный путь "
        "к PNG (например, assets/logo.png) и самостоятельное визуальное описание для промпта. "
        "Используй точные имена из актуальной очереди и отдельный шаг для каждого изображения. "
    )
    step_title = "Правка плана по замечанию" if feedback else "Формирование плана"
    for name, lead in (await rank(active) if rank else active):
        order = "\n".join(f"{index}. {name}" for index, name in enumerate(
            (member for member in roster if member not in failed), 1,
        ))
        plan_context = f"{context}\n\n{history}\n\nАктуальная очередь:\n{order}"
        plan = await (synthesize(lead, plan_prompt, plan_context, step_title) if synthesize else
                      lead.ask_question(plan_prompt, plan_context))
        if (plan.status == "success" and plan.message.strip() and
                not (autonomy_mode == "autonomous" and "LOOMIS_QUESTION" in plan.message)):
            try:
                steps = scheduled_steps(plan.message)
                if not steps:
                    raise ValueError("План не содержит исполняемых шагов.")
                validate_steps(workspace, steps, set(roster) - failed, artists)
            except (ValueError, OSError) as exc:
                if publish:
                    await publish(lead, str(exc), step_title)
            else:
                break
        failed.add(name)
        if publish:
            detail = "" if autonomy_mode == "autonomous" else f" {plan.message}"
            await publish(lead, f"Не удалось собрать план.{detail}", step_title)
    else:
        raise ValueError("Не удалось собрать единый план.")
    if publish:
        await publish(lead, plan.message, step_title)
    return f"# Очередь исполнителей\n\n{order}\n\n{plan.message}"


async def periodic_meeting(previous: str, agents, diff: str, next_agent: str, *, autonomous=False):
    history = f"{previous[-16000:]}\n\nGit diff:\n{diff[:50000]}"
    if autonomous:
        history += "\n\nНе обращайся с вопросами к пользователю. Решай неоднозначности внутри команды."
    reports = await asyncio.gather(*(agent.participate_in_meeting(history) for _, agent in agents))
    summary = "\n\n".join(f"## {name} ({result.status})\n{result.message}"
                          for (name, _), result in zip(agents, reports)
                          if not autonomous or "LOOMIS_QUESTION" not in result.message)
    summary = f"# Собрание\n\nСледующий исполнитель — {next_agent}.\n\n{summary}"
    return summary
