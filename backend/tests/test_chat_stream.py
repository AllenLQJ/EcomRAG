import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import AppError, FaqHit, RouteDecision
from app.pipeline import REFUSAL
from app.schemas import ChatRequest
from app.streaming import chat_events
from chat_fakes import FakeServices, document


def events(text):
    rows = []
    for block in text.split('\n\n'):
        lines = block.splitlines()
        name = next((line[7:] for line in lines if line.startswith('event: ')), None)
        if name:
            rows.append((name, json.loads('\n'.join(line[6:] for line in lines if line.startswith('data: ')))))
    return rows


def stream_parts(*parts, error=None):
    async def source(*args):
        for part in parts:
            yield part
        if error:
            raise error
    return source


@pytest.mark.parametrize('route', ['cache', 'faq', 'general', 'rag', 'sensitive', 'low'])
def test_all_routes_event_order_headers_and_final_contract(app_factory, route, caplog):
    caplog.set_level(logging.INFO)
    app = app_factory(speculative_retrieval=False)
    services = app.state.services
    if route in {'cache', 'faq'}:
        services.faq_lookup.return_value = FaqHit(faq_id=1, question='PRIVATE_QUESTION', answer='PRIVATE_ANSWER',
                                                 score=1, source=route)
    elif route == 'sensitive':
        services.route.return_value = RouteDecision(intent='sensitive', rewritten_query='敏感请求')
    elif route in {'rag', 'low'}:
        services.knowledge()
        services.retrieve.return_value = [document(1, .3), document(2, .9)] if route == 'rag' else []
        services.generate_answer_stream = stream_parts('PRIVATE_', 'ANSWER[2]')
    else:
        services.chat_general_stream = stream_parts('PRIVATE_', 'ANSWER')
    with TestClient(app) as api:
        response = api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': 'PRIVATE_QUESTION'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['cache-control'] == 'no-cache'
    assert response.headers['x-accel-buffering'] == 'no'
    assert response.headers['content-encoding'] == 'identity'
    rows = events(response.text)
    assert rows[0] == ('meta', {'request_id': response.headers['x-request-id'], 'session_id': 's'})
    stages = [data['stage'] for event, data in rows if event == 'status']
    expected = ['faq']
    if route not in {'cache', 'faq'}:
        expected.append('routing')
    if route in {'rag', 'low'}:
        expected.append('retrieving')
    if route in {'general', 'rag'}:
        expected.append('generating')
    assert stages == expected
    assert rows[-1][0] == 'done'
    assert all(event == 'delta' for event, _ in rows[1 + len(expected):-1])
    result = rows[-1][1]
    assert result['route'] == ('fallback' if route in {'low', 'sensitive'} else route)
    assert result['trace'] is None and result['is_mock'] is False
    assert ''.join(data['text'] for event, data in rows if event == 'delta') == result['answer']
    if route == 'rag':
        assert [source['doc_id'] for source in result['sources']] == ['d2']
        assert result['sources'][0]['index'] == 2
    assert 'PRIVATE_QUESTION' not in caplog.text and 'PRIVATE_ANSWER' not in caplog.text
    app.state.chat_logs.write.assert_awaited_once()
    row = app.state.chat_logs.write.await_args.args[0]
    assert (row.question, row.answer, row.route) == ('PRIVATE_QUESTION', result['answer'], result['route'])


