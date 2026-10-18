import asyncio
import json
from contextlib import aclosing
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.contracts import AppError
from app.llm.client import LLMClient, _semaphore

KEY, QUESTION, ANSWER = 'STREAM_PRIVATE_KEY', 'STREAM_PRIVATE_QUESTION', 'STREAM_PRIVATE_ANSWER'


def packet(content=None, *, reasoning=None, finish=None):
    delta = {}
    if content is not None:
        delta['content'] = content
    if reasoning is not None:
        delta['reasoning_content'] = reasoning
    return ('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]}) + '\n\n').encode()


class Upstream(httpx.AsyncByteStream):
    def __init__(self, source):
        self.source = source
        self.closed = False

    async def __aiter__(self):
        async with aclosing(self.source) as chunks:
            async for chunk in chunks:
                yield chunk

    async def aclose(self):
        self.closed = True
        await self.source.aclose()


def stream_response(*chunks, error=None):
    async def source():
        for chunk in chunks:
            yield chunk
        if error:
            raise error
    stream = Upstream(source())
    return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=stream)


def settings(**overrides):
    return Settings(_env_file=None, llm_api_key=KEY, llm_base_url='https://stream.test/v1', **overrides)


def complete(client):
    return client.stream(model='fake', messages=[{'role': 'user', 'content': QUESTION}], temperature=0, max_tokens=64)


@pytest.mark.parametrize('failures', [('connect', 'connect'), ('timeout', 'connect'), ('429', '503')])
def test_pre_content_retries_reuse_limits_and_only_forward_content(failures, monkeypatch, caplog):
    monkeypatch.setattr('app.llm.client.random.uniform', lambda low, high: 0)
    calls, responses = [], []
    def handler(request):
        calls.append(request)
        assert json.loads(request.content)['stream'] is True
        if len(calls) <= len(failures):
            failure = failures[len(calls) - 1]
            if failure in {'connect', 'timeout'}:
                error = httpx.ConnectError if failure == 'connect' else httpx.ReadTimeout
                raise error(KEY + QUESTION, request=request)
            response = httpx.Response(int(failure), headers={'Retry-After': '1'}, text=KEY + QUESTION)
        else:
            response = stream_response(packet(reasoning='PRIVATE_REASONING'), packet(ANSWER[:7]),
                                       packet(ANSWER[7:]), packet(finish='stop'), b'data: [DONE]\n\n')
        responses.append(response)
        return response
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert ''.join([part async for part in complete(client)]) == ANSWER
            assert not _semaphore(2).locked()
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 3 and all(response.is_closed for response in responses)
    assert [call.args[0] for call in sleep.await_args_list] == ([1, .5] if failures[0] == '429' else [.5, 1])
    assert all(value not in caplog.text for value in (KEY, QUESTION, ANSWER, 'PRIVATE_REASONING'))


@pytest.mark.parametrize('phase', ['before', 'after'])
@pytest.mark.parametrize('failure', ['network', 'timeout', 'protocol'])
def test_failures_exhaust_before_content_but_never_retry_after_content(phase, failure):
    calls, streams, visible = [], [], []
    def handler(request):
        calls.append(request)
        error = {'network': httpx.ReadError, 'timeout': httpx.ReadTimeout, 'protocol': httpx.RemoteProtocolError}[failure]
        response = stream_response(*( [packet('部分')] if phase == 'after' else [] ), error=error(KEY + QUESTION))
        streams.append(response.stream)
        return response
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_network_retries=2), transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                async with aclosing(complete(client)) as source:
                    async for text in source:
                        visible.append(text)
            assert caught.value.code == (50401 if failure == 'timeout' else 50201)
            assert not _semaphore(2).locked()
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == (1 if phase == 'after' else 3)
    assert visible == (['部分'] if phase == 'after' else [])
    assert sleep.await_count == (0 if phase == 'after' else 2)
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('phase', ['first_content', 'idle'])
def test_first_content_and_idle_deadlines_ignore_reasoning_and_release_slot(phase):
    calls, streams = [], []
    async def source():
        yield packet(reasoning='hidden reasoning')
        if phase == 'idle':
            yield packet('开始')
        await asyncio.Event().wait()
    def handler(request):
        calls.append(request)
        stream = Upstream(source())
        streams.append(stream)
        return httpx.Response(200, stream=stream)
    async def run():
        client = LLMClient(settings(llm_timeout_s=.02 if phase == 'first_content' else .5,
                                    llm_stream_idle_timeout_s=.5 if phase == 'first_content' else .02,
                                    llm_network_retries=0, llm_max_concurrency=1), transport=httpx.MockTransport(handler))
        visible = []
        async def read():
            async for text in complete(client):
                visible.append(text)
        try:
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(read(), .3)
            assert caught.value.code == 50401
            assert visible == (['开始'] if phase == 'idle' else [])
            assert not _semaphore(1).locked()
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 1 and streams[0].closed


