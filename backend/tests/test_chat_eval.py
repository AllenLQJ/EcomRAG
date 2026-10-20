import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.contracts import AppError, FaqHit
from app.llm.generator import GeneratedAnswer
from eval import chat_demo, run_eval
from chat_fakes import FakeServices, document


def test_evaluation_aggregates_routes_source_hits_timings_and_refusals():
    rows = [dict(question=q, expected_route=r, expected_doc=d) for q, r, d in [
        ("首次缓存", "cache", None), ("闲聊", "general", None),
        ("命中资料", "rag", "doc1.md"), ("缺失资料", "rag", "doc2.md"),
        ("拒答", "fallback", None), ("故障", "general", None),
    ]]
    services = FakeServices().knowledge()
    services.faq_lookup.side_effect = lambda q: (
        FaqHit(faq_id=1, question=q, answer="FAQ", score=0.9) if q == "首次缓存" else None)
    original = services.route.return_value
    def route(q, history):
        if q == "故障":
            raise AppError(50201, 502, "模型服务错误")
        return original.model_copy(update={"intent": "general" if q == "闲聊" else "knowledge"})
    services.route.side_effect = route
    services.retrieve.side_effect = lambda queries, rerank_query, top_k: (
        [] if rerank_query == "拒答" else [document().model_copy(update={"doc_id": "hashed-doc1"})])
    report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None, allow_trace=True),
                                           services=services, doc_ids={"doc1.md": "hashed-doc1", "doc2.md": "hashed-doc2"}))
    assert report["total"] == 6 and report["completed"] == 5
    assert report["route_accuracy"] == pytest.approx(5 / 6)
    assert report["rag_total"] == 2 and report["rag_source_hits"] == 1
    assert report["rag_source_hit_rate"] == 0.5
    assert report["refused_questions"] == ["拒答"]
    assert report["errors"] == [{"question": "故障", "code": 50201}]
    assert report["error_distribution"] == {"50201": 1}
    assert len(report["results"]) == 6
    assert [row["error_code"] for row in report["results"]] == [None] * 5 + [50201]
    assert [row["is_error"] for row in report["results"]] == [False] * 5 + [True]
    assert report["route_errors"] == [{"question": "故障", "expected_route": "general", "route": "error", "error_code": 50201}]
    assert set(report["stage_mean_ms"]) == {"cache", "faq", "router", "retrieve", "rerank", "generate", "total"}
    assert all(ms >= 0 for ms in report["stage_mean_ms"].values())


def test_eval_empty_input_and_internal_trace_with_api_trace_disabled():
    settings = Settings(_env_file=None)
    report = asyncio.run(run_eval.evaluate([], settings=settings, services=FakeServices()))
    assert report["route_accuracy"] == 0 and report["stage_mean_ms"] == {}
    assert report["rag_total"] == 0 and report["refused_questions"] == []
    assert report["total_p50_ms"] is None and report["total_p90_ms"] is None
    report = asyncio.run(run_eval.evaluate([{"question": "你好", "expected_route": "general"}],
                                          settings=settings, services=FakeServices()))
    assert set(report["stage_mean_ms"]) == {"cache", "faq", "router", "generate", "total"}
    assert report["trace_enabled"] is True and report["api_trace_allowed"] is False
    row = report["results"][0]
    assert row["answer"] == "你好，我是灵码客服助手。" and row["sources"] == []
    assert row["total_ms"] >= 0 and set(row["stage_ms"]) == set(report["stage_mean_ms"])


def test_expected_doc_matches_ingest_stable_id():
    filename = "after_sales_returns.md"
    assert run_eval.expected_doc_id(filename) == hashlib.sha256(filename.encode()).hexdigest()[:24]


@pytest.mark.parametrize("answer,hit", [("案例说明[2]", True), ("原规则[1]", True),
                                       ("不相关[3]", False),
                                           # 无有效引用时并列重叠度按原顺序选择 original.md。
                                           ("没有有效引用[99]", True)])
def test_source_hit_accepts_any_expected_document_using_only_cited_sources(answer, hit):
    expected = ["original.md", "case.md"]
    services = FakeServices().knowledge()
    services.retrieve.return_value = [document(i, score).model_copy(update={"content": content})
        for i, score, content in [(1, 0.99, "原规则。"), (2, 0.98, "案例说明。"), (3, 0.97, "不相关。")]]
    services.generate_answer.return_value = GeneratedAnswer(answer, set())
    report = asyncio.run(run_eval.evaluate(
        [{"question": "引用", "expected_route": "rag", "expected_doc": expected}],
        settings=Settings(_env_file=None, rag_context_min_score=0), services=services,
        doc_ids={"original.md": "d1", "case.md": "d2"}))
    assert report["rag_total"] == 1
    assert report["rag_source_hits"] == int(hit)
    assert report["results"][0]["source_hit"] is hit
    assert report["results"][0]["expected_doc"] == expected


