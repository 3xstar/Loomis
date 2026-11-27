from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


from .i18n import status_label, translate


PROJECT_ROWS = (
    ("Общий чат ИИ", "Личный чат с ИИ"),
    ("Агенты проекта", "Обсуждение плана"),
    ("▶️ Старт / Пауза", "⚙️ Настройки проекта"),
    ("Статистика", "Все проекты"),
    ("История коммитов", "Push в Git"),
    ("Очистить экран чата",),
)
PROJECT_BUTTONS = {
    translate(label, language): label
    for row in PROJECT_ROWS for label in row for language in ("ru", "en")
}


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


def project_mode_keyboard() -> InlineKeyboardMarkup:
    return inline_menu([
        [("🖥 Создать для ПК", "project_mode:desktop")],
        [("📱 Сделать полностью в Telegram", "project_mode:mobile")],
        [("Отмена", "cancel")],
    ])


def review_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return inline_menu([
        [("✅ Подтвердить завершение", f"project_action:{project_id}:confirm_completion")],
        [("Продолжить разработку", f"review_continue:{project_id}")],
    ])


def pause_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return inline_menu([[("⏸ Пауза", f"project_action:{project_id}:pause")]])


def main_menu(account_exists: bool) -> InlineKeyboardMarkup:
    if not account_exists:
        return auth_menu()
    else:
        rows = [[InlineKeyboardButton(text="➕ Создать проект", callback_data="new_project")],
                [InlineKeyboardButton(text="Мои проекты", callback_data="projects")],
                [InlineKeyboardButton(text="Хаб нейросетей", callback_data="hub")],
                [InlineKeyboardButton(text="⚙️ Настройки аккаунта", callback_data="profile")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard(github_linked: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отвязать GitHub" if github_linked else "Войти через GitHub",
                              callback_data="profile:unlink" if github_linked else "profile:github")],
        [InlineKeyboardButton(text="Изменить общую папку для проектов", callback_data="profile:folder")],
        [InlineKeyboardButton(text="Папка сохранения на телефоне", callback_data="profile:mobile_folder")],
        [InlineKeyboardButton(text="Переключить рабочие логи в Telegram", callback_data="profile:logs")],
        [InlineKeyboardButton(text="Язык / Language", callback_data="profile:language")],
        [InlineKeyboardButton(text="Изменить логин", callback_data="profile:new_username")],
        [InlineKeyboardButton(text="Электронная почта", callback_data="profile:email")],
        [InlineKeyboardButton(text="Задать новый пароль", callback_data="profile:new_password")],
        [InlineKeyboardButton(text="Мои проекты", callback_data="projects")],
    ])


def auth_menu(*, desktop: bool = False) -> InlineKeyboardMarkup:
    rows = [[("Войти по логину и паролю", "auth:login")]]
    if not desktop:
        rows.insert(0, [("Войти через Telegram", "auth:telegram")])
        rows.append([("Войти через GitHub", "auth:github")])
    rows.append([("Зарегистрироваться", "auth:register")])
    if not desktop:
        rows.append([("Забыли пароль?", "auth:reset")])
    if desktop:
        rows.append([("Отмена", "cancel")])
    return inline_menu(rows)


def profile_setup_keyboard() -> InlineKeyboardMarkup:
    return inline_menu([
        [("Задать логин и пароль", "profile:setup"), ("Пропустить", "profile:setup_skip")],
    ])


def telegram_login_keyboard(code: str, *, existing: bool = True) -> InlineKeyboardMarkup:
    rows = [[("Подтвердить вход в Desktop", f"approve:{code}")]]
    if not existing:
        rows.append([("Войти по логину и паролю", "auth:login")])
    rows.append([("Отклонить", f"reject:{code}")])
    return inline_menu(rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ]])


def spec_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Фрагменты ТЗ", callback_data=f"spec_items:{project_id}:0")],
        [InlineKeyboardButton(text="✅ Собрать ТЗ проекта", callback_data=f"build_spec:{project_id}")],
        [InlineKeyboardButton(text="❌ Отменить проект", callback_data=f"cancel_project:{project_id}")],
    ])


def projects_keyboard(projects) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p.name} · {status_label(p.status)}", callback_data=f"project:{p.id}")]
            for p in projects]
    rows.append([InlineKeyboardButton(text="➕ Создать проект", callback_data="new_project")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def project_keyboard(project) -> InlineKeyboardMarkup:
    project_id = project.id
    rows = []
    if project.status == "review_pending":
        rows.extend(review_keyboard(project_id).inline_keyboard)
    if project.status == "draft":
        rows.append([InlineKeyboardButton(text="Открыть материалы ТЗ", callback_data=f"open_buffer:{project_id}")])
    else:
        rows.extend([
            [InlineKeyboardButton(text="Добавить CLI-агента", callback_data=f"agent_add:{project_id}:cli"),
             InlineKeyboardButton(text="Добавить API-агента", callback_data=f"agent_add:{project_id}:api")],
            [InlineKeyboardButton(text="Чат с агентом", callback_data=f"agent_list:{project_id}"),
             InlineKeyboardButton(text="Общий чат", callback_data=f"team_chat:{project_id}")],
        ])
        actions = {
            "planning": [("Составить план", "council")],
            "awaiting_approval": [("Утвердить план и запустить", "approve")],
            "council": [("⏸ Пауза", "pause")],
            "running": [("⏸ Пауза", "pause")],
            "paused": [("Возобновить", "resume"), ("⏪ Откатить шаг", "rollback")],
            "completed": [("↩️ Вернуться к проекту", "reopen_project"),
                          ("⏪ Откатить шаг", "rollback")],
        }
        for label, action in actions.get(project.status, []):
            rows.append([InlineKeyboardButton(text=label, callback_data=f"project_action:{project_id}:{action}")])
    if project.delivery_mode == "mobile" and project.status in {"review_pending", "completed"}:
        rows.append([InlineKeyboardButton(text="Отправить архив", callback_data=f"mobile_result:{project_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows + [
        [InlineKeyboardButton(text="Посмотреть ТЗ проекта", callback_data=f"view_spec:{project_id}")],
        [InlineKeyboardButton(text="Удалить проект", callback_data=f"delete_project:{project_id}")],
        [InlineKeyboardButton(text="Мои проекты", callback_data="projects")],
    ])



def agent_chat_keyboard(project_id, agents) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=agent.name, callback_data=f"agent_chat:{project_id}:{agent.id}")]
        for agent in agents
    ] + [[InlineKeyboardButton(text="К проекту", callback_data=f"project:{project_id}")]])
