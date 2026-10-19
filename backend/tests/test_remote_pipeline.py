import asyncio
import inspect
import threading
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.config import Settings
from app.contracts import AppError, RouteDecision, SERVICE_BUSY
from app.pipeline import run_chat
from app.retrieval.remote_reranker import RemoteReranker
from app.retrieval.retriever import Retriever
from app.services import Services
from app.schemas import ChatRequest
from app.tracing import items
from chat_fakes import FakeServices, document


def configured(**overrides):
    values = dict(rerank_provider="remote", remote_refuse_threshold=0.6, llm_api_key="PRIVATE_KEY",
                  llm_base_url="https://vendor.example/v4")
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_remote_assembly_skips_local_loading_warmup_and_warns_safely(caplog):
    async def run():
        with patch("app.services._embedding_model", return_value=(Mock(), threading.Lock())), \
             patch("app.services.BgeReranker", side_effect=AssertionError("must not load local")), \
             patch("app.services.FaqService"), \
             patch("app.services.Retriever", return_value=Mock(client=None)):
            services = Services(configured(remote_refuse_threshold=None, llm_api_key=""),
                                transport=httpx.MockTransport(lambda r: pytest.fail("warmup network forbidden")))
            try:
                assert inspect.iscoroutinefunction(services.rerank_candidates)
                assert inspect.iscoroutinefunction(services.retrieve)
                await services.warmup()
                assert services._local_reranker is None
            finally:
                await services.aclose()
    asyncio.run(run())
    assert "远程重排的分数分布与本地不同，REFUSE_THRESHOLD 需用 eval.check_retrieval 校准后设置 REMOTE_REFUSE_THRESHOLD" in caplog.text
    assert "未配置" in caplog.text
    assert "vendor.example" not in caplog.text and "PRIVATE_KEY" not in caplog.text


@pytest.mark.parametrize("fallback,local_failure,provider", [
    ("rrf", False, "fallback_rrf"), ("local", False, "fallback_local"), ("local", True, "fallback_rrf")])
def test_fallback_modes_keep_inputs_unmodified_and_record_provider(fallback, local_failure, provider):
    async def run():
        remote = Mock(rerank=AsyncMock(side_effect=AppError(50201, 502, "safe")))
        local = AsyncMock(side_effect=ValueError("PRIVATE_LOCAL_TEXT")) if local_failure else AsyncMock(return_value=[0.1, 0.9])
        retriever = Retriever(None, None, remote, rerank_fallback=fallback, local_rerank=local,
                              rerank_candidates=2)
        candidates = [document(1, 0.03), document(2, 0.02), document(3, 0.01)]
        trace = []
        token = items.set(trace)
        try:
            result = await retriever.arerank_candidates("q", candidates, top_k=3)
        finally:
            items.reset(token)
        assert [doc.score for doc in candidates] == [0.03, 0.02, 0.01]
        assert all(doc.score_provider is None for doc in candidates)
        assert trace[0].provider == provider
        assert trace[0].fallback == ("local" if provider == "fallback_local" else "rrf")
        assert trace[0].ms >= 0
        if provider == "fallback_local":
            assert [doc.parent_id for doc in result] == ["p2", "p1"]
            assert [doc.score for doc in result] == [0.9, 0.1]
        else:
            assert [doc.parent_id for doc in result] == ["p1", "p2", "p3"]
        if fallback == "rrf":
            local.assert_not_awaited()
        else:
            local.assert_awaited_once()
    asyncio.run(run())


def test_error_policy_reraises_original_app_error_and_cancellation_never_falls_back():
    async def run():
        error = AppError(50401, 504, "safe")
        remote = Mock(rerank=AsyncMock(side_effect=error))
        local = AsyncMock()
        retriever = Retriever(None, None, remote, rerank_fallback="error", local_rerank=local)
        with pytest.raises(AppError) as caught:
            await retriever.arerank_candidates("q", [document()])
        assert caught.value is error
        remote.rerank.side_effect = asyncio.CancelledError()
        retriever.rerank_fallback = "local"
        with pytest.raises(asyncio.CancelledError):
            await retriever.arerank_candidates("q", [document()])
        local.assert_not_awaited()
    asyncio.run(run())


