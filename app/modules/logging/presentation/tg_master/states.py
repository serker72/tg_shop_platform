from aiogram.fsm.state import State, StatesGroup


class LogsSG(StatesGroup):
    list = State()


__all__ = ["LogsSG"]
