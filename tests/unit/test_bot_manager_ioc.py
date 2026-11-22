from app.modules.bot_manager.ioc import BotManagerProvider


def test_bot_manager_provider_does_not_create_rabbit_broker() -> None:
    """RabbitBroker is owned by the scheduler, not BotManagerProvider."""
    assert not hasattr(BotManagerProvider, "provide_broker")
