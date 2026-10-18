import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import AppError, FaqHit
from app.llm.client import LLMClient, _semaphore
from app.llm.generator import GeneratedAnswer, Generator
from app.pipeline import REFUSAL, run_chat
from app.schemas import ChatRequest
from chat_fakes import FakeServices, document
from deadline_helpers import ControlledDeadlines


def run(services, *, debug=True, **overrides):
    # 此组保留顺序路径的契约；默认启用的投机路径另用 Event 验证。
    overrides.setdefault("speculative_retrieval", False)
    return asyncio.run(run_chat(ChatRequest(session_id="test", message="原问题", debug=debug),
                                settings=Settings(_env_file=None, **overrides), services=services))


@pytest.mark.parametrize("source", ["cache", "faq"])
def test_faq_and_cache_routes_skip_llm(source):
    services = FakeServices()
    services.faq_lookup.return_value = FaqHit(faq_id=3, question="FAQ 问句", answer="答" * 80,
                                             score=0.93, source=source)
    result = run(services, allow_trace=True)
    assert result.route == source and result.answer == "答" * 80
    assert result.is_mock is False and result.strategy is None
    assert result.sources[0].model_dump() == {
        "index": 1, "doc_id": "faq:3", "title": "FAQ 问句", "snippet": "答" * 60, "score": 0.93,
    }
    services.route.assert_not_awaited()
    services.generate_answer.assert_not_awaited()
    assert result.trace[0].stage == "cache"
    assert result.trace[0].hit == (source == "cache")


def test_general_route():
    services = FakeServices()
    result = run(services)
    assert result.route == "general" and not result.sources and result.strategy is None
    services.route.assert_awaited_once_with("原问题", [])
    services.chat_general.assert_awaited_once_with("原问题", [])
    services.retrieve.assert_not_called()


@pytest.mark.parametrize("speculative", [False, True])
def test_configured_top_k_reaches_sequential_and_speculative_retrieval(speculative):
    services = FakeServices().knowledge()
    services.rerank_candidates = AsyncMock(return_value=[document()])
    assert run(services, speculative_retrieval=speculative, rerank_top_k=3).route == "rag"
    if speculative:
        services.rerank_candidates.assert_awaited_once_with("原问题", [document()], top_k=3)
    else:
        services.retrieve.assert_called_once_with(["改写后的检索问题"], rerank_query="原问题", top_k=3)


@pytest.mark.parametrize("answer,cited,indices", [("回答[2]", {2}, [2]), ("回答", set(), [1]),
                                                 ("重复[2][2]与[1]", {1, 2}, [1, 2]),
                                                 ("回答[2]", {1}, [2])])
def test_rag_sources_preserve_original_indices(answer, cited, indices):
    services = FakeServices().knowledge()
    docs = [document(), document(2)]
    services.retrieve.return_value = docs
    services.generate_answer.return_value = GeneratedAnswer(answer, cited)
    result = run(services, allow_trace=True)
    assert result.route == "rag" and result.strategy == "direct" and not result.is_mock
    assert [source.index for source in result.sources] == indices
    assert [source.doc_id for source in result.sources] == [f"d{i}" for i in indices]
    services.retrieve.assert_called_once_with(["改写后的检索问题"], rerank_query="原问题", top_k=5)
    services.generate_answer.assert_awaited_once_with("原问题", docs, [])
    assert [item.stage for item in result.trace] == ["cache", "faq", "router", "retrieve", "rerank", "generate", "total"]
    assert result.trace[2].intent == "knowledge"
    assert result.trace[3].candidates == 2 and result.trace[4].kept == 2


@pytest.mark.parametrize("answer,expected", [("事实[1]", [1]), ("事实[3]", [3]),
                                           ("没有引用", [2]), ("无效[0][4][999]", [2]),
                                           ("代码里的 `[1]` 不算引用", [2])])
