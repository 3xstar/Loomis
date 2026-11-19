from aiogram.fsm.state import State, StatesGroup


class ProjectSetup(StatesGroup):
    name = State()
    spec_buffer = State()


class AccountSetup(StatesGroup):
    username = State()
    link_code = State()
