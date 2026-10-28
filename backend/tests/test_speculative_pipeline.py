import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import Settings
from app.contracts import AppError, RouteDecision
from app.pipeline import SENSITIVE_REFUSAL, run_chat
from app.schemas import ChatRequest
from app.tracing import items, record
from chat_fakes import FakeServices, document


def request():
    return ChatRequest(session_id="parallel-test", message="原问题", debug=True)


def decision(intent="knowledge"):
    return RouteDecision(intent=intent, rewritten_query="路由改写问题")


def test_knowledge_router_and_retrieval_start_concurrently_and_use_original_query():
    async def run():
        router_started, retrieval_started = asyncio.Event(), asyncio.Event()
        services = FakeServices().knowledge()
        docs = [document(2)]

        async def route(question, history):
            router_started.set()
            await retrieval_started.wait()
            return decision()

        async def candidates(queries):
            retrieval_started.set()
            await router_started.wait()
            return docs

        services.route = AsyncMock(side_effect=route)
        services.retrieve_candidates = AsyncMock(side_effect=candidates)
        services.rerank_candidates = AsyncMock(return_value=docs)
        result = await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True, allow_trace=True), services=services)
        assert result.route == "rag" and result.sources[0].doc_id == "d2"
        services.retrieve_candidates.assert_awaited_once_with(["原问题"])
        services.rerank_candidates.assert_awaited_once_with("原问题", docs, top_k=5)
        services.retrieve.assert_not_called()
        stages = {item.stage: item for item in result.trace}
        assert stages["retrieve"].speculative is True and stages["rerank"].speculative is True
        assert stages["total"].ms >= max(stages["router"].ms, stages["retrieve"].ms, stages["rerank"].ms)
        assert items.get() is None
    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("intent", ["general", "sensitive"])
@pytest.mark.parametrize("failure", [False, True])
def test_discarded_speculation_has_no_unretrieved_exception_or_trace_leak(intent, failure, caplog):
    async def run():
        entered, finished = asyncio.Event(), asyncio.Event()
        services = FakeServices()
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))

        async def candidates(queries):
            entered.set()
            # 模拟工作线程写入投机上下文，再让路由作出丢弃决定。
            from time import perf_counter
            record("retrieve", perf_counter(), candidates=1)
            finished.set()
            if failure:
                raise RuntimeError("PRIVATE_SPECULATIVE_ERROR")
            await asyncio.Event().wait()

        async def route(*args):
            await entered.wait()
            await finished.wait()
            return decision(intent)

        services.route = AsyncMock(side_effect=route)
        services.retrieve_candidates = AsyncMock(side_effect=candidates)
        services.rerank_candidates = Mock()
        result = await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True, allow_trace=True), services=services)
        await asyncio.sleep(0)  # 消费取消回调，不等待任何模型线程。
        assert not unhandled
        assert result.route == ("general" if intent == "general" else "fallback")
        if intent == "sensitive":
            assert result.answer == SENSITIVE_REFUSAL
            services.chat_general.assert_not_awaited()
        assert not {"retrieve", "rerank"} & {item.stage for item in result.trace}
        services.rerank_candidates.assert_not_called()
        services.generate_answer.assert_not_awaited()
    caplog.set_level("DEBUG", logger="app.pipeline")
    asyncio.run(asyncio.wait_for(run(), 2))
    assert sum("speculative_retrieval_discarded" in record.getMessage() for record in caplog.records) == 1
    assert "PRIVATE_SPECULATIVE_ERROR" not in caplog.text


@pytest.mark.parametrize("error", [ConnectionError("private"),
    type("MilvusException", (Exception,), {"__module__": "pymilvus.exceptions"})("private")])
def test_knowledge_speculative_infrastructure_failure_maps_to_503(error):
    services = FakeServices().knowledge()
    services.retrieve_candidates = AsyncMock(side_effect=error)
    with pytest.raises(AppError) as caught:
        asyncio.run(run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True), services=services))
    assert (caught.value.code, caught.value.http_status) == (50301, 503)
    services.generate_answer.assert_not_awaited()


def test_knowledge_speculative_programming_error_propagates():
    services = FakeServices().knowledge()
    error = ValueError("bug")
    services.retrieve_candidates = AsyncMock(side_effect=error)
    with pytest.raises(ValueError) as caught:
        asyncio.run(run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True), services=services))
    assert caught.value is error


