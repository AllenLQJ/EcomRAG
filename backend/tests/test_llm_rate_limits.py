import asyncio
import json
from collections import Counter
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient, _semaphore
from deadline_helpers import ControlledDeadlines

KEY = "RATE_PRIVATE_KEY_81"
QUESTION = "RATE_PRIVATE_QUESTION_82"
ANSWER = "RATE_PRIVATE_ANSWER_83"


def settings(**overrides):
    return Settings(_env_file=None, llm_api_key=KEY, llm_base_url="https://limit.example/v1", **overrides)


async def complete(client, model="test-model"):
    return await client.complete(model=model, messages=[{"role": "user", "content": QUESTION}],
                                 temperature=0, max_tokens=16)


def success():
    return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})


def limited(headers=None):
    return httpx.Response(429, headers=headers, json={"error": {
        "code": "1302", "message": KEY + QUESTION + ANSWER}})


def test_default_two_slots_make_third_call_wait_without_starting_transfer_timeout(monkeypatch):
    async def run():
        full, release = asyncio.Event(), asyncio.Event()
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(2))
        attempts = Counter()
        timeout = asyncio.timeout
        def capture_timeout(seconds):
            attempts[asyncio.current_task()] += 1
            return timeout(seconds)
        monkeypatch.setattr(asyncio, "timeout", capture_timeout)
        active = peak = entered = 0
        async def handler(request):
            nonlocal active, peak, entered
            active += 1
            entered += 1
            peak = max(peak, active)
            if entered == 2:
                full.set()
            try:
                await release.wait()
                return success()
            finally:
                active -= 1
        configured = settings(llm_timeout_s=60, request_timeout_s=60)
        clients = [LLMClient(configured, transport=httpx.MockTransport(handler)) for _ in range(3)]
        tasks = []
        try:
            tasks.extend(asyncio.create_task(complete(client)) for client in clients[:2])
            await asyncio.wait_for(full.wait(), 2)
            third = asyncio.create_task(complete(clients[2]))
            tasks.append(third)
            assert await asyncio.wait_for(deadlines.waiters.get(), 2) is third
            assert entered == peak == 2 and not third.done()
            assert attempts[third] == 0  # 信号量排队尚未创建单次 HTTP 超时。
            release.set()
            assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [ANSWER] * 3
            assert entered == 3 and peak == 2 and attempts[third] == 1
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for client in clients:
                await client.aclose()
    asyncio.run(run())


def test_default_rate_retries_succeed_with_independent_budget_and_safe_provider_code(caplog):
    responses = []
    def handler(request):
        assert all(response.is_closed for response in responses)
        response = limited() if len(responses) < 3 else success()
        responses.append(response)
        return response
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=0), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await complete(client) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(responses) == 4 and all(response.is_closed for response in responses)
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4]
    records = [record for record in caplog.records if record.name == "app.llm.client"
               and record.getMessage().startswith("llm_retry ")]
    infos = [json.loads(record.getMessage().removeprefix("llm_retry ")) for record in records]
    assert infos == [{"category": "http_status", "http_status": 429, "error_code": "1302", "retry": number,
                      "host": "limit.example", "model": "test-model"} for number in (1, 2, 3)]
    assert all(private not in caplog.text for private in (KEY, QUESTION, ANSWER))


@pytest.mark.parametrize("retries", [0, 1, 2, 3, 5])
def test_continuous_upstream_429_exhausts_its_own_limit_and_maps_to_busy(retries, caplog):
    responses = []
    def handler(request):
        response = limited()
        responses.append(response)
        return response
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=0, llm_rate_limit_retries=retries),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            error = caught.value
            assert (error.code, error.http_status, error.message) == (50302, 503, "服务有点忙，请稍后再试")
            assert error.diagnostic["category"] == "http_status"
            assert error.diagnostic["http_status"] == 429 and error.diagnostic["error_code"] == "1302"
            assert all(private not in str(error.diagnostic) for private in (KEY, QUESTION, ANSWER))
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(responses) == retries + 1 and all(response.is_closed for response in responses)
    assert [call.args[0] for call in sleep.await_args_list] == [min(2 ** i, 8) for i in range(retries)]
    final = [record for record in caplog.records if record.getMessage().startswith("llm_error ")]
    assert len(final) == 1
    info = json.loads(final[0].getMessage().removeprefix("llm_error "))
    assert (info["category"], info["http_status"], info["error_code"]) == ("http_status", 429, "1302")
    assert info["error_message"] is None
    assert all(private not in caplog.text for private in (KEY, QUESTION, ANSWER))


@pytest.mark.parametrize("header,delays", [(None, [1, 2, 4]), ("0.5", [1, 2, 4]), ("3", [3, 3, 4]),
                                        ("1.5", [1.5, 2, 4]), ("8", [8, 8, 8]), ("9999", [8, 8, 8]),
                                        ("-1", [1, 2, 4]), ("NaN", [1, 2, 4]), ("inf", [1, 2, 4]),
                                        ("invalid", [1, 2, 4]), ("Wed, 21 Oct 2026 07:28:00 GMT", [1, 2, 4])])
