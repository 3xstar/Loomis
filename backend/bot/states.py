from aiogram.fsm.state import State, StatesGroup


class ProjectSetup(StatesGroup):
    name = State()
    workspace = State()
    git = State()
    options = State()
    deadline = State()
    spec_buffer = State()


class AccountSetup(StatesGroup):
    username = State()
    link_code = State()
    projects_dir = State()


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
