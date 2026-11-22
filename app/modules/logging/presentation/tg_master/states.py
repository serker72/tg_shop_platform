from aiogram.fsm.state import State, StatesGroup


class LogsSG(StatesGroup):
    list = State()
    filters = State()
    category = State()
    choices = State()
    input = State()


__all__ = ["LogsSG"]
