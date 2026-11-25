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


COMPACT_PLAN = "## Компактная задача"


def is_compact_plan(plan):
    return COMPACT_PLAN in plan.splitlines()


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
            workspace_file(workspace, path)


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

    text_agents = [(name, agent) for name, agent in agents if name not in artists]
    active = await rank(text_agents) if rank else text_agents
    if not active:
        raise ValueError("Нет доступного агента для составления плана.")
    failed = {name for name, _ in text_agents} - {name for name, _ in active}
    scope = "Сохрани не затронутые правками задачи." if previous else ""
    plan_prompt = (
        "Составь полный план и верни только Markdown. Укажи критерии проверки. "
        f"Учти правки пользователя. {scope}\n"
        "Если ТЗ можно закрыть за 1–4 действия одним текстовым исполнителем, добавь отдельный "
        f"заголовок «{COMPACT_PLAN}». В таком плане назначь один шаг исполнения ведущему. "
        "Он сам пишет, запускает и проверяет результат. Если указан проверяющий, добавь ровно "
        "один последний шаг быстрой проверки ему, без правок файлов. "
        "Для сложной задачи распредели шаги и файлы по ролям без пересечений. "
        "Добавь все исполняемые шаги в формате `- [loomis] @Имя | путь | описание`. "
        "Для текстового шага укажи относительный путь к одному итоговому файлу, если его можно "
        "однозначно определить из ТЗ, иначе укажи «-». Для проверки укажи «-». "
        "Для генератора укажи уникальный путь к PNG и самостоятельное визуальное описание. "
        "Используй точные имена из актуальной очереди."
    )
    step_title = "Правка плана по замечанию" if feedback else "Формирование плана"
    for name, lead in active:
        order = "\n".join(f"{index}. {member}" for index, member in enumerate(
            (member for member in roster if member not in failed), 1,
        ))
        reviewer = next((member for member, _ in active if member != name and member not in failed), None)
        plan_context = (
            f"{context}\n\nАктуальная очередь:\n{order}\n"
            f"Ведущий: @{name}. Проверяющий: {('@' + reviewer) if reviewer else 'отсутствует'}."
        )
        correction = ""
        for _ in range(2):
            prompt = f"{plan_prompt}\n{correction}" if correction else plan_prompt
            plan = await (synthesize(lead, prompt, plan_context, step_title) if synthesize else
                          ask(lead, prompt, plan_context, step_title) if ask else
                          lead.ask_question(prompt, plan_context))
            if plan.status != "success":
                break
            if autonomy_mode == "autonomous":
                plan.message = re.sub(r"(?m)^.*LOOMIS_QUESTION.*$", "", plan.message).strip()
            try:
                steps = scheduled_steps(plan.message)
                if not steps:
                    raise ValueError("План не содержит исполняемых шагов.")
                validate_steps(workspace, steps, set(roster) - failed, artists)
                if is_compact_plan(plan.message):
                    expected = [name, reviewer] if reviewer else [name]
                    if [executor for executor, _, _ in steps] != expected:
                        raise ValueError("Компактный план должен содержать шаг ведущего и шаг проверяющего, если он назначен.")
                    if reviewer and steps[-1][1] != "-":
                        raise ValueError("В шаге проверяющего укажи «-» вместо пути.")
            except (ValueError, OSError) as exc:
                correction = f"Исправь план: {exc}"
            else:
                correction = ""
                break
        if plan.status == "success" and not correction:
            break
        failed.add(name)
        if publish:
            await publish(lead, correction or "Не удалось собрать план.", step_title)
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
