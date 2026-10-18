import asyncio
import errno
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient
from app.pipeline import run_chat
from app.schemas import ChatRequest
from chat_fakes import FakeServices

KEY = "RETRY_PRIVATE_KEY_71"
QUESTION = "RETRY_PRIVATE_QUESTION_72"
ANSWER = "RETRY_PRIVATE_ANSWER_73"
PROMPT = "RETRY_PRIVATE_PROMPT_74"


def settings(**overrides):
    return Settings(_env_file=None, llm_api_key=KEY, llm_base_url=f"https://retry.example/v1/{KEY}", **overrides)


async def complete(client, model="retry-model"):
    return await client.complete(model=model, messages=[{"role": "system", "content": PROMPT},
                                                       {"role": "user", "content": QUESTION}],
                                 temperature=0.2, max_tokens=20)


def success():
    return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})


@pytest.mark.parametrize("mode", ["connect", "reset", "read_reset", "caused_reset", "503", "429"])
def test_retryable_failure_then_success_and_safe_retry_warning(mode, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) > 1:
            return success()
        if mode == "connect":
            raise httpx.ConnectError(KEY + QUESTION, request=request)
        if mode == "reset":
            raise ConnectionResetError(errno.ECONNRESET, KEY + QUESTION)
        if mode == "read_reset":
            raise httpx.ReadError("connection reset " + KEY + QUESTION, request=request)
        if mode == "caused_reset":
            try:
                raise OSError(errno.ECONNRESET, KEY)
            except OSError as exc:
                raise httpx.ReadError(QUESTION, request=request) from exc
        return httpx.Response(int(mode), json={"error": {"message": KEY + QUESTION + ANSWER + PROMPT}})
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await complete(client, model=KEY + QUESTION) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    sleep.assert_awaited_once_with(0.5)
    assert all(json.loads(call.content)["messages"][1]["content"] == QUESTION for call in calls)
    records = [record for record in caplog.records if record.name == "app.llm.client" and record.getMessage().startswith("llm_retry ")]
    assert len(records) == 1
    info = json.loads(records[0].getMessage().removeprefix("llm_retry "))
    assert info == {"category": "http_status" if mode.isdecimal() else "network", "host": "retry.example",
                    "model": "******", "http_status": int(mode) if mode.isdecimal() else None, "retry": 1}
    application_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("app."))
    for private in (KEY, QUESTION, ANSWER, PROMPT):
        assert private not in application_logs


@pytest.mark.parametrize("mode,code", [("connect", 50201), ("503", 50201), ("429", 42901)])
@pytest.mark.parametrize("retries", [0, 2])
def test_retry_limit_exhaustion_preserves_error_mapping_and_exponential_backoff(mode, code, retries):
    calls = []
    def handler(request):
        calls.append(request)
        if mode == "connect":
            raise httpx.ConnectError(KEY + QUESTION, request=request)
        return httpx.Response(int(mode), text=KEY + QUESTION)
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=retries), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert caught.value.code == code
            assert caught.value.http_status == (429 if code == 42901 else 502)
            assert KEY not in str(caught.value) + str(caught.value.diagnostic)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == retries + 1
    assert [call.args[0] for call in sleep.await_args_list] == [0.5 * 2 ** i for i in range(retries)]


@pytest.mark.parametrize("mode,code", [("timeout", 50401), ("connect_timeout", 50401), ("wall_timeout", 50401),
                                      ("400", 50201), ("401", 50201), ("404", 50201), ("302", 50201),
                                      ("bad_json", 50201), ("empty", 50201), ("read_other", 50201)])
def test_nonretryable_failures_never_sleep_or_repeat(mode, code):
    calls = []
    async def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout(KEY + QUESTION, request=request)
        if mode == "connect_timeout":
            raise httpx.ConnectTimeout(KEY, request=request)
        if mode == "wall_timeout":
            await asyncio.Event().wait()
        if mode == "read_other":
            raise httpx.ReadError("other read failure " + KEY, request=request)
        if mode == "bad_json":
            return httpx.Response(200, text=KEY + QUESTION)
        if mode == "empty":
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        return httpx.Response(int(mode))
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=3, llm_timeout_s=0.02),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert caught.value.code == code
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("mode,code", [("timeout", 50401), ("bad_json", 50201), ("401", 50201)])
def test_nonretryable_second_attempt_stops_even_with_more_retries_available(mode, code):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        if mode == "timeout":
            raise httpx.ReadTimeout(KEY, request=request)
        return httpx.Response(200, text=KEY) if mode == "bad_json" else httpx.Response(401)
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=3), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert caught.value.code == code
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    sleep.assert_awaited_once_with(0.5)


def test_retry_backoff_holds_one_shared_concurrency_slot_and_releases_after_success():
    async def run():
        backoff, release = asyncio.Event(), asyncio.Event()
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503) if len(calls) == 1 else success()
        async def sleep(delay):
            assert delay == 0.25
            backoff.set()
            await release.wait()
        configured = settings(llm_max_concurrency=1, llm_retry_backoff_s=0.25)
        first = LLMClient(configured, transport=httpx.MockTransport(handler), sleep=sleep)
        second = LLMClient(configured, transport=httpx.MockTransport(handler))
        tasks = []
        try:
            tasks.append(asyncio.create_task(complete(first)))
            await asyncio.wait_for(backoff.wait(), 1)
            tasks.append(asyncio.create_task(complete(second)))
            await asyncio.sleep(0)
            assert len(calls) == 1 and not any(task.done() for task in tasks)
            release.set()
            assert await asyncio.wait_for(asyncio.gather(*tasks), 1) == [ANSWER, ANSWER]
            assert len(calls) == 3  # first 的两次尝试 + second 的一次。
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await first.aclose()
            await second.aclose()
    asyncio.run(run())


def test_whole_request_timeout_cancels_backoff_before_second_attempt():
    async def run():
        entered = asyncio.Event()
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ConnectError(KEY, request=request)
        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()
        configured = settings(request_timeout_s=0.1, speculative_retrieval=False)
        client = LLMClient(configured, transport=httpx.MockTransport(handler), sleep=sleep)
        services = FakeServices()
        async def route(*args):
            await complete(client)
            raise AssertionError("timeout must interrupt backoff")
        services.route = route
        try:
            with pytest.raises(AppError) as caught:
                await run_chat(ChatRequest(session_id="retry-timeout", message=QUESTION),
                               settings=configured, services=services)
            assert caught.value.code == 50401 and entered.is_set()
            assert len(calls) == 1
        finally:
            await client.aclose()
    asyncio.run(run())