def test_error_policy_propagates_upstream_429_as_service_busy():
    async def run():
        config = configured(rerank_fallback="error", rerank_remote_max_retries=1,
                            rerank_remote_retry_backoff_s=0)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"code": "busy"}})
        remote = RemoteReranker(config, transport=httpx.MockTransport(handler))
        local = AsyncMock()
        retriever = Retriever(None, None, remote, rerank_fallback=config.rerank_fallback,
                              local_rerank=local)
        try:
            with pytest.raises(AppError) as caught:
                await retriever.arerank_candidates("q", [document()])
            assert (caught.value.code, caught.value.http_status) == (SERVICE_BUSY, 503)
            assert caught.value.message == "服务有点忙，请稍后再试"
            assert caught.value.diagnostic["http_status"] == 429
            assert len(calls) == 2
            local.assert_not_awaited()
        finally:
            await remote.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("mode,max_chars,expected", [("child", 1, ["child one", "child two"]),
    ("parent", 3, ["par", "oth"]), ("parent", 0, ["parent one", "other two"])])
def test_remote_candidate_limit_modes_sorting_and_trace(mode, max_chars, expected):
    async def run():
        remote = Mock(rerank=AsyncMock(return_value=[-2.0, 9.0]))
        retriever = Retriever(None, None, remote, rerank_candidates=2, rerank_text_mode=mode,
                              rerank_max_chars=max_chars)
        candidates = [document(1).model_copy(update={"content": "parent one", "snippet": "child one"}),
                      document(2).model_copy(update={"content": "other two", "snippet": "child two"}), document(3)]
        trace = []
        token = items.set(trace)
        try:
            result = await retriever.arerank_candidates("original", candidates, top_k=2)
        finally:
            items.reset(token)
        remote.rerank.assert_awaited_once_with("original", expected)
        assert [doc.score for doc in result] == [9.0, -2.0]
        assert [doc.parent_id for doc in result] == ["p2", "p1"]
        assert trace[0].provider == "remote"
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["local", "remote"])
def test_disabled_rerank_has_priority_in_pipeline(provider):
    services = FakeServices().knowledge()
    remote = Mock(rerank=AsyncMock(side_effect=AssertionError("must not rerank")))
    retriever = Retriever(None, None, remote, rerank_enabled=False, rerank_candidates=1)
    candidates = [document(1, 0.001), document(2, 0.0001)]
    services.retrieve_candidates = Mock(return_value=candidates)
    services.rerank_candidates = retriever.arerank_candidates
    result = asyncio.run(run_chat(ChatRequest(session_id="off", message="q", debug=True),
                                   settings=configured(rerank_provider=provider, rerank_enabled=False,
                                                       allow_trace=True), services=services))
    assert result.route == "rag" and len(services.generate_answer.call_args.args[1]) == 2
    remote.rerank.assert_not_awaited()


@pytest.mark.parametrize("speculative", [False, True])
@pytest.mark.parametrize("policy,score,expected", [("rrf", 0.001, "rag"), ("remote", 0.5, "fallback"),
    ("remote", 0.8, "rag"), ("local", 0.3, "rag"), ("local", 0.1, "fallback")])
def test_pipeline_refusal_uses_actual_score_source_and_rrf_skips_rescue(speculative, policy, score, expected):
    async def run():
        services = FakeServices().knowledge()
        candidates = [document(1, 0.001), document(2, 0.0001)]
        services.retrieve_candidates = Mock(return_value=candidates)
        remote = Mock(rerank=AsyncMock(return_value=[score, score - 0.1]))
        if policy != "remote":
            remote.rerank.side_effect = AppError(50201, 502, "safe")
        local = AsyncMock(return_value=[score, score - 0.1])
        retriever = Retriever(None, None, remote, rerank_fallback=policy if policy != "remote" else "error",
                              local_rerank=local)
        services.rerank_candidates = retriever.arerank_candidates
        async def retrieve(*args, **kwargs):
            return await retriever.arerank_candidates(kwargs["rerank_query"], candidates, kwargs["top_k"])
        services.retrieve = retrieve
        result = await run_chat(ChatRequest(session_id="threshold", message="q", debug=True),
                                settings=configured(allow_trace=True, speculative_retrieval=speculative), services=services)
        assert result.route == expected
        stages = [item for item in result.trace if item.stage == "rerank"]
        assert stages[0].provider == {"rrf": "fallback_rrf", "local": "fallback_local", "remote": "remote"}[policy]
        if policy == "rrf":
            remote.rerank.assert_awaited_once()
            assert len(services.generate_answer.call_args.args[1]) == 2
        if speculative:
            assert stages[0].speculative is True
    asyncio.run(run())