def test_multi_document_reference_errors_and_refusals_remain_in_denominator(monkeypatch):
    async def chat(request, **kwargs):
        if request.message == "错误":
            raise RuntimeError("offline failure")
        from types import SimpleNamespace
        return SimpleNamespace(route="fallback", answer="无依据", sources=[])

    monkeypatch.setattr(run_eval, "run_chat", chat)
    report = asyncio.run(run_eval.evaluate(
        [{"question": question, "expected_route": "rag", "expected_doc": ["a.md", "b.md"]}
         for question in ("错误", "拒答")], settings=Settings(_env_file=None), services=FakeServices(),
        doc_ids={"a.md": "a", "b.md": "b"}))
    assert report["rag_total"] == 2 and report["rag_source_hits"] == 0
    assert [row["source_hit"] for row in report["results"]] == [False, False]


@pytest.mark.parametrize("expected", [[], "", " ", 1, False, {}, ["a.md", None], [["a.md"]]])
def test_source_reference_validation_precedes_chat(expected):
    with patch.object(run_eval, "run_chat", new_callable=AsyncMock) as chat, \
            pytest.raises(ValueError, match="expected_doc"):
        asyncio.run(run_eval.evaluate(
            [{"question": "标注错误", "expected_route": "rag", "expected_doc": expected}],
            settings=Settings(_env_file=None), services=FakeServices()))
    chat.assert_not_called()


@pytest.mark.parametrize("answer,hit,ids", [("说明[1]", False, ["d1"]),
                                          ("说明[2]", True, ["d2"]),
                                          ("没有有效引用[99]", False, ["d1"])])
def test_rag_source_hit_counts_filtered_sources_not_uncited_candidates(answer, hit, ids):
    rows = [{"question": "引用测试", "expected_route": "rag", "expected_doc": "doc2.md"}]
    services = FakeServices().knowledge()
    services.retrieve.return_value = [document(score=0.995), document(2, 0.036), document(3, 0.02)]
    services.generate_answer.return_value = GeneratedAnswer(answer, set())
    report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None, rag_context_min_score=0), services=services,
                                           doc_ids={"doc2.md": "d2"}))
    assert report["rag_source_hits"] == int(hit)
    assert report["rag_source_hit_rate"] == float(hit)
    assert report["results"][0]["source_hit"] is hit
    assert [source["doc_id"] for source in report["results"][0]["sources"]] == ids


@pytest.mark.parametrize("module,args", [(chat_demo, ["你好"]), (run_eval, [])])
@pytest.mark.parametrize("injected", [False, True])
def test_chat_clis_runtime_settings_real_assembly_and_cleanup(module, args, injected, capsys, tmp_path):
    from app import config
    settings = Settings(_env_file=None, allow_trace=True)
    services = FakeServices()
    services.aclose = AsyncMock()
    kwargs = {"settings": settings} if injected else {}
    output_file = tmp_path / "eval.json"
    if module is run_eval:
        args = [*args, "--output", str(output_file)]
    with patch.object(config, "get_settings", return_value=settings) as get_settings, \
         patch.object(module, "Services", return_value=services) as factory:
        assert module.main(args, **kwargs) == 0
    assert get_settings.call_count == (0 if injected else 1)
    factory.assert_called_once_with(settings)
    services.warmup.assert_awaited_once()
    services.aclose.assert_awaited_once()
    terminal = capsys.readouterr().out
    if module is chat_demo:
        output = json.loads(terminal)
        assert output["route"] == "general" and output["is_mock"] is False
    else:
        output = json.loads(output_file.read_text())
        assert output["total"] == 24 and output["completed"] == 24
        assert all({"answer", "sources", "total_ms", "stage_ms"} <= row.keys() for row in output["results"])
        assert "路由准确率" in terminal and '"results"' not in terminal


def test_eval_stage_means_use_only_observed_samples():
    from types import SimpleNamespace
    async def fake_chat(chat, *, trace_sink, **kwargs):
        trace = [("cache", 3), ("router", 5)] if chat.message == "第一" else [("cache", 7)]
        trace_sink.extend(SimpleNamespace(stage=s, ms=ms) for s, ms in trace)
        return SimpleNamespace(route="general", sources=[], answer="你好", trace=None)
    rows = [{"question": "第一", "expected_route": "general"}, {"question": "第二", "expected_route": "general"}]
    with patch.object(run_eval, "run_chat", new=fake_chat):
        report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None, allow_trace=False),
                                               services=FakeServices()))
    assert report["stage_mean_ms"] == {"cache": 5, "router": 5}
    assert report["stage_p50_ms"] == {"cache": 5, "router": 5}
    assert report["stage_p90_ms"] == {"cache": pytest.approx(6.6), "router": 5}
    assert report["route_accuracy"] == 1
    assert report["results"][0]["stage_ms"] == {"cache": 3, "router": 5}
    assert report["results"][1]["stage_ms"] == {"cache": 7}


