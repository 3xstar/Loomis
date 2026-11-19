from aiogram.fsm.state import State, StatesGroup


class ProjectSetup(StatesGroup):
    name = State()
    spec_buffer = State()


class AccountSetup(StatesGroup):
    username = State()
    link_code = State()


class AgentInteraction(StatesGroup):
    configure = State()
    question = State()


class ProjectSettings(StatesGroup):
    deadline = State()


class HubSetup(StatesGroup):
    name = State()
    adapter = State()
    base_url = State()
    api_key = State()
    model = State()