def test_lazy_local_fallback_runs_in_model_executor_only_after_remote_failure():
    async def run():
        event_thread = threading.get_ident()
        fake = Mock()
        def rerank(query, texts):
            assert threading.get_ident() != event_thread
            return [0.9] * len(texts)
        fake.rerank.side_effect = rerank
        with patch("app.services._embedding_model", return_value=(Mock(), threading.Lock())), \
             patch("app.services.BgeReranker", return_value=fake) as factory:
            services = Services(configured(rerank_fallback="local", rerank_remote_max_retries=0),
                                transport=httpx.MockTransport(lambda r: httpx.Response(503)))
            try:
                factory.assert_not_called()
                for _ in range(2):
                    result = await services.rerank_candidates("q", [document()])
                    assert result[0].score_provider == "fallback_local"
                factory.assert_called_once()
            finally:
                await services.aclose()
    asyncio.run(run())


def test_remote_speculation_cancels_io_without_trace_leaks():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        services = FakeServices()
        services.retrieve_candidates = Mock(return_value=[document()])
        async def remote(query, texts):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        retriever = Retriever(None, None, Mock(rerank=remote))
        services.rerank_candidates = retriever.arerank_candidates
        async def route(*args):
            await entered.wait()
            return RouteDecision(intent="general", rewritten_query="q")
        services.route = route
        result = await run_chat(ChatRequest(session_id="cancel", message="q", debug=True),
                                settings=configured(allow_trace=True), services=services)
        await cancelled.wait()
        assert result.route == "general"
        assert not any(item.stage in {"retrieve", "rerank"} for item in result.trace)
    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.parametrize("speculative", [False, True])
@pytest.mark.parametrize("history", [[], [{"role": "user", "content": "上一轮问题"}]])
def test_real_remote_services_await_io_on_event_loop_in_both_pipeline_paths(speculative, history):
    import json
    async def run():
        event_thread = threading.get_ident()
        def handler(request):
            assert threading.get_ident() == event_thread
            body = json.loads(request.content)
            assert body["query"] == "q"
            return httpx.Response(200, json={"results": [
                {"index": i + 1, "document": text, "relevance_score": 0.9 - i * 0.1}
                for i, text in reversed(list(enumerate(body["documents"])))]})
        with patch("app.services._embedding_model", return_value=(Mock(), threading.Lock())), \
             patch("app.services.BgeReranker", side_effect=AssertionError("no local model")):
            services = Services(configured(speculative_retrieval=speculative, allow_trace=True),
                                transport=httpx.MockTransport(handler))
        services.faq_lookup = Mock(return_value=None)
        services.retrieve_candidates = Mock(return_value=[document(1, 0.03), document(2, 0.02)])
        services.route = AsyncMock(return_value=RouteDecision(intent="knowledge", rewritten_query="q"))
        services.generate_answer = FakeServices().generate_answer
        try:
            result = await run_chat(ChatRequest(session_id="real-remote", message="追问" if history else "q",
                                               history=history, debug=True),
                                    settings=services.settings, services=services)
            assert result.route == "rag" and result.sources[0].score == 0.9
            stage = next(item for item in result.trace if item.stage == "rerank")
            assert stage.provider == "remote" and stage.ms >= 0
            assert stage.speculative == (True if speculative and not history else None)
        finally:
            await services.aclose()
    asyncio.run(run())


def test_local_trace_still_identifies_provider():
    from app.retrieval.reranker import FakeReranker
    retriever = Retriever(None, None, FakeReranker())
    trace = []
    token = items.set(trace)
    try:
        retriever.rerank_candidates("q", [document()])
    finally:
        items.reset(token)
    assert trace[0].provider == "local"


