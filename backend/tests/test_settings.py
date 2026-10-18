import ast
import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient
from app.main import create_app
from chat_fakes import FakeServices
from settings_helpers import clear_app_environment


@pytest.mark.parametrize("mode", ["legacy", "current", "both"])
def test_llm_environment_names_priority_and_safe_deprecation(monkeypatch, caplog, mode):
    caplog.set_level(logging.WARNING, logger="app.config")
    old_key, new_key = "LEGACY_PRIVATE_KEY_91", "CURRENT_PRIVATE_KEY_92"
    old_url, new_url = "https://legacy.example/private-old", "https://current.example/private-new"
    if mode in {"legacy", "both"}:
        monkeypatch.setenv("QWEN_API_KEY", old_key)
        monkeypatch.setenv("QWEN_BASE_URL", old_url)
    if mode in {"current", "both"}:
        monkeypatch.setenv("LLM_API_KEY", new_key)
        monkeypatch.setenv("LLM_BASE_URL", new_url)
    settings = Settings(_env_file=None)
    assert settings.llm_api_key.get_secret_value() == (old_key if mode == "legacy" else new_key)
    assert settings.llm_base_url == (old_url if mode == "legacy" else new_url)
    assert not any(name.startswith("legacy_llm_") for name in settings.model_dump())
    warnings = [record for record in caplog.records if record.name == "app.config"]
    assert len(warnings) == (0 if mode == "current" else 1)
    if mode == "legacy":
        assert warnings[0].getMessage() == "QWEN_* 配置名已弃用，请改用 LLM_*"
    if mode == "both":
        assert "冲突" in warnings[0].getMessage()
    assert all(value not in caplog.text for value in (old_key, new_key, old_url, new_url))


def test_legacy_url_alone_warns_without_logging_value(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "CURRENT_PRIVATE_KEY_93")
    monkeypatch.setenv("QWEN_BASE_URL", "https://legacy.example/private-path")
    settings = Settings(_env_file=None)
    assert settings.llm_base_url == "https://legacy.example/private-path"
    assert "配置名已弃用" in caplog.text
    assert "private-path" not in caplog.text and "CURRENT_PRIVATE_KEY_93" not in caplog.text


def test_blank_current_key_falls_back_while_nonempty_current_url_wins(monkeypatch, caplog):
    monkeypatch.setenv("QWEN_API_KEY", "LEGACY_PRIVATE_KEY_94")
    monkeypatch.setenv("QWEN_BASE_URL", "https://legacy.example")
    settings = Settings(_env_file=None, llm_api_key="", llm_base_url="https://explicit.example")
    assert settings.llm_api_key.get_secret_value() == "LEGACY_PRIVATE_KEY_94"
    assert settings.llm_base_url == "https://explicit.example"
    assert "配置名已弃用" in caplog.text


def test_empty_current_key_uses_valid_legacy_key(monkeypatch, caplog):
    monkeypatch.setenv("QWEN_API_KEY", "LEGACY_PRIVATE_KEY_95")
    monkeypatch.setenv("LLM_API_KEY", "")
    assert Settings(_env_file=None).llm_api_key.get_secret_value() == "LEGACY_PRIVATE_KEY_95"
    assert "配置名已弃用" in caplog.text


def test_blank_current_url_uses_valid_legacy_and_equal_values_do_not_conflict(monkeypatch, caplog):
    monkeypatch.setenv("LLM_BASE_URL", " ")
    monkeypatch.setenv("QWEN_BASE_URL", "https://legacy.test/private-path")
    monkeypatch.setenv("LLM_API_KEY", "same-private-key")
    monkeypatch.setenv("QWEN_API_KEY", "same-private-key")
    settings = Settings(_env_file=None)
    assert settings.llm_base_url == "https://legacy.test/private-path"
    assert "冲突" not in caplog.text and "已弃用" in caplog.text
    assert "private-path" not in caplog.text and "same-private-key" not in caplog.text


def test_unconfigured_health_and_client_do_not_use_network(monkeypatch):
    settings = Settings(_env_file=None, warmup_on_start=False)
    assert settings.llm_api_key.get_secret_value() == ""
    app = create_app(settings, services=FakeServices())

    async def refused(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr("app.main.asyncio.open_connection", refused)
    with TestClient(app) as client:
        assert client.get("/health").json()["llm"] == "unconfigured"

    def forbidden(request):
        pytest.fail("unconfigured client must not call transport")

    async def run():
        client = LLMClient(settings, transport=httpx.MockTransport(forbidden))
        try:
            with pytest.raises(AppError, match="模型服务未配置") as caught:
                await client.complete(model=settings.router_model, messages=[], temperature=0, max_tokens=8)
            assert caught.value.code == 50201
        finally:
            await client.aclose()

    asyncio.run(run())


def test_test_configuration_removes_fake_environment(monkeypatch, tmp_path):
    # 与 autouse fixture 共用清理函数，模拟继承了开发者终端的全部配置。
    for name in Settings.model_fields:
        monkeypatch.setenv(name.upper(), "INVALID_PRIVATE_ENV_VALUE")
    monkeypatch.setenv("QWEN_API_KEY", "LEGACY_PRIVATE_ENV_VALUE")
    monkeypatch.setenv("QWEN_BASE_URL", "https://unexpected.example")
    clear_app_environment(monkeypatch, Settings.model_fields)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    settings = Settings(_env_file=None)
    assert settings.llm_api_key.get_secret_value() == ""
    assert settings.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.router_model == "qwen-turbo" and settings.generate_model == "qwen-plus"
    assert settings.llm_extra_body == {} and settings.faq_threshold == 0.9
    assert settings.mysql_host == "127.0.0.1" and settings.allow_trace is False
    assert Path(settings.log_dir) == tmp_path / "logs"


def test_all_test_settings_constructors_explicitly_disable_env_files():
    for path in Path(__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name == "Settings":
                keywords = {kw.arg: kw.value for kw in node.keywords}
                value = keywords.get("_env_file")
                assert isinstance(value, ast.Constant) and value.value is None, f"{path.name}:{node.lineno}"


def test_performance_config_defaults_and_zero_char_limit():
    settings = Settings(_env_file=None, rerank_max_chars=0)
    assert settings.speculative_retrieval is True
    assert settings.rerank_candidates == 10 and settings.router_max_tokens == 64
    assert settings.rerank_max_length == 512 and settings.rerank_batch_size == 16
    assert settings.rerank_max_chars == 0
    assert settings.rerank_text_mode == "child" and settings.rerank_fp16 is False
    assert settings.generate_max_tokens == 600
    assert settings.llm_max_retries == 1 and settings.llm_retry_backoff_s == 0.5


@pytest.mark.parametrize("field,value", [("rerank_candidates", 0), ("rerank_max_length", 0),
                                        ("rerank_batch_size", 0), ("rerank_max_chars", -1),
                                        ("rerank_text_mode", "invalid"), ("llm_max_retries", -1),
                                        ("llm_retry_backoff_s", -0.1)])
def test_performance_config_rejects_invalid_limits(field, value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
