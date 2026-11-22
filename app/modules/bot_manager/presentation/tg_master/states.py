from aiogram.fsm.state import State, StatesGroup


class BotsSG(StatesGroup):
    list = State()
    add_instruction = State()
    add_token = State()
    card = State()
    replace_token = State()
    confirm = State()


__all__ = ["BotsSG"]
