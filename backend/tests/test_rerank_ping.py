import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from app import config
from app.config import Settings
from eval import rerank_ping, check_retrieval
from chat_fakes import document

KEY = "PING_REMOTE_KEY_111"


def settings():
    return Settings(_env_file=None, llm_api_key=KEY, llm_base_url="https://vendor.example/v4",
                    rerank_remote_max_retries=0)


@pytest.mark.parametrize("mode,base,descending", [("zero", "从 0 开始", True),
    ("one", "从 1 开始", True), ("shuffled", "从 0 开始", False), ("no_index", "未知", True)])
def test_ping_measures_real_index_scores_order_usage_without_printing_text(mode, base, descending, capsys):
    texts = []
    def handler(request):
        body = json.loads(request.content)
        assert body["query"] == "怎么申请退货" and len(body["documents"]) == 3
        texts.extend(body["documents"])
        results = [{"index": i + (mode == "one"), "document": {"text": text}, "relevance_score": score}
                   for i, (text, score) in enumerate(zip(body["documents"], [2.1, 0.3, -0.7]))]
        if mode == "shuffled":
            results = results[1:] + results[:1]
        if mode == "no_index":
            for result in results:
                result.pop("index")
        return httpx.Response(200, json={"results": results, "usage": {"prompt_tokens": 30, "total_tokens": 30,
                                                                 "private": KEY + texts[0]}})
    assert rerank_ping.main([], settings=settings(), transport=httpx.MockTransport(handler)) == 0
    output = capsys.readouterr().out
    row = json.loads(output)
    assert row["host"] == "vendor.example" and row["model"] == "rerank"
    assert row["success"] is True and row["returned_count"] == 3 and row["ms"] >= 0
    assert row["index_base"] == base and row["descending"] is descending
    assert row["score_min"] == -0.7 and row["score_max"] == 2.1
    assert row["usage"] == {"prompt_tokens": 30, "total_tokens": 30}
    assert row["index_min"] == (None if mode == "no_index" else 1 if mode == "one" else 0)
    assert row["index_max"] == (None if mode == "no_index" else 3 if mode == "one" else 2)
    assert KEY not in output and "怎么申请退货" not in output
    assert all(text not in output for text in texts)


@pytest.mark.parametrize("mode,category", [("missing", "bad_response"), ("401", "http_status"),
    ("timeout", "timeout"), ("network", "network")])
def test_ping_safe_failure_diagnostics(mode, category, capsys, caplog):
    texts = []
    def handler(request):
        body = json.loads(request.content)
        texts.extend([body["query"], *body["documents"]])
        if mode == "timeout":
            raise httpx.ReadTimeout(KEY)
        if mode == "network":
            raise httpx.ConnectError(KEY)
        if mode == "missing":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(401, json={"error": {"code": "invalid", "message": KEY + "".join(texts)}})
    assert rerank_ping.main([], settings=settings(), transport=httpx.MockTransport(handler)) == 1
    output = capsys.readouterr().out
    row = json.loads(output)
    assert row["success"] is False and row["diagnostic"]["category"] == category
    assert all(text not in output + caplog.text for text in [KEY, *texts])


def test_ping_loads_runtime_settings_only_when_not_injected(monkeypatch, capsys):
    getter = Mock(return_value=settings())
    monkeypatch.setattr(config, "get_settings", getter)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [
        {"index": i, "relevance_score": 0.5} for i in range(3)]}))
    assert rerank_ping.main([], transport=transport) == 0
    getter.assert_called_once_with()


def test_async_retrieval_evaluation_reports_midpoint_and_timing(capsys):
    class Fake:
        def retrieve_candidates(self, queries):
            return [document().model_copy(update={"source": "a.md"})]
        async def rerank_candidates(self, question, candidates, top_k):
            score = 2.0 if question == "rag" else -0.5
            return [doc.model_copy(update={"score": score, "score_provider": "remote"}) for doc in candidates]
    rows = [{"question": "rag", "expected_route": "rag", "expected_doc": "a.md"},
            {"question": "refuse", "expected_route": "fallback"}]
    report = asyncio.run(check_retrieval.evaluate_async(rows, Fake(), refuse_threshold=0.75))
    assert report["hit_at_1"] == report["hit_at_5"] == 1.0
    assert report["rag_min_score"] == 2.0 and report["fallback_max_score"] == -0.5
    assert report["suggested_threshold"] == 0.75
    assert report["rag"][0]["rerank_ms"] >= 0
    check_retrieval.print_report(report)
    assert "建议阈值=0.75" in capsys.readouterr().out


@pytest.mark.parametrize("explicit", [False, True])
def test_check_retrieval_remote_cli_uses_configured_provider_without_loading_local(monkeypatch, capsys, explicit):
    from app.retrieval import remote_reranker, retriever, reranker, embedder
    from app.retrieval.retriever import Retriever
    actual_remote = remote_reranker.RemoteReranker
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"index": i, "document": text, "relevance_score": 0.8}
            for i, text in enumerate(body["documents"])]})
    def create(settings):
        return actual_remote(settings, transport=httpx.MockTransport(handler))
    def create_retriever(*args, **kwargs):
        result = Retriever(*args, **kwargs)
        result.retrieve_candidates = Mock(return_value=[document()])
        return result
    monkeypatch.setattr(embedder, "BgeM3Embedder", Mock())
    local = Mock(side_effect=AssertionError("no local model"))
    monkeypatch.setattr(reranker, "BgeReranker", local)
    monkeypatch.setattr(remote_reranker, "RemoteReranker", create)
    monkeypatch.setattr(retriever, "Retriever", create_retriever)
    configured = settings().model_copy(update={"rerank_provider": "local" if explicit else "remote",
                                               "remote_refuse_threshold": 0.7})
    args = ["--rerank-text-mode", "child"] + (["--rerank-provider", "remote"] if explicit else [])
    assert check_retrieval.main(args, settings=configured) == 0
    output = capsys.readouterr().out
    assert calls and "provider=remote" in output and "REFUSE_THRESHOLD=0.7" in output
    assert "建议阈值" in output
    local.assert_not_called()