def test_sources_use_actual_citations_or_highest_score_only(answer, expected):
    services = FakeServices().knowledge()
    services.retrieve.return_value = [document(score=0.036), document(2, score=0.995), document(3, score=0.02)]
    services.generate_answer.return_value = GeneratedAnswer(answer, set())
    result = run(services, rag_context_min_score=0)  # 单独验证引用过滤；上下文筛分另测默认值。
    assert result.route == "rag"
    assert [source.index for source in result.sources] == expected
    assert [source.doc_id for source in result.sources] == [f"d{i}" for i in expected]
    if answer.startswith("无效"):
        assert result.answer == "无效"


@pytest.mark.parametrize("score,expected", [(None, "fallback"), (0.088, "fallback"), (0.1396, "fallback"),
                                           (0.1999, "fallback"), (0.2, "rag"), (0.2001, "rag"),
                                           (0.44, "rag"), (0.4999, "rag"), (0.5, "rag"), (0.5001, "rag")])
def test_refusal_boundary_and_skips_generation(score, expected):
    services = FakeServices().knowledge()
    services.retrieve.return_value = [] if score is None else [document(score=score)]
    result = run(services)
    assert result.route == expected
    if expected == "fallback":
        assert result.answer == REFUSAL and result.sources == []
        services.generate_answer.assert_not_awaited()
    else:
        services.generate_answer.assert_awaited_once()


@pytest.mark.parametrize("docs,expected", [([], "fallback"), ([document(score=0.001)], "rag")])
def test_disabled_rerank_never_compares_rrf_to_refusal_threshold(docs, expected):
    services = FakeServices().knowledge()
    services.retrieve.return_value = docs
    assert run(services, rerank_enabled=False).route == expected
    if docs:
        services.generate_answer.assert_awaited_once()
    else:
        services.generate_answer.assert_not_awaited()


def test_default_context_filter_excludes_unrelated_low_scores_before_generation():
    services = FakeServices().knowledge()
    docs = [document(score=0.995), document(2, 0.036), document(3, 0.02)]
    services.retrieve.return_value = docs
    result = run(services)
    services.generate_answer.assert_awaited_once_with("原问题", [docs[0]], [])
    assert result.route == "rag"
    assert [(source.index, source.doc_id) for source in result.sources] == [(1, "d1")]


@pytest.mark.parametrize("enabled,expected_ids", [(True, ["d1", "d3", "d4"]),
                                                (False, ["d1", "d2", "d3", "d4", "d5"])])
def test_context_score_boundary_and_disabled_rerank(enabled, expected_ids):
    services = FakeServices().knowledge()
    services.retrieve.return_value = [document(score=0.995), document(2, 0.036), document(3, 0.2),
                                      document(4, 0.1), document(5, 0.0999)]
    result = run(services, rerank_enabled=enabled)
    sent = services.generate_answer.call_args.args[1]
    assert [doc.doc_id for doc in sent] == expected_ids
    assert result.route == "rag"


def test_context_always_keeps_highest_even_below_context_minimum_and_not_first():
    services = FakeServices().knowledge()
    docs = [document(score=0.1), document(2, 0.6), document(3, 0.2)]
    services.retrieve.return_value = docs
    result = run(services, rag_context_min_score=0.9)
    services.generate_answer.assert_awaited_once_with("原问题", [docs[1]], [])
    assert [(source.index, source.doc_id) for source in result.sources] == [(1, "d2")]


def test_generator_and_response_share_renumbered_filtered_sources():
    services = FakeServices().knowledge()
    services.retrieve.return_value = [document(score=0.995), document(2, 0.036), document(3, 0.2)]
    llm = AsyncMock()
    llm.complete.return_value = "已知事实[2]"
    services.generate_answer = Generator(llm, "fake-model").generate_answer
    result = run(services)
    materials = llm.complete.call_args.kwargs["messages"][1]["content"]
    assert "[1] 资料1" in materials and "[2] 资料3" in materials
    assert "资料2" not in materials and "[3]" not in materials
    assert result.answer == "已知事实[2]"
    assert [(source.index, source.doc_id) for source in result.sources] == [(2, "d3")]


