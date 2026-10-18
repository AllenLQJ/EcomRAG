import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient, TRUNCATED_REASON
from app.llm.generator import Generator
from app.llm.router import Router
from eval.llm_ping import main as ping
from chat_fakes import document

KEY = "REASONING_FAKE_KEY_DO_NOT_LOG"
QUESTION = "PRIVATE_QUESTION_DO_NOT_LOG"
REASONING = "PRIVATE_REASONING_DO_NOT_LOG"
ANSWER = "PRIVATE_FINAL_ANSWER_DO_NOT_LOG"
EXTRA_VALUE = "PRIVATE_EXTRA_VALUE_DO_NOT_LOG"


def settings(**overrides):
    return Settings(_env_file=None, llm_api_key=KEY, **overrides)


def response(content, *, finish="stop", **fields):
    return {"choices": [{"finish_reason": finish, "message": {"content": content, **fields}}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 64,
                      "completion_tokens_details": {"reasoning_tokens": 64}}}


async def complete(data, *, configured=None, handler=None):
    client = LLMClient(configured or settings(), transport=httpx.MockTransport(handler or (
        lambda request: httpx.Response(200, json=data))))
    try:
        return await client.complete(model="reasoning-test", messages=[{"role": "user", "content": QUESTION}],
                                     temperature=0, max_tokens=64)
    finally:
        await client.aclose()


@pytest.mark.parametrize("content,fields,expected", [
    (ANSWER, {}, ANSWER),
    ([{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}], {}, "第一段第二段"),
    ([{"type": "reasoning", "text": REASONING}, {"type": "text", "text": ANSWER},
      {"type": "image", "url": REASONING}], {}, ANSWER),
    (ANSWER, {"reasoning_content": REASONING, "reasoning": REASONING}, ANSWER),
])
def test_string_and_segmented_content_never_use_reasoning(content, fields, expected, caplog):
    assert asyncio.run(complete(response(content, **fields))) == expected
    assert ANSWER not in caplog.text and REASONING not in caplog.text


@pytest.mark.parametrize("content", [None, "", " \n", []])
def test_empty_length_is_truncated_with_safe_structure(content, caplog):
    with pytest.raises(AppError) as caught:
        asyncio.run(complete(response(content, finish="length", reasoning_content=REASONING)))
    error = caught.value
    assert (error.code, error.http_status) == (50201, 502)
    assert error.message == "模型服务返回异常"  # C 端保持通用文案，具体原因进入诊断。
    info = error.diagnostic
    assert info["category"] == "truncated" and info["reason"] == TRUNCATED_REASON
    structure = info["response_structure"]
    assert structure == {"choices_count": 1, "finish_reason": "length",
                         "content_type": type(content).__name__ if content is not None else "None",
                         "content_empty": True, "has_reasoning_content": True, "has_reasoning": False,
                         "usage": {"prompt_tokens": 31, "completion_tokens": 64, "reasoning_tokens": 64}}
    for secret in (KEY, QUESTION, REASONING, ANSWER):
        assert secret not in caplog.text + json.dumps(info)
    assert '"category": "truncated"' in caplog.text


def test_reasoning_without_final_content_is_not_answer(caplog):
    with pytest.raises(AppError) as caught:
        asyncio.run(complete(response(None, reasoning=REASONING)))
    assert caught.value.diagnostic["category"] == "bad_response"
    structure = caught.value.diagnostic["response_structure"]
    assert structure["content_type"] == "None" and structure["content_empty"] is True
    assert structure["has_reasoning"] is True
    assert REASONING not in caplog.text


def test_nonempty_length_keeps_final_text():
    assert asyncio.run(complete(response(ANSWER, finish="length", reasoning_content=REASONING))) == ANSWER


@pytest.mark.parametrize("data,expected_count", [
    ({"choices": []}, 0), ({"choices": [{"message": None}]}, 1),
    ({"choices": [{"message": {"content": {"private": ANSWER}}}]}, 1),
    (None, None),
])
def test_bad_response_shape_is_logged_without_content(data, expected_count, caplog):
    with pytest.raises(AppError) as caught:
        asyncio.run(complete(data))
    info = caught.value.diagnostic
    assert info["category"] == "bad_response"
    assert info["response_structure"]["choices_count"] == expected_count
    assert ANSWER not in caplog.text