@pytest.mark.parametrize('phase', ['reading', 'queued'])
def test_cancelling_upstream_or_waiter_releases_semaphore(phase):
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def source():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield b''
            finally:
                closed.set()
        stream = Upstream(source())
        client = LLMClient(settings(llm_max_concurrency=1),
                           transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
        semaphore = _semaphore(1)
        if phase == 'queued':
            await semaphore.acquire()
        async def read():
            async with aclosing(complete(client)) as chunks:
                return [text async for text in chunks]
        task = asyncio.create_task(read())
        try:
            if phase == 'reading':
                await asyncio.wait_for(entered.wait(), 2)
            else:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if phase == 'queued':
                semaphore.release()
            else:
                assert closed.is_set() and stream.closed
            assert not semaphore.locked()
            await asyncio.wait_for(semaphore.acquire(), 1)
            semaphore.release()
        finally:
            await client.aclose()
    asyncio.run(run())


def test_stream_budget_too_short_to_retry_returns_last_error_without_sleep():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError('hidden')
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(llm_timeout_s=12, request_timeout_s=1),
                           transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            with pytest.raises(AppError) as caught:
                _ = [text async for text in complete(client)]
            assert caught.value.code == 50201
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize('mode', ['429', 'network'])
def test_stream_rate_backoff_releases_slot_but_network_backoff_holds_it(mode):
    async def run():
        backoff, release = asyncio.Event(), asyncio.Event()
        calls = []
        def handler(request):
            model = json.loads(request.content)['model']
            calls.append(model)
            if calls == ['first']:
                if mode == 'network':
                    raise httpx.ConnectError('private')
                return httpx.Response(429)
            return stream_response(packet('成功'), b'data: [DONE]\n\n')
        async def sleep(delay):
            backoff.set()
            await release.wait()
        client = LLMClient(settings(llm_max_concurrency=1), transport=httpx.MockTransport(handler), sleep=sleep)
        async def read(model):
            return ''.join([text async for text in client.stream(model=model, messages=[], temperature=0, max_tokens=8)])
        first = asyncio.create_task(read('first'))
        second = None
        try:
            await asyncio.wait_for(backoff.wait(), 2)
            second = asyncio.create_task(read('second'))
            if mode == '429':
                assert await asyncio.wait_for(second, 2) == '成功'
                assert calls == ['first', 'second']
            else:
                await asyncio.sleep(0)
                assert calls == ['first'] and not second.done() and _semaphore(1).locked()
            release.set()
            assert await asyncio.wait_for(first, 2) == '成功'
            assert await asyncio.wait_for(second, 2) == '成功'
            assert not _semaphore(1).locked()
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'network', '429', '503'])
def test_expired_whole_budget_never_computes_backoff_or_retries(failure):
    from app.budget import budget_scope
    from unittest.mock import Mock
    calls = []
    async def handler(request):
        calls.append(request)
        scope = budget_scope.get()
        scope.reschedule(asyncio.get_running_loop().time() - 1)
        if failure == 'timeout':
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 复现传输/内层超时将整请求取消转换成 TimeoutError。
                raise TimeoutError from None
        if failure == 'network':
            raise httpx.ConnectError('PRIVATE_TIMEOUT_DETAIL')
        return httpx.Response(int(failure))
    sleep = AsyncMock()
    async def run():
        client = LLMClient(settings(request_timeout_s=60), transport=httpx.MockTransport(handler), sleep=sleep)
        client._network_delay = Mock(side_effect=AssertionError('过期后不得计算退避'))
        try:
            with pytest.raises(AppError) as caught:
                _ = [part async for part in complete(client)]
            assert (caught.value.code, caught.value.http_status) == (50401, 504)
            client._network_delay.assert_not_called()
            assert not _semaphore(2).locked()
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 1
    sleep.assert_not_awaited()
