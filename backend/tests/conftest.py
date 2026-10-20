import importlib
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import config
from chat_fakes import FakeServices
from settings_helpers import clear_app_environment


@pytest.fixture(autouse=True)
def isolated_log_dir(monkeypatch, tmp_path):
    # _env_file=None 只禁用环境文件，仍会读取进程环境，因此先隔离全部应用配置。
    clear_app_environment(monkeypatch, config.Settings.model_fields)
    config.get_settings.cache_clear()
    folder = tmp_path / "logs"
    monkeypatch.setenv("LOG_DIR", str(folder))
    previous_level = logging.getLogger().level
    yield folder
    # 避免处理器在下一个测试中继续向已结束测试的目录写入。
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_ecomrag", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(previous_level)
    # 某些测试会替换 getter，不能假定此刻它仍带有 cache_clear。
    clear_cache = getattr(config.get_settings, "cache_clear", None)
    if clear_cache is not None:
        clear_cache()


@pytest.fixture(autouse=True)
def offline_chat_logger(monkeypatch):
    # 所有 API 测试的默认记录器都是假对象，新增记录功能不能触发真实 DB。
    main = importlib.import_module("app.main")
    def factory(settings):
        from types import SimpleNamespace
        return SimpleNamespace(write=AsyncMock(), ensure_schema=AsyncMock())
    monkeypatch.setattr(main, "ChatLogWriter", factory)


@pytest.fixture(autouse=True)
def offline_account_io(monkeypatch):
    # 新增存储也必须离线；显式注入的 fake Redis 不受影响。
    main = importlib.import_module("app.main")
    account_class, quota_class = main.AccountService, main.DailyQuota

    def accounts(settings):
        service = account_class(settings)
        service.store.cache._client = lambda: service.store.cache.client
        return service

    def quota(settings):
        service = quota_class(settings)
        service.cache._client = lambda: service.cache.client
        return service

    monkeypatch.setattr(main, "AccountService", accounts)
    monkeypatch.setattr(main, "DailyQuota", quota)


@pytest.fixture
def account_factory():
    from account_fakes import build_account_app
    return build_account_app


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
        values = {"debug": False, "llm_api_key": ""}
        values.update(overrides)
        settings = config.Settings(_env_file=None, **values)
        app = main.create_app(settings, services=FakeServices())
        # 基础 API 测试使用隔离的进程内限流，禁止 Redis 建连。
        app.state.rate_limiter.cache._client = lambda: None
        app.state.rate_limiter.clock = lambda: 100000.0
        return app

    return build


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client