@pytest.mark.parametrize("marker", ["NO_ANSWER", " \nno_answer\t ", "【No_Answer】。", "!!!NO_ANSWER???",
                                   "n o _ a n s w e r", "`NO_ANSWER`", "NO_ANSWER[1]",
                                   "NO_ANSWER。 nO_aNsWeR"])
def test_generator_no_answer_returns_fixed_fallback_and_safe_reason(marker, caplog):
    caplog.set_level("INFO", logger="app.pipeline")
    services = FakeServices().knowledge()
    services.generate_answer.return_value = GeneratedAnswer(marker, set())
    result = run(services, llm_api_key="PRIVATE_KEY_NO_LOG")
    assert result.route == "fallback" and result.answer == REFUSAL and result.sources == []
    assert "NO_ANSWER" not in result.answer.upper()
    assert sum("reason=generator_no_answer" in record.getMessage() for record in caplog.records) == 1
    assert "原问题" not in caplog.text and "PRIVATE_KEY_NO_LOG" not in caplog.text


@pytest.mark.parametrize("answer", ["已知部分[1]，NO_ANSWER，其余资料未提及。",
                                   "nO_aNsWeR 已知部分[1]。", "已知部分[1]。NO_ANSWER",
                                   "已知部分[1]，其余资料里没有。"])
def test_partial_answers_remain_rag_and_never_leak_internal_marker(answer):
    services = FakeServices().knowledge()
    services.generate_answer.return_value = GeneratedAnswer(answer, {1})
    result = run(services)
    assert result.route == "rag" and result.sources[0].index == 1
    assert "已知部分[1]" in result.answer and "NO_ANSWER" not in result.answer.upper()


def test_general_generator_marker_is_also_hidden():
    services = FakeServices()
    services.chat_general.return_value = "【no_answer】"
    result = run(services)
    assert result.route == "fallback" and result.answer == REFUSAL and result.sources == []


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("allow", [False, True])
def test_trace_requires_both_flags(debug, allow):
    result = run(FakeServices(), debug=debug, allow_trace=allow)
    assert (result.trace is not None) == (debug and allow)


def test_request_timeout_and_cancelled_generation():
    services = FakeServices()

    async def blocked(*args):
        await asyncio.Event().wait()
    services.route = blocked
    with pytest.raises(AppError) as caught:
        run(services, request_timeout_s=0.01)
    assert (caught.value.code, caught.value.http_status) == (50401, 504)


@pytest.mark.parametrize("stage", ["faq_lookup", "retrieve"])
@pytest.mark.parametrize("error_type", [ConnectionError,
    type("OperationalError", (Exception,), {"__module__": "pymysql.err"}),
    type("MilvusException", (Exception,), {"__module__": "pymilvus.exceptions"}),
])
def test_database_unavailable_maps_to_503(stage, error_type):
    services = FakeServices().knowledge()
    getattr(services, stage).side_effect = error_type("私密连接信息")
    with pytest.raises(AppError) as caught:
        run(services)
    assert (caught.value.code, caught.value.http_status, caught.value.message) == (
        50301, 503, "服务暂时不可用，请稍后再试")


@pytest.mark.parametrize("error", [TypeError("bug"), ValueError("bug"), FileNotFoundError("本地模型缺失")])
@pytest.mark.parametrize("stage", ["faq_lookup", "retrieve"])
def test_programming_and_model_errors_are_not_swallowed(stage, error):
    services = FakeServices().knowledge()
    getattr(services, stage).side_effect = error
    with pytest.raises(type(error)) as caught:
        run(services)
    assert caught.value is error


