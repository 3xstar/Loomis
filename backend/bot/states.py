from aiogram.fsm.state import State, StatesGroup


class ProjectSetup(StatesGroup):
    mode = State()
    name = State()
    workspace = State()
    git = State()
    options = State()
    deadline = State()
    spec_buffer = State()
    spec_edit = State()


class AccountSetup(StatesGroup):
    username = State()
    password = State()
    new_password = State()
    link_code = State()
    projects_dir = State()
    mobile_download_dir = State()


class AgentInteraction(StatesGroup):
    role = State()
    configure = State()
    question = State()


class ProjectSettings(StatesGroup):
    name = State()
    deadline = State()
    workspace = State()
    git = State()
    git_visibility = State()


class HubSetup(StatesGroup):
    role = State()
    name = State()
    adapter = State()
    base_url = State()
    api_key = State()
    model = State()