@pytest.mark.parametrize("enabled,history", [(False, []), (True, [{"role": "user", "content": "上轮"}])])
def test_disabled_or_multiturn_uses_rewritten_query_after_router(enabled, history):
    async def run():
        services = FakeServices().knowledge()
        services.retrieve_candidates = Mock(side_effect=AssertionError("must not speculate"))
        def retrieve(queries, rerank_query, top_k):
            services.route.assert_awaited_once_with("原问题", history)
            assert queries == ["改写后的检索问题"]
            assert rerank_query == ("改写后的检索问题" if history else "原问题")
            return [document()]
        services.retrieve.side_effect = retrieve
        result = await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=enabled),
                                services=services, history=history)
        assert result.route == "rag"
        services.retrieve_candidates.assert_not_called()
        services.generate_answer.assert_awaited_once()
        assert services.generate_answer.call_args.args[2] == history
    asyncio.run(run())


def test_speculative_rerank_runs_off_event_loop_with_trace_context():
    async def run():
        loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
        rerank_entered = asyncio.Event()
        services = FakeServices().knowledge()
        services.retrieve_candidates = AsyncMock(return_value=[document()])
        def rerank(question, candidates, top_k):
            assert threading.get_ident() != event_thread
            from time import perf_counter
            record("rerank", perf_counter(), kept=1)
            loop.call_soon_threadsafe(rerank_entered.set)
            return candidates
        async def route(*args):
            await rerank_entered.wait()
            return decision()
        services.route = route
        services.rerank_candidates = rerank
        result = await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True, allow_trace=True), services=services)
        assert result.route == "rag"
        assert len([item for item in result.trace if item.stage == "rerank"]) == 1
    asyncio.run(asyncio.wait_for(run(), 2))


def test_request_timeout_consumes_both_parallel_tasks_and_records_total():
    async def run():
        services = FakeServices().knowledge()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def candidates(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def router(*args):
            await entered.wait()
            await asyncio.Event().wait()
        services.route = router
        services.retrieve_candidates = candidates
        trace = []
        with pytest.raises(AppError) as caught:
            await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True, request_timeout_s=0.02),
                           services=services, trace_sink=trace)
        assert caught.value.code == 50401
        await asyncio.wait_for(cancelled.wait(), 1)
        assert [item.stage for item in trace] == ["cache", "faq", "total"]
    asyncio.run(run())


def test_discarded_worker_finishes_later_without_delaying_response_or_leaking_trace():
    async def run():
        loop = asyncio.get_running_loop()
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        services = FakeServices()
        services.retrieve_candidates = AsyncMock(return_value=[document()])
        def rerank(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(2):
                    raise AssertionError("test must release worker")
                from time import perf_counter
                record("rerank", perf_counter(), kept=1)
                raise ValueError("PRIVATE_LATE_WORKER_ERROR")
            finally:
                loop.call_soon_threadsafe(finished.set)
        async def route(*args):
            await entered.wait()
            return decision("general")
        services.route, services.rerank_candidates = route, rerank
        trace = []
        try:
            result = await asyncio.wait_for(run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True, allow_trace=True),
                                                     services=services, trace_sink=trace), 1)
            assert result.route == "general" and not finished.is_set()
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        assert not unhandled
        assert not {"retrieve", "rerank"} & {item.stage for item in trace}
        assert [item.stage for item in result.trace] == [item.stage for item in trace]
    asyncio.run(run())


def test_handoff_timeout_drops_all_speculative_trace_and_consumes_worker_exception(caplog):
    async def run():
        loop = asyncio.get_running_loop()
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        services = FakeServices().knowledge()
        services.retrieve_candidates = AsyncMock(return_value=[document()])
        def rerank(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait(2)
                from time import perf_counter
                record("rerank", perf_counter(), kept=1)
                raise RuntimeError("PRIVATE_LATE_EXCEPTION")
            finally:
                loop.call_soon_threadsafe(finished.set)
        services.rerank_candidates = rerank
        trace = []
        try:
            with pytest.raises(AppError) as caught:
                await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True, request_timeout_s=0.1),
                               services=services, trace_sink=trace)
            assert caught.value.code == 50401 and entered.is_set()
            assert [item.stage for item in trace] == ["cache", "faq", "router", "total"]
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        assert not unhandled and not {"retrieve", "rerank"} & {item.stage for item in trace}
    asyncio.run(run())
    assert "PRIVATE_LATE_EXCEPTION" not in caplog.text


def test_handoff_baseexception_is_reraised_and_private_trace_discarded():
    class Abort(BaseException):
        pass
    async def run():
        services = FakeServices().knowledge()
        async def candidates(*args):
            from time import perf_counter
            record("retrieve", perf_counter(), candidates=1)
            return [document()]
        services.retrieve_candidates = candidates
        services.rerank_candidates = AsyncMock(side_effect=Abort())
        trace = []
        with pytest.raises(Abort):
            await run_chat(request(), settings=Settings(_env_file=None, speculative_retrieval=True), services=services, trace_sink=trace)
        await asyncio.sleep(0)
        assert not {"retrieve", "rerank"} & {item.stage for item in trace}
    asyncio.run(run())
