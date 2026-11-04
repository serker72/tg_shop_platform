import importlib


def test_app_package_importable() -> None:
    module = importlib.import_module("app")
    assert module.__name__ == "app"
