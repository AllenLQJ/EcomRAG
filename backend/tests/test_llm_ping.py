import asyncio
import json
import logging
from unittest.mock import Mock

import httpx
import pytest

from app import config
from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient
from eval import llm_ping

KEY = "PING_FAKE_KEY_NEVER_PRINT_81"
QUESTION = "PRIVATE_USER_QUESTION_72"


def settings(**overrides):
    values = dict(llm_api_key=KEY, llm_base_url=f"https://vendor.example/v1/{KEY}",
                  router_model="router-test", generate_model="generate-test", llm_max_retries=0)
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("mode,status,category", [
    ("200", 200, None), ("401", 401, "http_status"), ("404", 404, "http_status"),
    ("429", 429, "http_status"), ("503", 503, "http_status"),
    ("timeout", None, "timeout"), ("network", None, "network"),
    ("bad_response", 200, "bad_response"),
])
def test_ping_outputs_safe_diagnostics_for_both_models(mode, status, category, capsys, caplog):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["messages"] == [{"role": "user", "content": "回复ok"}]
        assert body["max_tokens"] == 64
        if mode == "timeout":
            raise httpx.ReadTimeout(KEY, request=request)
        if mode == "network":
            raise httpx.ConnectError(KEY, request=request)
        if mode == "bad_response":
            return httpx.Response(200, text=KEY)
        if mode == "200":
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok" + KEY + "文" * 40}}]})
        return httpx.Response(status, json={"error": {"code": "InvalidApiKey", "message": f"invalid {KEY} " + "诊" * 250}})
    code = llm_ping.main([], settings=settings(), transport=httpx.MockTransport(handler))
    output = capsys.readouterr().out
    assert KEY not in output + caplog.text
    lines = output.splitlines()
    configuration = json.loads(lines[0].removeprefix("配置："))
    assert configuration == {"host": "vendor.example", "router_model": "router-test", "generate_model": "generate-test",
                             "key_configured": True, "key_length": len(KEY)}
    results = [json.loads(line) for line in lines[1:]]
    assert len(results) == 2 and [call["model"] for call in calls] == ["router-test", "generate-test"]
    assert code == (0 if mode == "200" else 1)
    for result in results:
        assert result["ms"] >= 0
        if mode == "200":
            assert result["success"] is True and result["reply_length"] == len("ok" + KEY + "文" * 40)
            assert "reply" not in result and "文" * 20 not in output
        else:
            info = result["diagnostic"]
            assert result["success"] is False
            assert info["category"] == category and info["http_status"] == status
            assert info["exception_type"]
            assert info["host"] == "vendor.example" and info["model"] == result["model"]
            assert info["error_message"] is None or len(info["error_message"]) <= 200
            if category == "http_status":
                assert info["error_code"] == "InvalidApiKey" and "***" in info["error_message"]
            assert result["error_code"] == (42901 if mode == "429" else 50401 if mode == "timeout" else 50201)


def test_warning_redacts_provider_echoes_of_question_prompt_and_key(caplog):
    # 显式启用请求 INFO，确保只统计诊断 WARNING，不依赖此前测试的 root 级别。
    caplog.set_level(logging.INFO, logger="app.llm.client")
    messages = [{"role": "system", "content": "PRIVATE_SYSTEM_PROMPT_17"},
                {"role": "user", "content": f"<问题>\n{QUESTION}\n</问题>"}]
    def handler(request):
        return httpx.Response(401, json={"error": {"code": KEY,
            "message": f"bad auth {KEY}, echo {QUESTION}, PRIVATE_SYSTEM_PROMPT_17"}})
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(AppError, match="模型服务暂时不可用") as caught:
                await client.complete(model="router-test", messages=messages, temperature=0, max_tokens=8)
            assert caught.value.code == 50201
            info = caught.value.diagnostic
            assert info["category"] == "http_status" and info["http_status"] == 401
            assert info["error_code"] == "***"
        finally:
            await client.aclose()
    asyncio.run(run())
    records = [record for record in caplog.records if record.name == "app.llm.client"]
    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].getMessage().startswith("llm_error ")
    assert any(record.levelno == logging.INFO and record.getMessage().startswith("llm_request ")
               for record in records)
    application_logs = "\n".join(record.getMessage() for record in records)
    for value in (KEY, QUESTION, "PRIVATE_SYSTEM_PROMPT_17"):
        assert value not in application_logs


def test_ping_loads_runtime_settings_and_unconfigured_never_calls_transport(monkeypatch, capsys):
    loaded = settings(llm_api_key="")
    getter = Mock(return_value=loaded)
    monkeypatch.setattr(config, "get_settings", getter)
    def forbidden(request):
        pytest.fail("unconfigured must not call transport")
    assert llm_ping.main([], transport=httpx.MockTransport(forbidden)) == 1
    getter.assert_called_once_with()
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0].removeprefix("配置："))["key_length"] == 0
    assert all(json.loads(line)["diagnostic"]["category"] == "unconfigured" for line in lines[1:])


def test_direct_ping_total_timeout_and_no_reply_body(capsys):
    async def blocked(request):
        await asyncio.Event().wait()
    configured = settings(request_timeout_s=0.02, llm_timeout_s=1)
    assert llm_ping.main([], settings=configured, transport=httpx.MockTransport(blocked)) == 1
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()[1:]]
    assert len(rows) == 2 and all(row["error_code"] == 50401 for row in rows)
    assert all("reply" not in row and row["diagnostic"]["category"] == "timeout" for row in rows)
