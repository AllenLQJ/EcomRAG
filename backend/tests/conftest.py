import importlib

import pytest
from fastapi.testclient import TestClient

from app import config


@pytest.fixture
def app_factory(monkeypatch):
    # 测试不读取 .env，也不打开任何网络连接。
    def unexpected_settings_load():
        raise AssertionError("测试必须显式注入配置，不得加载 .env")

    monkeypatch.setattr(config, "get_settings", unexpected_settings_load)
    main = importlib.import_module("app.main")

    async def refused_connection(*args, **kwargs):
        raise ConnectionRefusedError("测试模拟依赖不可用")

    monkeypatch.setattr(main.asyncio, "open_connection", refused_connection)

    def build(**overrides):
        values = {"debug": False, "qwen_api_key": ""}
        values.update(overrides)
        settings = config.Settings(_env_file=None, **values)
        return main.create_app(settings)

    return build


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client
