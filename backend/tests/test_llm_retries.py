import asyncio
import errno
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.budget import deadline
from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient, _semaphore
from app.pipeline import run_chat
from app.schemas import ChatRequest
from chat_fakes import FakeServices
from deadline_helpers import ControlledDeadlines

KEY = "RETRY_PRIVATE_KEY_71"
QUESTION = "RETRY_PRIVATE_QUESTION_72"
ANSWER = "RETRY_PRIVATE_ANSWER_73"
PROMPT = "RETRY_PRIVATE_PROMPT_74"


@pytest.fixture(autouse=True)
def deterministic_jitter(monkeypatch):
    monkeypatch.setattr("app.llm.client.random.uniform", lambda low, high: 0)


def settings(**overrides):
    return Settings(_env_file=None, llm_api_key=KEY, llm_base_url=f"https://retry.example/v1/{KEY}", **overrides)


async def complete(client, model="retry-model"):
    return await client.complete(model=model, messages=[{"role": "system", "content": PROMPT},
                                                       {"role": "user", "content": QUESTION}],
                                 temperature=0.2, max_tokens=20)


def success():
    return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})


@pytest.mark.parametrize("mode", ["connect", "reset", "read_reset", "read_other", "protocol", "caused_reset", "503", "429",
                                 "timeout", "connect_timeout", "write_timeout", "pool_timeout", "wall_timeout"])
def test_retryable_failure_then_success_and_safe_retry_warning(mode, caplog):
    calls = []
    async def handler(request):
        calls.append(request)
        if len(calls) > 1:
            return success()
        if mode == "timeout":
            raise httpx.ReadTimeout(KEY + QUESTION, request=request)
        if mode == "connect_timeout":
            raise httpx.ConnectTimeout(KEY + QUESTION, request=request)
        if mode in {"write_timeout", "pool_timeout"}:
            error = httpx.WriteTimeout if mode == "write_timeout" else httpx.PoolTimeout
            raise error(KEY + QUESTION, request=request)
        if mode == "wall_timeout":
            await asyncio.Event().wait()
        if mode == "connect":
            raise httpx.ConnectError(KEY + QUESTION, request=request)
        if mode == "reset":
            raise ConnectionResetError(errno.ECONNRESET, KEY + QUESTION)
        if mode == "read_reset":
            raise httpx.ReadError("connection reset " + KEY + QUESTION, request=request)
        if mode in {"read_other", "protocol"}:
            error = httpx.ReadError if mode == "read_other" else httpx.RemoteProtocolError
            raise error(KEY + QUESTION, request=request)
        if mode == "caused_reset":
            try:
                raise OSError(errno.ECONNRESET, KEY)
            except OSError as exc:
                raise httpx.ReadError(QUESTION, request=request) from exc
        return httpx.Response(int(mode), json={"error": {"message": KEY + QUESTION + ANSWER + PROMPT}})
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_timeout_s=0.02), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await complete(client, model=KEY + QUESTION) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    sleep.assert_awaited_once_with(1 if mode == "429" else 0.5)
    assert all(json.loads(call.content)["messages"][1]["content"] == QUESTION for call in calls)
    records = [record for record in caplog.records if record.name == "app.llm.client" and record.getMessage().startswith("llm_retry ")]
    assert len(records) == 1
    info = json.loads(records[0].getMessage().removeprefix("llm_retry "))
    category = "http_status" if mode.isdecimal() else "timeout" if "timeout" in mode else "network"
    expected = {"category": category, "host": "retry.example",
                "model": "******", "http_status": int(mode) if mode.isdecimal() else None, "retry": 1}
    if mode == "429":
        expected["error_code"] = None
    assert info == expected
    application_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("app."))
    for private in (KEY, QUESTION, ANSWER, PROMPT):
        assert private not in application_logs