def test_queue_wait_counts_toward_request_timeout(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(1))
        calls = []

        async def handler(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})
        settings = Settings(_env_file=None, llm_api_key="TEST", llm_base_url="https://model.test/v1",
                            llm_max_concurrency=1, request_timeout_s=60, llm_timeout_s=60,
                            speculative_retrieval=False)
        client = LLMClient(settings, transport=httpx.MockTransport(handler))
        services = FakeServices()
        services.faq_lookup = AsyncMock(return_value=None)

        async def general(*args):
            return await client.complete(model="test", messages=[], temperature=0, max_tokens=10)
        services.chat_general = general
        holder = asyncio.create_task(general())
        queued = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            queued = asyncio.create_task(run_chat(ChatRequest(session_id="queue", message="你好"),
                                                  settings=settings, services=services))
            assert await asyncio.wait_for(deadlines.waiters.get(), 5) is queued
            # 只触发排队请求的整请求 deadline；持有者有独立、充裕的预算。
            deadlines.expire(queued)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(queued, 5)
            assert (caught.value.code, caught.value.http_status) == (50401, 504)
            assert not holder.done()
            assert len(calls) == 1  # 排队没有启动 HTTP 传输或单次调用超时。
            release.set()
            assert await asyncio.wait_for(holder, 5) == "你好"
        finally:
            release.set()
            tasks = [task for task in (holder, queued) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 清理不得用另一个任务的异常覆盖测试本身的失败。
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
    asyncio.run(scenario())


def test_api_injection_does_not_construct_real_services(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "Services", lambda *a: pytest.fail("不得创建真实服务"))
    fake = FakeServices().knowledge()
    app = main.create_app(Settings(_env_file=None, allow_trace=True), services=fake)
    app.state.rate_limiter.cache._client = lambda: None
    with TestClient(app) as client:
        response = client.post("/api/v1/chat", json={"session_id": "test", "message": "问题", "debug": True})
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "rag" and body["is_mock"] is False
    assert body["sources"][0]["index"] == 1
    assert body["trace"] and body["request_id"] == response.headers["X-Request-ID"]
    fake.warmup.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_lifespan_warmup_switch(enabled):
    from app.main import create_app
    fake = FakeServices()
    with TestClient(create_app(Settings(_env_file=None, warmup_on_start=enabled), services=fake)):
        pass
    assert fake.warmup.await_count == int(enabled)


def test_real_service_trace_metrics_keep_worker_context():
    from app.faq.cache import FaqCache
    from app.faq.repo import FaqRecord
    from app.faq.service import FaqService
    from app.retrieval.embedder import FakeEmbedder
    from faq_fakes import FakeRepo, FakeRedis, FakeBM25

    faq = FaqService(FakeRepo([FaqRecord(1, "原问题", "答案")]), FaqCache(FakeRedis()),
                     FakeEmbedder(), tokenizer=list, index_factory=FakeBM25)
    services = FakeServices()
    services.faq_lookup = faq.faq_lookup
    result = run(services, allow_trace=True)
    assert result.route == "faq"
    assert [item.stage for item in result.trace] == ["cache", "faq", "total"]
    assert result.trace[1].top_score == pytest.approx(1)


def test_database_os_errors_mapped_at_io_boundary():
    from app.faq.repo import FaqRepo
    from app.retrieval.embedder import FakeEmbedder
    from app.retrieval.retriever import Retriever
    from unittest.mock import Mock

    services = FakeServices().knowledge()
    services.faq_lookup = lambda question: FaqRepo(connection_factory=Mock(side_effect=OSError("connection"))).load_enabled()
    with pytest.raises(AppError) as caught:
        run(services)
    assert caught.value.code == 50301
    services = FakeServices().knowledge()
    milvus = Mock()
    milvus.hybrid_search.side_effect = OSError("connection")
    retriever = Retriever(milvus, FakeEmbedder(), rerank_enabled=False,
                          request_factory=lambda **kwargs: kwargs, rrf_factory=lambda **kwargs: kwargs)
    services.retrieve = retriever.retrieve
    with pytest.raises(AppError) as caught:
        run(services)
    assert caught.value.code == 50301


def test_api_trace_switch_and_timeout_cover_entry(app_factory):
    app = app_factory(allow_trace=True, request_timeout_s=0.01)
    async def slow_limit(*args):
        await asyncio.Event().wait()
    app.state.rate_limiter.check_async = slow_limit
    with TestClient(app) as client:
        response = client.post("/api/v1/chat", json={"session_id": "s", "message": "问题"})
    assert response.status_code == 504
    assert response.json()["code"] == 50401
    app.state.services.route.assert_not_awaited()