def test_retry_after_seconds_are_a_minimum_but_each_delay_is_capped_at_eight(header, delays):
    calls = []
    def handler(request):
        calls.append(request)
        return limited({"Retry-After": header} if header is not None else None) if len(calls) <= 3 else success()
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await complete(client) == ANSWER
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 4
    assert [call.args[0] for call in sleep.await_args_list] == delays


def test_rate_backoff_releases_slot_for_another_client_before_retry():
    async def run():
        backoff, release = asyncio.Event(), asyncio.Event()
        calls = []
        def handler(request):
            model = json.loads(request.content)["model"]
            calls.append(model)
            return limited() if calls == ["first"] else success()
        async def sleep(delay):
            assert delay == 1
            backoff.set()
            await release.wait()
        configured = settings(llm_max_concurrency=1)
        first = LLMClient(configured, transport=httpx.MockTransport(handler), sleep=sleep)
        second = LLMClient(configured, transport=httpx.MockTransport(handler))
        task = asyncio.create_task(complete(first, "first"))
        try:
            await asyncio.wait_for(backoff.wait(), 2)
            assert await asyncio.wait_for(complete(second, "second"), 2) == ANSWER
            assert calls == ["first", "second"] and not task.done()
            release.set()
            assert await asyncio.wait_for(task, 2) == ANSWER
            assert calls == ["first", "second", "first"]
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await first.aclose()
            await second.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("ordinary", ["503", "network", "timeout"])
def test_rate_retries_do_not_consume_or_reset_ordinary_retry_budget(ordinary, monkeypatch):
    monkeypatch.setattr("app.llm.client.random.uniform", lambda low, high: 0)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) in {1, 3}:
            return limited()
        if ordinary == "network":
            raise httpx.ConnectError(KEY, request=request)
        if ordinary == "timeout":
            raise httpx.ReadTimeout(KEY, request=request)
        return httpx.Response(503)
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_max_retries=1, llm_network_retries=1, llm_rate_limit_retries=3),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                await complete(client)
            assert caught.value.code == (50401 if ordinary == "timeout" else 50201)
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 4
    assert [call.args[0] for call in sleep.await_args_list] == [1, 0.5, 2]


@pytest.mark.parametrize("phase", ["backoff", "requeue"])
def test_request_deadline_interrupts_rate_retry_and_reclaims_permit(phase, monkeypatch):
    async def run():
        backoff, resume, occupied, free = (asyncio.Event() for _ in range(4))
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(1))
        calls = []
        def limited_handler(request):
            calls.append(request)
            return limited()
        async def sleep(delay):
            backoff.set()
            await resume.wait()
        async def holder_handler(request):
            occupied.set()
            await free.wait()
            return success()
        configured = settings(llm_max_concurrency=1, request_timeout_s=120, llm_timeout_s=60)
        client = LLMClient(configured, transport=httpx.MockTransport(limited_handler), sleep=sleep)
        holder = LLMClient(configured, transport=httpx.MockTransport(holder_handler))
        task = asyncio.create_task(complete(client))
        holder_task = None
        try:
            await asyncio.wait_for(backoff.wait(), 2)
            if phase == "requeue":
                holder_task = asyncio.create_task(complete(holder))
                await asyncio.wait_for(occupied.wait(), 2)
                resume.set()
                assert await asyncio.wait_for(deadlines.waiters.get(), 2) is task
            deadlines.expire(task)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(task, 2)
            assert (caught.value.code, caught.value.http_status) == (50401, 504)
            assert len(calls) == 1
            if holder_task is not None:
                assert not holder_task.done() and _semaphore(1).locked()
            free.set()
            if holder_task is not None:
                assert await asyncio.wait_for(holder_task, 2) == ANSWER
            assert await asyncio.wait_for(complete(holder), 2) == ANSWER
        finally:
            resume.set()
            free.set()
            tasks = [item for item in (task, holder_task) if item is not None]
            for item in tasks:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            await holder.aclose()
    asyncio.run(run())


def test_api_maps_upstream_429_to_busy_without_changing_local_rate_limit(app_factory, caplog):
    app = app_factory(llm_api_key=KEY, llm_base_url="https://limit.example/v1", llm_rate_limit_retries=0,
                      speculative_retrieval=False)
    llm = LLMClient(settings(llm_rate_limit_retries=0), transport=httpx.MockTransport(lambda request: limited()))
    async def route(*args):
        await complete(llm)
        raise AssertionError("上游限流应中断路由")
    app.state.services.route = route
    try:
        with TestClient(app) as api:
            response = api.post("/api/v1/chat", json={"session_id": "upstream-busy", "message": QUESTION})
            assert response.status_code == 503
            assert response.json() == {"code": 50302, "message": "服务有点忙，请稍后再试",
                                       "request_id": response.headers["X-Request-ID"]}
            app.state.rate_limiter.check_async = AsyncMock(side_effect=AppError(42901, 429, "提问有点快，休息一分钟再问吧"))
            response = api.post("/api/v1/chat", json={"session_id": "local-limit", "message": QUESTION})
            assert response.status_code == 429
            assert response.json() == {"code": 42901, "message": "提问有点快，休息一分钟再问吧",
                                       "request_id": response.headers["X-Request-ID"]}
    finally:
        asyncio.run(llm.aclose())
    assert all(private not in caplog.text for private in (KEY, QUESTION, ANSWER))