def test_extra_body_is_merged_but_only_top_level_keys_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="app.llm.client")
    configured = settings(llm_extra_body=json.dumps({"thinking": {"type": "disabled"}, "vendor_option": EXTRA_VALUE}))
    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["vendor_option"] == EXTRA_VALUE
        assert body["messages"] == [{"role": "user", "content": QUESTION}]
        # 错误回显参数值也必须被脱敏。
        return httpx.Response(400, json={"error": {"code": "BadOption", "message": EXTRA_VALUE + " disabled " + KEY}})
    with pytest.raises(AppError) as caught:
        asyncio.run(complete(None, configured=configured, handler=handler))
    assert caught.value.diagnostic["extra_body_keys"] == ["thinking", "vendor_option"]
    assert "thinking" in caplog.text and "vendor_option" in caplog.text
    for secret in ("disabled", EXTRA_VALUE, KEY, QUESTION):
        assert secret not in caplog.text
    assert EXTRA_VALUE not in repr(configured)


@pytest.mark.parametrize("stream", [True, "true", "True"])
def test_extra_body_cannot_enable_streaming(stream, caplog):
    configured = settings(llm_extra_body={"stream": stream})
    def handler(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json=response(ANSWER))
    assert asyncio.run(complete(None, configured=configured, handler=handler)) == ANSWER
    assert configured.llm_extra_body == {"stream": stream}
    assert all(value not in caplog.text for value in (ANSWER, QUESTION, KEY))


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", '"string"', '""', "true", '{"value":NaN}'])
def test_invalid_extra_body_has_clear_config_error_without_values(raw):
    with pytest.raises(ValidationError, match="LLM_EXTRA_BODY"):
        Settings(_env_file=None, llm_extra_body=raw)


def test_extra_body_environment_validation_and_empty_default(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"thinking":{"type":"disabled"}}')
    assert Settings(_env_file=None).llm_extra_body == {"thinking": {"type": "disabled"}}
    monkeypatch.setenv("LLM_EXTRA_BODY", "broken-" + EXTRA_VALUE)
    with pytest.raises(ValidationError, match="LLM_EXTRA_BODY") as caught:
        Settings(_env_file=None)
    assert EXTRA_VALUE not in str(caught.value)
    monkeypatch.setenv("LLM_EXTRA_BODY", "")
    assert Settings(_env_file=None).llm_extra_body == {}


@pytest.mark.parametrize("raw", ['""', '"{}"', "[]", "null"])
def test_non_object_json_in_environment_is_not_decoded_twice(raw, monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", raw)
    with pytest.raises(ValidationError, match="LLM_EXTRA_BODY"):
        Settings(_env_file=None)


def test_configured_token_limits_reach_router_and_both_generation_paths():
    configured = settings(router_max_tokens=320, generate_max_tokens=2048)
    fake = AsyncMock()
    fake.complete.return_value = '{"intent":"knowledge","rewritten_query":"问题"}'
    asyncio.run(Router(fake, configured.router_model, max_tokens=configured.router_max_tokens).route("问题", []))
    assert fake.complete.call_args.kwargs["max_tokens"] == 320
    generator = Generator(fake, configured.generate_model, max_tokens=configured.generate_max_tokens)
    fake.complete.return_value = "答案[1]"
    asyncio.run(generator.generate_answer("问题", [document()], []))
    assert fake.complete.call_args.kwargs["max_tokens"] == 2048
    asyncio.run(generator.chat_general("问题", []))
    assert fake.complete.call_args.kwargs["max_tokens"] == 2048
    assert Settings.model_fields["router_max_tokens"].default == 64
    assert Settings.model_fields["generate_max_tokens"].default == 600


def test_ping_prints_truncation_structure_without_any_response_text(capsys, caplog):
    configured = settings(llm_extra_body=json.dumps({"vendor_option": EXTRA_VALUE}))
    data = response(None, finish="length", reasoning_content=REASONING + KEY)
    assert ping([], settings=configured, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) == 1
    output = capsys.readouterr().out
    for result in [json.loads(line) for line in output.splitlines()[1:]]:
        assert result["diagnostic"]["category"] == "truncated"
        assert result["diagnostic"]["response_structure"]["usage"]["reasoning_tokens"] == 64
        assert result["diagnostic"]["reason"] == TRUNCATED_REASON
    for secret in (KEY, REASONING, ANSWER, EXTRA_VALUE):
        assert secret not in output + caplog.text