def test_eval_preserves_all_failed_rows_and_prints_error_distribution(capsys):
    rows = [{"question": str(i), "expected_route": "rag", "expected_doc": "doc.md"} for i in range(3)]
    services = FakeServices()
    services.route.side_effect = [AppError(50201, 502, "模型服务暂时不可用"),
                                 AppError(50201, 502, "模型服务暂时不可用"),
                                 AppError(50401, 504, "请求超时，请稍后再试")]
    report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None), services=services))
    assert report["completed"] == 0 and report["total"] == 3
    assert report["route_accuracy"] == 0 and report["rag_source_hit_rate"] == 0
    assert report["error_distribution"] == {"50201": 2, "50401": 1}
    assert [row["error_code"] for row in report["results"]] == [50201, 50201, 50401]
    assert all(row["is_error"] and row["route"] == "error" for row in report["results"])
    run_eval.print_report(report)
    output = capsys.readouterr().out
    assert "错误分布：50201 ×2，50401 ×1" in output
    assert "期望=rag | 实际=error" in output


@pytest.mark.parametrize("allow_trace", [False, True])
def test_eval_exports_answer_sources_and_internal_times_independent_of_api_trace(allow_trace):
    rows = [{"question": "检索问题", "expected_route": "rag", "expected_doc": "doc1.md"}]
    services = FakeServices().knowledge()
    report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None, allow_trace=allow_trace),
                                           services=services, doc_ids={"doc1.md": "d1"}))
    row = report["results"][0]
    assert row["answer"] == "说明[1]"
    assert row["sources"] == [{"index": 1, "doc_id": "d1", "title": "资料1", "score": 0.8}]
    assert row["total_ms"] >= 0
    assert set(row["stage_ms"]) == {"cache", "faq", "router", "retrieve", "rerank", "generate", "total"}
    assert row["stage_ms"] == report["stage_mean_ms"]
    assert report["trace_enabled"] is True and report["api_trace_allowed"] is allow_trace


def test_failed_eval_rows_keep_partial_internal_times_and_empty_answer_sources():
    services = FakeServices()
    services.route.side_effect = AppError(50201, 502, "模型服务暂时不可用")
    report = asyncio.run(run_eval.evaluate([{"question": "问题", "expected_route": "general"}],
                                          settings=Settings(_env_file=None, allow_trace=False), services=services))
    row = report["results"][0]
    assert row["is_error"] and row["error_code"] == 50201
    assert row["answer"] == "" and row["sources"] == [] and row["total_ms"] >= 0
    assert set(row["stage_ms"]) == {"cache", "faq", "total"}
    assert row["stage_ms"] == report["stage_mean_ms"]


def test_eval_sensitive_refusal_has_no_generation_or_retrieval_times():
    from app.contracts import RouteDecision
    from app.pipeline import SENSITIVE_REFUSAL
    services = FakeServices()
    services.route.return_value = RouteDecision(intent="sensitive", rewritten_query="敏感请求")
    report = asyncio.run(run_eval.evaluate([{"question": "内部指令请求", "expected_route": "fallback"}],
                                          settings=Settings(_env_file=None), services=services))
    row = report["results"][0]
    assert row["answer"] == SENSITIVE_REFUSAL and row["sources"] == []
    assert row["route"] == "fallback" and not row["is_error"]
    assert set(row["stage_ms"]) == {"cache", "faq", "router", "total"}
    services.generate_answer.assert_not_awaited()
    services.chat_general.assert_not_awaited()


def test_latency_percentiles_include_failed_requests_and_observed_stages_only():
    from types import SimpleNamespace
    async def fake_chat(chat, *, trace_sink, **kwargs):
        trace_sink.append(SimpleNamespace(stage="cache", ms=3 if chat.message == "成功" else 7))
        if chat.message == "失败":
            raise AppError(50201, 502, "模型服务暂时不可用")
        trace_sink.append(SimpleNamespace(stage="generate", ms=5))
        return SimpleNamespace(route="general", sources=[], answer="你好")
    rows = [{"question": q, "expected_route": "general"} for q in ("成功", "失败")]
    with patch.object(run_eval, "run_chat", new=fake_chat), \
            patch.object(run_eval, "perf_counter", side_effect=[0, 0.010, 1, 1.030]):
        report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None), services=FakeServices()))
    assert report["total_p50_ms"] == pytest.approx(20)
    assert report["total_p90_ms"] == pytest.approx(28)
    assert report["stage_p50_ms"] == {"cache": 5, "generate": 5}
    assert report["stage_p90_ms"] == {"cache": pytest.approx(6.6), "generate": 5}
    assert report["completed"] == 1 and report["results"][1]["is_error"]
