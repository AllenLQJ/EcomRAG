import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient, _semaphore
from deadline_helpers import ControlledDeadlines


def settings(**overrides):
    return Settings(_env_file=None, llm_api_key="PRIVATE_KEY", llm_base_url="https://model.test/v1", **overrides)


async def complete(client):
    return await client.complete(model="fake", messages=[{"role": "user", "content": "PRIVATE_Q"}],
                                 temperature=0, max_tokens=16)


@pytest.mark.parametrize("status,body,code", [(200, {"choices": [{"message": {"content": "ok"}}]}, None),
                                            (200, {"choices": []}, 50201), (401, {"error": {}}, 50201)])
def test_response_is_closed_on_success_and_final_failure(status, body, code):
    response = httpx.Response(status, json=body)
    original = response.aclose
    response.aclose = AsyncMock(side_effect=original)
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(lambda request: response))
        try:
            if code is None:
                assert await complete(client) == "ok"
            else:
                with pytest.raises(AppError) as caught:
                    await complete(client)
                assert caught.value.code == code
            assert response.is_closed and response.aclose.await_count >= 1
        finally:
            await client.aclose()
    asyncio.run(run())


def test_retry_closes_previous_response_before_next_attempt():
    first = httpx.Response(503, json={"error": {}})
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return first
        assert first.is_closed
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(handler), sleep=AsyncMock())
        try:
            assert await complete(client) == "ok"
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2


def test_queue_wait_has_total_deadline_without_consuming_transfer_timeout(monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(1))
        calls = []
        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        first = LLMClient(settings(llm_max_concurrency=1, llm_timeout_s=60, request_timeout_s=60),
                          transport=httpx.MockTransport(handler))
        second = LLMClient(settings(llm_max_concurrency=1, llm_timeout_s=60, request_timeout_s=60),
                           transport=httpx.MockTransport(handler))
        holder = asyncio.create_task(complete(first))
        queued = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            queued = asyncio.create_task(complete(second))
            assert await asyncio.wait_for(deadlines.waiters.get(), 5) is queued
            deadlines.expire(queued)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(queued, 5)
            assert caught.value.code == 50401 and not holder.done()
            assert len(calls) == 1
            release.set()
            assert await asyncio.wait_for(holder, 5) == "ok"
        finally:
            release.set()
            tasks = [task for task in (holder, queued) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await first.aclose()
            await second.aclose()
    asyncio.run(run())


def test_cancelled_queue_deadline_does_not_leak_semaphore_permit(monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(1))
        calls = []

        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        client = LLMClient(settings(llm_max_concurrency=1, llm_timeout_s=60, request_timeout_s=60),
                           transport=httpx.MockTransport(handler))
        tasks = [asyncio.create_task(complete(client))]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            cancelled = asyncio.create_task(complete(client))
            tasks.append(cancelled)
            assert await asyncio.wait_for(deadlines.waiters.get(), 5) is cancelled
            deadlines.expire(cancelled)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(cancelled, 5)
            assert caught.value.code == 50401
            assert not tasks[0].done() and len(calls) == 1

            # 新请求先确实排队，再释放持有者；被取消的 waiter 不得抢走或泄漏名额。
            successor = asyncio.create_task(complete(client))
            tasks.append(successor)
            assert await asyncio.wait_for(deadlines.waiters.get(), 5) is successor
            assert len(calls) == 1
            release.set()
            assert await asyncio.wait_for(tasks[0], 5) == "ok"
            assert await asyncio.wait_for(successor, 5) == "ok"
            assert await asyncio.wait_for(complete(client), 5) == "ok"
            assert len(calls) == 3
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
    asyncio.run(run())