@pytest.mark.parametrize("speculative", [False, True])
@pytest.mark.parametrize("fallback", ["rrf", "local"])
def test_rewrite_remote_failure_uses_rrf_without_comparing_or_merging_scores(speculative, fallback):
    async def run():
        services = FakeServices().knowledge()
        candidates = [document(1, 0.03), document(2, 0.02), document(3, 0.01)]
        services.retrieve_candidates = Mock(return_value=candidates)
        remote = Mock(rerank=AsyncMock(side_effect=[[0.01, 0.15, 0.05], AppError(50201, 502, "safe")]))
        local = AsyncMock(side_effect=ValueError("本地模型不可用"))
        retriever = Retriever(None, None, remote, rerank_fallback=fallback, local_rerank=local)
        services.rerank_candidates = AsyncMock(wraps=retriever.arerank_candidates)
        async def retrieve(queries, rerank_query, top_k):
            return await retriever.arerank_candidates(rerank_query, candidates, top_k)
        services.retrieve = AsyncMock(side_effect=retrieve)
        result = await run_chat(ChatRequest(session_id="rewrite-failure", message="q", debug=True),
                                settings=configured(allow_trace=True, speculative_retrieval=speculative,
                                                    rerank_top_k=2), services=services)
        assert result.route == "rag"
        docs = services.generate_answer.call_args.args[1]
        assert [(doc.parent_id, doc.score, doc.score_provider) for doc in docs] == [
            ("p1", 0.03, "fallback_rrf"), ("p2", 0.02, "fallback_rrf")]
        assert [doc.score for doc in candidates] == [0.03, 0.02, 0.01]
        services.retrieve_candidates.assert_called_once()
        assert all(call.args[1] is candidates for call in services.rerank_candidates.call_args_list)
        assert remote.rerank.await_count == 2
        if fallback == "local":
            local.assert_awaited_once()
        else:
            local.assert_not_awaited()
        assert [item.provider for item in result.trace if item.stage == "rerank"] == ["remote", "fallback_rrf"]
    asyncio.run(run())


@pytest.mark.parametrize("speculative", [False, True])
@pytest.mark.parametrize("context_threshold,expected_ids", [(None, ["p1", "p2", "p3"]),
                                                         (-0.7, ["p1", "p2"]), (-0.4, ["p1"])])
def test_remote_context_uses_its_own_optional_threshold(speculative, context_threshold, expected_ids):
    async def run():
        services = FakeServices().knowledge()
        candidates = [document(i, 0.04 - i * 0.01) for i in range(1, 4)]
        services.retrieve_candidates = Mock(return_value=candidates)
        remote = Mock(rerank=AsyncMock(return_value=[0.5, -0.7, -1.0]))
        retriever = Retriever(None, None, remote)
        services.rerank_candidates = retriever.arerank_candidates
        async def retrieve(queries, rerank_query, top_k):
            return await retriever.arerank_candidates(rerank_query, candidates, top_k)
        services.retrieve = AsyncMock(side_effect=retrieve)
        result = await run_chat(ChatRequest(session_id="remote-context", message="q"),
                               settings=configured(speculative_retrieval=speculative, remote_refuse_threshold=0,
                                                   remote_rag_context_min_score=context_threshold), services=services)
        assert result.route == "rag"
        assert [doc.parent_id for doc in services.generate_answer.call_args.args[1]] == expected_ids
    asyncio.run(run())


def test_remote_context_threshold_does_not_apply_to_local_fallback():
    async def run():
        services = FakeServices().knowledge()
        candidates = [document(i, 0.04 - i * 0.01) for i in range(1, 4)]
        services.retrieve_candidates = Mock(return_value=candidates)
        remote = Mock(rerank=AsyncMock(side_effect=AppError(50201, 502, "safe")))
        local = AsyncMock(return_value=[0.9, 0.1, 0.05])
        retriever = Retriever(None, None, remote, rerank_fallback="local", local_rerank=local)
        services.rerank_candidates = retriever.arerank_candidates
        result = await run_chat(ChatRequest(session_id="local-context", message="q"),
                               settings=configured(remote_rag_context_min_score=-2), services=services)
        assert result.route == "rag"
        docs = services.generate_answer.call_args.args[1]
        assert [(doc.parent_id, doc.score_provider) for doc in docs] == [
            ("p1", "fallback_local"), ("p2", "fallback_local")]
    asyncio.run(run())