@pytest.mark.parametrize("mode,code", [("connect", 50201), ("503", 50201), ("429", 50302)])
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
        client = LLMClient(settings(llm_max_retries=retries, llm_network_retries=retries,
                                    llm_rate_limit_retries=retries),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert caught.value.code == code
            assert caught.value.http_status == (503 if code == 50302 else 502)
            assert KEY not in str(caught.value) + str(caught.value.diagnostic)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == retries + 1
    assert [call.args[0] for call in sleep.await_args_list] == [
        min(2 ** i, 8) if mode == "429" else 0.5 * 2 ** i for i in range(retries)]


@pytest.mark.parametrize("mode,code", [("400", 50201), ("401", 50201), ("404", 50201), ("302", 50201),
                                      ("bad_json", 50201), ("empty", 50201), ("write_other", 50201)])
def test_nonretryable_failures_never_sleep_or_repeat(mode, code):
    calls = []
    async def handler(request):
        calls.append(request)
        if mode == "write_other":
            raise httpx.WriteError("other write failure " + KEY, request=request)
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


@pytest.mark.parametrize("mode,code", [("bad_json", 50201), ("401", 50201)])
def test_nonretryable_second_attempt_stops_even_with_more_retries_available(mode, code):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
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


@pytest.mark.parametrize("mode", ["read", "connect", "wall"])
@pytest.mark.parametrize("maximum,timeout_retries", [(1, 1), (3, 1), (0, 1), (3, 0), (3, 2), (1, 3)])
@pytest.mark.parametrize("network_retries", [0, 1, 3])
def test_timeouts_obey_network_limit_and_ignore_legacy_limits(mode, maximum, timeout_retries, network_retries):
    calls = []
    async def handler(request):
        calls.append(request)
        if mode == "wall":
            await asyncio.Event().wait()
        error = httpx.ReadTimeout if mode == "read" else httpx.ConnectTimeout
        raise error(KEY + QUESTION, request=request)
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=maximum, llm_timeout_retries=timeout_retries,
                                    llm_network_retries=network_retries,
                                    llm_timeout_s=0.01), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert (caught.value.code, caught.value.http_status) == (50401, 504)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == network_retries + 1
    assert [call.args[0] for call in sleep.await_args_list] == [0.5 * 2 ** i for i in range(network_retries)]


@pytest.mark.parametrize("succeeds", [False, True])
def test_timeout_retry_uses_an_isolated_client_and_closes_it(monkeypatch, succeeds):
    async def run():
        calls, created = [], []
        def handler(request):
            calls.append(request)
            if len(calls) == 1 or not succeeds:
                raise httpx.ReadTimeout(KEY + QUESTION, request=request)
            return success()
        client = LLMClient(settings(llm_network_retries=1), transport=httpx.MockTransport(handler), sleep=AsyncMock())
        original_post, new_http = client._http.post, client._new_http
        client._http.post = AsyncMock(side_effect=original_post)
        def create():
            http = new_http()
            http.post = AsyncMock(side_effect=http.post)
            created.append(http)
            return http
        monkeypatch.setattr(client, "_new_http", create)
        try:
            if succeeds:
                assert await complete(client) == ANSWER
            else:
                with pytest.raises(AppError) as caught:
                    await complete(client)
                assert caught.value.code == 50401
            client._http.post.assert_awaited_once()
            assert len(created) == 1 and created[0] is not client._http
            created[0].post.assert_awaited_once()
            assert created[0].is_closed and not client._http.is_closed
        finally:
            await client.aclose()
    asyncio.run(run())


def test_5xx_and_network_retries_have_independent_counts():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        raise httpx.ReadTimeout(KEY, request=request)
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=2, llm_network_retries=1),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert caught.value.code == 50401
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 0.5]