@pytest.mark.parametrize('allow,debug', [(False, False), (False, True), (True, False), (True, True)])
def test_stream_trace_gate_and_unreferenced_highest_score(app_factory, allow, debug):
    app = app_factory(allow_trace=allow, speculative_retrieval=False)
    services = app.state.services.knowledge()
    services.retrieve.return_value = [document(1, .4), document(2, .95)]
    services.generate_answer_stream = stream_parts('有明确说明。')
    with TestClient(app) as api:
        result = events(api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': '参数', 'debug': debug}).text)[-1][1]
    assert [source['doc_id'] for source in result['sources']] == ['d2']
    assert bool(result['trace']) == (allow and debug)
    if allow and debug:
        assert result['trace'][-1]['stage'] == 'total'


@pytest.mark.parametrize('route', ['general', 'rag'])
@pytest.mark.parametrize('text', ['NO_ANSWER', '** n o _ a n s w e r **[1]。', '已知NO_ANSWER说明[1]',
                                 'NO_ANS', 'ＮＯ＿ＡＮＳＷＥＲ', 'ＮＯ＿ＡＮＳ', '已知ＮＯ＿ＡＮＳＷＥＲ说明[1]'])
def test_no_answer_is_buffered_or_removed_before_deltas(app_factory, route, text):
    app = app_factory(speculative_retrieval=False)
    services = app.state.services
    if route == 'rag':
        services.knowledge()
    setattr(services, 'generate_answer_stream' if route == 'rag' else 'chat_general_stream', stream_parts(*text))
    with TestClient(app) as api:
        rows = events(api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': '参数'}).text)
    deltas = ''.join(data['text'] for event, data in rows if event == 'delta')
    assert 'NO_ANSWER' not in deltas and 'n o _ a n s w e r' not in deltas
    if text.startswith('已知'):
        assert deltas == '已知说明[1]' and rows[-1][1]['route'] == route
    else:
        assert deltas == REFUSAL
        assert sum(event == 'delta' for event, _ in rows) == 1
        assert rows[-2] == ('delta', {'text': REFUSAL})
        assert rows[-1][1]['route'] == 'fallback' and rows[-1][1]['answer'] == REFUSAL
        assert rows[-1][1]['sources'] == []


@pytest.mark.parametrize('code,status,message', [(42901, 429, '提问有点快，休息一分钟再问吧'),
                                                 (50302, 503, '服务有点忙，请稍后再试'),
                                                 (50401, 504, '请求超时，请稍后再试'),
                                                 (50201, 502, '模型服务返回异常'),
                                                 (50301, 503, '服务暂时不可用，请稍后再试')])
def test_stream_errors_keep_existing_contract_and_log_error_row(app_factory, code, status, message):
    app = app_factory()
    app.state.rate_limiter.check_async = AsyncMock(side_effect=AppError(code, status, message))
    with TestClient(app) as api:
        response = api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': '问题'})
    rows = events(response.text)
    assert [event for event, _ in rows] == ['meta', 'error']
    assert rows[-1][1] == dict(code=code, message=message, request_id=response.headers['x-request-id'])
    row = app.state.chat_logs.write.await_args.args[0]
    assert row.route == 'error' and row.error_code == code and row.answer == ''


def test_after_delta_failure_sends_error_and_no_done(app_factory):
    app = app_factory(speculative_retrieval=False)
    app.state.services.chat_general_stream = stream_parts('部分文本', error=AppError(50201, 502, '模型服务返回异常'))
    with TestClient(app) as api:
        rows = events(api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': '你好'}).text)
    assert [event for event, _ in rows] == ['meta', 'status', 'status', 'status', 'delta', 'error']
    assert rows[-1][1]['code'] == 50201
    assert app.state.chat_logs.write.await_args.args[0].answer == ''


def test_heartbeat_while_waiting_and_closing_cancels_upstream(monkeypatch):
    monkeypatch.setattr('app.streaming.HEARTBEAT_S', .005)
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()
        services = FakeServices()
        async def source(*args):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield 'unreachable'
            finally:
                closed.set()
        services.chat_general_stream = source
        stream = chat_events(ChatRequest(session_id='s', message='你好'), 'r',
                             settings=Settings(_env_file=None, speculative_retrieval=False), services=services,
                             before_chat=AsyncMock(), chat_logs=SimpleNamespace(write=AsyncMock()))
        try:
            assert (await anext(stream)).startswith('event: meta')
            frames = []
            while not entered.is_set():
                frames.append(await asyncio.wait_for(anext(stream), 2))
            async with asyncio.timeout(2):
                while True:
                    frame = await anext(stream)
                    if frame.startswith('event:'):
                        continue
                    assert frame == ': ping\n\n'
                    break
        finally:
            await stream.aclose()
        assert closed.is_set()
    asyncio.run(run())


def test_asgi_disconnect_closes_generator_even_while_sending_delta():
    from app.streaming import SSEStreamingResponse
    async def run():
        disconnected, closed = asyncio.Event(), asyncio.Event()
        services = FakeServices()
        async def source(*args):
            try:
                yield '部分'
                await asyncio.Event().wait()
            finally:
                closed.set()
        services.chat_general_stream = source
        stream = chat_events(ChatRequest(session_id='s', message='你好'), 'r',
                             settings=Settings(_env_file=None, speculative_retrieval=False), services=services,
                             before_chat=AsyncMock(), chat_logs=SimpleNamespace(write=AsyncMock()))
        response = SSEStreamingResponse(stream, media_type='text/event-stream')
        async def receive():
            await disconnected.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body' and b'event: delta' in message.get('body', b''):
                disconnected.set()
                # 模拟 socket 发送还在等待时客户端已断开。
                await asyncio.Event().wait()
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.0'}}
        await asyncio.wait_for(response(scope, receive, send), 2)
        assert closed.is_set()
    asyncio.run(run())


def test_disconnect_removes_model_executor_waiter_without_running_faq():
    import threading
    from app.executor import BoundedExecutor
    async def run():
        entered, queued = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        executor = BoundedExecutor(1, name='stream-test', queue_size=1)
        loop = asyncio.get_running_loop()
        def hold():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
        holder = asyncio.create_task(executor.run(hold))
        services = FakeServices()
        services.model_executor = executor
        # Observe actual admission rather than sleeping for queue timing.
        original_run = executor.run
        async def observe(function, *args, **kwargs):
            loop.call_soon(queued.set)
            return await original_run(function, *args, **kwargs)
        stream = chat_events(ChatRequest(session_id='s', message='你好'), 'r',
                             settings=Settings(_env_file=None, speculative_retrieval=False), services=services,
                             before_chat=AsyncMock(), chat_logs=SimpleNamespace(write=AsyncMock()))
        pending = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            executor.run = observe
            assert 'event: meta' in await anext(stream)
            assert 'event: status' in await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.wait_for(queued.wait(), 2)
            assert len(executor._waiting) == 1 and executor._admitted == 2
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await stream.aclose()
            assert not executor._waiting and executor._admitted == 1
            services.faq_lookup.assert_not_called()
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.cancel()
            await asyncio.gather(*(task for task in (pending, holder) if task is not None), return_exceptions=True)
            await executor.aclose()
    asyncio.run(run())


def test_whole_stream_request_budget_cancels_upstream_and_emits_timeout(app_factory):
    app = app_factory(speculative_retrieval=False, request_timeout_s=.02)
    closed = []
    async def source(*args):
        try:
            await asyncio.Event().wait()
            yield 'unreachable'
        finally:
            closed.append(True)
    app.state.services.chat_general_stream = source
    with TestClient(app) as api:
        rows = events(api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': '你好'}).text)
    assert rows[-1][0] == 'error' and rows[-1][1]['code'] == 50401
    assert not any(event in {'delta', 'done'} for event, _ in rows)
    assert closed == [True]


def test_stream_actual_dependency_failure_and_unexpected_error_do_not_leak(app_factory, caplog):
    caplog.set_level(logging.INFO)
    for failure, code in [(ConnectionError('PRIVATE_QUESTION PRIVATE_ANSWER FAKE_KEY'), 50301),
                          (RuntimeError('PRIVATE_QUESTION PRIVATE_ANSWER FAKE_KEY'), 50001)]:
        app = app_factory(llm_api_key='FAKE_KEY')
        app.state.services.faq_lookup.side_effect = failure
        with TestClient(app) as api:
            rows = events(api.post('/api/v1/chat/stream', json={'session_id': 's', 'message': 'PRIVATE_QUESTION'}).text)
        assert [event for event, _ in rows] == ['meta', 'status', 'error']
        assert rows[-1][1]['code'] == code
    assert all(value not in caplog.text for value in ('PRIVATE_QUESTION', 'PRIVATE_ANSWER', 'FAKE_KEY'))


@pytest.mark.parametrize('write_fails', [False, True])
def test_disconnect_shields_partial_chat_log_and_preserves_cancellation(write_fails, caplog):
    caplog.set_level(logging.INFO)
    async def run():
        entered, closed, writing, finish_write = (asyncio.Event() for _ in range(4))
        services = FakeServices()
        async def source(*args):
            try:
                yield 'PRIVATE_PARTIAL_ANSWER'
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def write(row):
            writing.set()
            await finish_write.wait()
            if write_fails:
                raise RuntimeError('PRIVATE_QUESTION PRIVATE_PARTIAL_ANSWER FAKE_KEY')
        writer = SimpleNamespace(write=AsyncMock(side_effect=write))
        services.chat_general_stream = source
        stream = chat_events(ChatRequest(session_id='s', message='PRIVATE_QUESTION'), 'r',
                             settings=Settings(_env_file=None, speculative_retrieval=False, llm_api_key='FAKE_KEY'),
                             services=services, before_chat=AsyncMock(), chat_logs=writer)
        pending = None
        try:
            async with asyncio.timeout(2):
                while 'event: delta' not in await anext(stream):
                    pass
                await entered.wait()
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.wait_for(writing.wait(), 2)
            assert not pending.done() and closed.is_set()
            finish_write.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            writer.write.assert_awaited_once()
            row = writer.write.await_args.args[0]
            assert (row.question, row.answer, row.route) == ('PRIVATE_QUESTION', 'PRIVATE_PARTIAL_ANSWER', 'error')
        finally:
            finish_write.set()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
    asyncio.run(run())
    assert all(value not in caplog.text for value in ('PRIVATE_QUESTION', 'PRIVATE_PARTIAL_ANSWER', 'FAKE_KEY'))


@pytest.mark.parametrize('failure', ['cancelled', 'serialize'])
def test_abnormal_producer_exit_still_ends_with_error_event(monkeypatch, failure, caplog):
    class BadSource:
        def model_dump(self):
            raise RuntimeError('PRIVATE_DETAIL')
    if failure == 'cancelled':
        run = AsyncMock(side_effect=asyncio.CancelledError())
    else:
        run = AsyncMock(return_value=SimpleNamespace(answer='partial', route='general', strategy=None, sources=[BadSource()]))
    monkeypatch.setattr('app.streaming.run_chat', run)
    async def collect():
        stream = chat_events(ChatRequest(session_id='s', message='问题'), 'r', settings=Settings(_env_file=None),
                             services=FakeServices(), before_chat=AsyncMock(), chat_logs=SimpleNamespace(write=AsyncMock()))
        async with asyncio.timeout(2):
            rows = [frame async for frame in stream]
        return events(''.join(rows))
    rows = asyncio.run(collect())
    assert [event for event, _ in rows] == ['meta', 'error']
    assert rows[-1][1] == dict(code=50001, message='内部错误', request_id='r')
    assert 'PRIVATE_DETAIL' not in caplog.text
