from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


PROJECT_ROWS = (
    ("Общий чат ИИ", "Личный чат с ИИ"),
    ("Агенты проекта", "План и Консилиум"),
    ("▶️ Старт / Пауза", "⚙️ Настройки проекта"),
    ("Статистика", "Все проекты"),
    ("История коммитов", "Push в Git"),
    ("Очистить экран чата",),
)
PROJECT_BUTTONS = {label for row in PROJECT_ROWS for label in row}


def project_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in PROJECT_ROWS],
        resize_keyboard=True,
        is_persistent=True,
    )


def inline_menu(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


def main_menu(account_exists: bool) -> InlineKeyboardMarkup:
    if not account_exists:
        rows = [[InlineKeyboardButton(text="⚡ Быстрая регистрация", callback_data="quick_register")],
                [InlineKeyboardButton(text="Привязать по коду", callback_data="link_account")]]
    else:
        rows = [[InlineKeyboardButton(text="➕ Новый проект", callback_data="new_project")],
                [InlineKeyboardButton(text="Мои проекты", callback_data="projects")],
                [InlineKeyboardButton(text="Хаб нейросетей", callback_data="hub")],
                [InlineKeyboardButton(text="⚙️ Настройки аккаунта", callback_data="profile")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard(github_linked: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отвязать GitHub" if github_linked else "Войти через GitHub",
                              callback_data="profile:unlink" if github_linked else "profile:github")],
        [InlineKeyboardButton(text="Изменить общую папку для проектов", callback_data="profile:folder")],
        [InlineKeyboardButton(text="Переключить рабочие логи в Telegram", callback_data="profile:logs")],
        [InlineKeyboardButton(text="🔑 Сгенерировать новый пароль", callback_data="profile:new_password")],
        [InlineKeyboardButton(text="Мои проекты", callback_data="projects")],
    ])


def magic_login_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить вход в Desktop", callback_data=f"magic_approve:{request_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"magic_reject:{request_id}")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ]])


def first_project_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Создать первый проект", callback_data="new_project")
    ]])


def spec_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ТЗ загружено — собрать SPEC.md", callback_data=f"build_spec:{project_id}")],
        [InlineKeyboardButton(text="❌ Отменить проект", callback_data=f"cancel_project:{project_id}")],
    ])


def projects_keyboard(projects) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p.name} · {p.status}", callback_data=f"project:{p.id}")]
            for p in projects]
    rows.append([InlineKeyboardButton(text="➕ Новый проект", callback_data="new_project")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def project_keyboard(project) -> InlineKeyboardMarkup:
    project_id = project.id
    rows = []
    if project.status == "draft":
        rows.append([InlineKeyboardButton(text="Открыть буфер ТЗ", callback_data=f"open_buffer:{project_id}")])
    else:
        rows.extend([
            [InlineKeyboardButton(text="Добавить CLI-агента", callback_data=f"agent_add:{project_id}:cli"),
             InlineKeyboardButton(text="Добавить API-агента", callback_data=f"agent_add:{project_id}:api")],
            [InlineKeyboardButton(text="Чат с агентом", callback_data=f"agent_list:{project_id}"),
             InlineKeyboardButton(text="Общий чат", callback_data=f"team_chat:{project_id}")],
        ])
        actions = {
            "planning": [("Запустить стартовый консилиум", "council")],
            "awaiting_approval": [("Утвердить план и запустить", "approve")],
            "running": [("Пауза", "pause")],
            "paused": [("Возобновить", "resume"), ("⏪ Откатить шаг", "rollback")],
            "completed": [("⏪ Откатить шаг", "rollback")],
        }
        for label, action in actions.get(project.status, []):
            rows.append([InlineKeyboardButton(text=label, callback_data=f"project_action:{project_id}:{action}")])
    return InlineKeyboardMarkup(inline_keyboard=rows + [
        [InlineKeyboardButton(text="Посмотреть SPEC.md", callback_data=f"view_spec:{project_id}")],
        [InlineKeyboardButton(text="Удалить проект", callback_data=f"delete_project:{project_id}")],
        [InlineKeyboardButton(text="Мои проекты", callback_data="projects")],
    ])



def agent_chat_keyboard(project_id, agents) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=agent.name, callback_data=f"agent_chat:{project_id}:{agent.id}")]
        for agent in agents
    ] + [[InlineKeyboardButton(text="К проекту", callback_data=f"project:{project_id}")]])