def test_router_and_generator_both_retry_timeouts():
    from app.llm.router import Router
    from app.llm.generator import Generator
    calls = []
    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if calls.count(model) == 1:
            raise httpx.ReadTimeout(KEY, request=request)
        text = '{"intent":"knowledge","rewritten_query":"申请发票"}' if model == "router" else ANSWER + "[1]"
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
    async def run():
        configured = settings(speculative_retrieval=False)
        client = LLMClient(configured, transport=httpx.MockTransport(handler), sleep=AsyncMock())
        services = FakeServices()
        services.route = Router(client, "router").route
        services.generate_answer = Generator(client, "generate").generate_answer
        try:
            result = await run_chat(ChatRequest(session_id="two-stages", message=QUESTION),
                                    settings=configured, services=services)
            assert result.route == "rag" and result.answer == ANSWER + "[1]"
        finally:
            await client.aclose()
    asyncio.run(run())
    assert calls == ["router", "router", "generate", "generate"]


@pytest.mark.parametrize("blocked_stage", ["retry", "generate"])
def test_total_request_budget_is_shared_by_timeout_retry_and_generation(monkeypatch, blocked_stage):
    from app.llm.router import Router
    from app.llm.generator import Generator
    calls = []
    async def run():
        entered = asyncio.Event()
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(1))
        async def handler(request):
            model = json.loads(request.content)["model"]
            calls.append(model)
            if len(calls) == 1:
                raise httpx.ReadTimeout(KEY, request=request)
            if model == "router" and blocked_stage == "generate":
                return httpx.Response(200, json={"choices": [{"message": {
                    "content": '{"intent":"knowledge","rewritten_query":"申请发票"}'}}]})
            entered.set()
            await asyncio.Event().wait()
        configured = settings(speculative_retrieval=False, llm_max_concurrency=1,
                              llm_timeout_s=60, request_timeout_s=120)
        client = LLMClient(configured, transport=httpx.MockTransport(handler), sleep=AsyncMock())
        services = FakeServices()
        services.route = Router(client, "router").route
        services.generate_answer = Generator(client, "generate").generate_answer
        task = asyncio.create_task(run_chat(ChatRequest(session_id="shared-budget", message=QUESTION),
                                            settings=configured, services=services))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deadlines.expire(task)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(task, 2)
            assert (caught.value.code, caught.value.http_status) == (50401, 504)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
    asyncio.run(run())
    assert calls == (["router", "router"] if blocked_stage == "retry" else ["router", "router", "generate"])


@pytest.mark.parametrize("mode", ["503", "network", "timeout"])
def test_ordinary_retry_backoff_holds_one_shared_concurrency_slot_and_releases_after_success(mode):
    async def run():
        backoff, release = asyncio.Event(), asyncio.Event()
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) > 1:
                return success()
            if mode == "network":
                raise httpx.ConnectError(KEY, request=request)
            if mode == "timeout":
                raise httpx.ReadTimeout(KEY, request=request)
            return httpx.Response(503)
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


@pytest.mark.parametrize("mode", ["network", "timeout"])
def test_whole_request_timeout_cancels_backoff_before_second_attempt(mode, monkeypatch):
    async def run():
        entered = asyncio.Event()
        calls = []
        def handler(request):
            calls.append(request)
            error = httpx.ReadTimeout if mode == "timeout" else httpx.ConnectError
            raise error(KEY, request=request)
        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()
        configured = settings(request_timeout_s=40, speculative_retrieval=False)
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(configured.llm_max_concurrency))
        client = LLMClient(configured, transport=httpx.MockTransport(handler), sleep=sleep)
        services = FakeServices()
        async def route(*args):
            await complete(client)
            raise AssertionError("timeout must interrupt backoff")
        services.route = route
        task = None
        try:
            task = asyncio.create_task(run_chat(ChatRequest(session_id="retry-timeout", message=QUESTION),
                                                settings=configured, services=services))
            await asyncio.wait_for(entered.wait(), 2)
            deadlines.expire(task)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(task, 2)
            assert caught.value.code == 50401 and entered.is_set()
            assert len(calls) == 1
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("errors", [(httpx.ConnectError, httpx.ConnectError),
                                    (httpx.ReadTimeout, httpx.ConnectError)])
