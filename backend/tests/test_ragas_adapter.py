"""单轮映射与评估专用钩子；所有生成/检索均为替身。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.contracts import FaqHit, RouteDecision
from app.llm.generator import GeneratedAnswer, Generator
from app.llm.prompts import block
from app.pipeline import run_chat
from app.schemas import ChatRequest
from chat_fakes import FakeServices, document
from eval.ragas_adapter import not_applicable_category, to_sample


def test_maps_full_ordered_contexts_and_reference():
    case = {"question": "问题", "answer": "答案[2]", "reference": "标准答案", "route": "rag",
            "contexts": [{"index": 1, "text": "全文甲"}, {"index": 2, "text": "全文乙"}],
            "sources": [{"index": 2}]}
    assert to_sample(case) == {"status": "applicable", "reason": None, "sample": {
        "user_input": "问题", "response": "答案[2]", "retrieved_contexts": ["全文甲", "全文乙"],
        "reference": "标准答案"}}


@pytest.mark.parametrize("route", ["faq", "cache", "general", "sensitive", "fallback"])
def test_non_rag_routes_are_explicitly_not_applicable(route):
    mapped = to_sample({"question": "问题", "answer": "答", "route": route, "contexts": []})
    assert mapped["status"] == "not_applicable"
    assert mapped["reason"] == f"route_{route}"
    assert mapped["sample"]["retrieved_contexts"] == []


def test_missing_rag_context_and_collection_errors_remain_visible():
    assert to_sample({"question": "问题", "route": "rag"})["status"] == "invalid"
    assert to_sample({"question": "问题", "route": "rag", "contexts": [{"text": ""}]})["status"] == "invalid"
    assert to_sample({"question": "问题", "route": "error"})["status"] == "error"


@pytest.mark.parametrize("stream", [False, True])
def test_collector_matches_generator_order_and_preserves_default_response(stream):
    settings = Settings(_env_file=None, speculative_retrieval=False, rag_context_min_score=0.1)
    history = [{"role": "user", "content": "历史问题"}]
    request = ChatRequest(session_id="fixed", message="问题", history=history)
    fake = FakeServices().knowledge()
    docs = [document(i, score).model_copy(update={"content": "父块完整内容。答案。"})
            for i, score in [(1, 0.8), (2, 0.01), (3, 0.7)]]
    fake.retrieve.return_value = docs
    fake.generate_answer.return_value = GeneratedAnswer("答案[2]", {2})
    collected, deltas = [], []

    async def delta(value):
        deltas.append(value)

    options = {"on_delta": delta} if stream else {}
    default = asyncio.run(run_chat(request, "fixed", settings=settings, services=fake, **options))
    explicit_none = asyncio.run(run_chat(request, "fixed", settings=settings, services=fake,
                                         collector=None, **options))
    evaluated = asyncio.run(run_chat(request, "fixed", settings=settings, services=fake,
                                     collector=collected.append, **options))
    assert default.model_dump() == explicit_none.model_dump() == evaluated.model_dump()
    assert set(default.model_dump()) == {
        "request_id", "session_id", "answer", "route", "strategy", "sources", "is_mock", "trace"}
    event = collected[0]
    assert event["intent"] == "knowledge" and event["refuse_reason"] is None
    assert event["history"] == history and event["answer"] == "答案[2]" and event["route"] == "rag"
    assert event["contexts"] == [
        {"index": 1, "doc_id": "d1", "title": "资料1", "score": 0.8, "text": f"[1] 资料1\n{docs[0].content}"},
        {"index": 2, "doc_id": "d3", "title": "资料3", "score": 0.7, "text": f"[2] 资料3\n{docs[2].content}"}]
    assert event["sources"] == [{"index": 2, "doc_id": "d3", "title": "资料3", "score": 0.7}]
    prompt = Generator(None, "fake")._rag_messages("问题", [docs[0], docs[2]], history)
    assert block("资料", "\n\n".join(context["text"] for context in event["contexts"])) in prompt[1]["content"]
    assert "父块完整内容" not in json.dumps(default.model_dump(), ensure_ascii=False)


@pytest.mark.parametrize("mode,intent,refuse_reason,route", [
    ("sensitive", "sensitive", "sensitive", "fallback"),
    ("no_docs", "knowledge", "no_docs", "fallback"),
    ("low_score", "knowledge", "low_score", "fallback"),
    ("no_answer", "knowledge", "no_answer", "fallback"),
    ("general_no_answer", "general", "no_answer", "fallback"),
    ("general", "general", None, "general"),
    ("faq", "faq", None, "faq"), ("cache", "cache", None, "cache"),
])
@pytest.mark.parametrize("stream", [False, True])
def test_collection_only_intent_and_refusal_reasons(mode, intent, refuse_reason, route, stream):
    fake = FakeServices()
    if mode in {"no_docs", "low_score", "no_answer"}:
        fake.knowledge()
    if mode == "sensitive":
        fake.route.return_value = RouteDecision(intent="sensitive", rewritten_query="问题")
    elif mode == "no_docs":
        fake.retrieve.return_value = []
    elif mode == "low_score":
        fake.retrieve.return_value = [document(score=0.01).model_copy(update={"score_provider": "local"})]
    elif mode == "no_answer":
        fake.generate_answer.return_value = GeneratedAnswer("NO_ANSWER", set())
    elif mode == "general_no_answer":
        fake.chat_general.return_value = "NO_ANSWER"
    elif mode in {"faq", "cache"}:
        fake.faq_lookup.return_value = FaqHit(faq_id=1, question="问题", answer="答案", score=0.99, source=mode)
    captured = []

    async def delta(text):
        pass

    result = asyncio.run(run_chat(
        ChatRequest(session_id="s", message="问题"), services=fake,
        settings=Settings(_env_file=None, speculative_retrieval=False), collector=captured.append,
        **({"on_delta": delta} if stream else {})))
    assert result.route == route
    assert "intent" not in result.model_dump() and "refuse_reason" not in result.model_dump()
    assert captured[0]["intent"] == intent and captured[0]["refuse_reason"] == refuse_reason
    if mode == "sensitive":
        assert to_sample(captured[0])["reason"] == "route_sensitive"
        assert not_applicable_category(captured[0]) == "sensitive"


def test_sensitive_metadata_and_legacy_unknown_are_distinguished():
    legacy = {"question": "问题", "route": "fallback", "contexts": []}
    assert to_sample(legacy)["status"] == "not_applicable"
    assert not_applicable_category(legacy) == "unknown"
    assert not_applicable_category({**legacy, "intent": "knowledge", "refuse_reason": "no_docs"}) == "fallback"
    for fields in ({"intent": "sensitive"}, {"refuse_reason": "sensitive"}):
        assert to_sample({**legacy, **fields})["reason"] == "route_sensitive"
        assert not_applicable_category({**legacy, **fields}) == "sensitive"


def test_default_reads_body_for_alignment_without_exporting_collected_contexts():
    reads = []

    class GuardDocument(SimpleNamespace):
        @property
        def content(self):
            reads.append(True)
            return "父块完整内容。说明。"

    fake = FakeServices().knowledge()
    fake.retrieve.return_value = [GuardDocument(doc_id="d", title="标题", snippet="摘要", score=0.8,
                                               score_provider="local")]
    result = asyncio.run(run_chat(ChatRequest(session_id="s", message="问题"),
                                 settings=Settings(_env_file=None, speculative_retrieval=False), services=fake))
    assert result.route == "rag"
    assert reads  # 引用校验和展示片段现在都必须使用模型读取的正文。
    assert result.sources[0].snippet == "说明。"
    assert "contexts" not in result.model_dump()


@pytest.mark.parametrize("endpoint", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_http_never_supplies_collector_or_runs_evaluation(endpoint, app_factory, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, streaming
    from eval import ragas_eval

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP 不得启动评估")

    monkeypatch.setattr(ragas_eval, "make_scorer", forbidden)
    called = []

    async def checked(*args, **kwargs):
        assert "collector" not in kwargs
        called.append(True)
        return await run_chat(*args, **kwargs)

    monkeypatch.setattr(main, "run_chat", checked)
    monkeypatch.setattr(streaming, "run_chat", checked)
    app = app_factory(speculative_retrieval=False)
    app.state.services.knowledge()
    with TestClient(app) as client:
        result = client.post(endpoint, json={"session_id": "s", "message": "问题"})
    assert result.status_code == 200 and called == [True]
    assert "父块完整内容" not in result.text
    assert '"retrieved_contexts"' not in result.text and '"contexts"' not in result.text