def test_two_network_failures_then_third_attempt_succeeds(errors, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1](KEY + QUESTION + ANSWER + PROMPT, request=request)
        return success()
    sleep = AsyncMock()
    async def run():
        # 旧的 5xx/超时配置为零也不会压低默认网络重试预算。
        client = LLMClient(settings(llm_max_retries=0, llm_timeout_retries=0),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await complete(client) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1]
    infos = [json.loads(record.getMessage().removeprefix("llm_retry ")) for record in caplog.records
             if record.name == "app.llm.client" and record.getMessage().startswith("llm_retry ")]
    assert [info["retry"] for info in infos] == [1, 2]
    assert [info["category"] for info in infos] == [
        "timeout" if issubclass(error, httpx.TimeoutException) else "network" for error in errors]
    assert all(private not in caplog.text for private in (KEY, QUESTION, ANSWER, PROMPT))


@pytest.mark.parametrize("errors", [(httpx.ReadTimeout, httpx.ConnectError),
                                    (httpx.ConnectError, httpx.ReadTimeout)])
def test_mixed_network_and_timeout_failures_exhaust_one_shared_limit(errors):
    calls = []
    def handler(request):
        calls.append(request)
        assert len(calls) <= 2, "混合失败不得重置重试计数"
        raise errors[len(calls) - 1](KEY + QUESTION, request=request)
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_network_retries=1, llm_max_retries=5, llm_timeout_retries=5),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            expected = (50401, 504) if issubclass(errors[-1], httpx.TimeoutException) else (50201, 502)
            assert (caught.value.code, caught.value.http_status) == expected
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    sleep.assert_awaited_once_with(0.5)


def test_network_backoff_has_small_bounded_jitter(monkeypatch):
    from unittest.mock import Mock
    jitter = Mock(side_effect=lambda low, high: high)
    monkeypatch.setattr("app.llm.client.random.uniform", jitter)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) <= 3:
            raise httpx.ConnectError(KEY, request=request)
        return success()
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await complete(client) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 4
    assert [call.args[0] for call in sleep.await_args_list] == [0.55, 1.1, 2.1]
    assert [call.args for call in jitter.call_args_list] == [(0, 0.05), (0, 0.1), (0, 0.1)]


@pytest.mark.parametrize("mode,code,status", [("network", 50201, 502), ("timeout", 50401, 504),
                                            ("503", 50201, 502), ("429", 50302, 503)])
@pytest.mark.parametrize("remaining", [0.4, 5.4])
def test_insufficient_inherited_budget_skips_backoff_and_next_attempt(mode, code, status, remaining, caplog):
    from app.budget import request_budget
    calls, responses = [], []
    def handler(request):
        calls.append(request)
        if mode in {"network", "timeout"}:
            error = httpx.ConnectError if mode == "network" else httpx.ReadTimeout
            raise error(KEY + QUESTION, request=request)
        response = httpx.Response(int(mode))
        responses.append(response)
        return response
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_timeout_s=12, request_timeout_s=40),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            async with request_budget(remaining):
                inherited = deadline.get()
                with pytest.raises(AppError) as caught:
                    await complete(client)
                assert deadline.get() == inherited
            assert (caught.value.code, caught.value.http_status) == (code, status)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 1 and all(response.is_closed for response in responses)
    sleep.assert_not_awaited()
    assert not any(record.getMessage().startswith("llm_retry ") for record in caplog.records)
    assert all(private not in caplog.text for private in (KEY, QUESTION, ANSWER, PROMPT))


def test_remaining_budget_shorter_than_full_timeout_still_allows_retry():
    # 排队与网络抖动后整请求只剩约 9 秒：不足一次完整的 12 秒超时，但足够一次正常调用。
    from app.budget import request_budget
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError(KEY, request=request)
        return success()
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_timeout_s=12, llm_min_attempt_s=5, request_timeout_s=40),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            async with request_budget(9):
                assert await complete(client) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    sleep.assert_awaited_once()
