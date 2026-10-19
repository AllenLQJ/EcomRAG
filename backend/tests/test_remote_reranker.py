import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.contracts import AppError, SERVICE_BUSY
from app.retrieval.remote_reranker import RemoteReranker, map_results, _semaphore
from deadline_helpers import ControlledDeadlines

KEY = "REMOTE_PRIVATE_KEY_101"
QUERY = "REMOTE_PRIVATE_QUERY_102"
TEXT = "REMOTE_PRIVATE_PASSAGE_103"


def settings(**overrides):
    values = dict(llm_api_key=KEY, llm_base_url="https://vendor.example/api/paas/v4/",
                  rerank_remote_retry_backoff_s=0)
    values.update(overrides)
    return Settings(_env_file=None, **values)


def response_for(request):
    body = json.loads(request.content)
    return httpx.Response(200, json={"results": [
        {"index": i, "document": text, "relevance_score": float(i)}
        for i, text in enumerate(body["documents"])]})


def test_payload_truncation_batches_and_core_fields_take_precedence(caplog):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == "https://vendor.example/api/paas/v4/rerank"
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request.headers["Content-Type"] == "application/json"
        assert body["model"] == "rerank" and body["query"] == QUERY[:4096]
        assert body["return_documents"] is True and body["top_n"] == 0
        assert body["return_raw_scores"] is True
        assert len(body["request_id"]) == 32
        assert all(len(text) <= 4096 for text in body["documents"])
        return response_for(request)
    async def run():
        client = RemoteReranker(settings(rerank_remote_extra_body={
            "model": "wrong", "query": "wrong", "documents": [], "return_documents": False,
            "top_n": 1, "request_id": "wrong", "return_raw_scores": True, "private": "PRIVATE_EXTRA_VALUE"}),
            transport=httpx.MockTransport(handler))
        try:
            texts = [TEXT + str(i) + "文" * 4200 for i in range(257)]
            scores = await client.rerank(QUERY, texts)
            assert scores == list(map(float, range(128))) * 2 + [0.0]
            assert [len(call["documents"]) for call in calls] == [128, 128, 1]
            assert len({call["request_id"] for call in calls}) == 3
        finally:
            await client.aclose()
    caplog.set_level(logging.INFO)
    asyncio.run(run())
    assert all(private not in caplog.text for private in (KEY, QUERY, TEXT, "PRIVATE_EXTRA_VALUE"))


def test_query_is_truncated_before_transmission():
    async def run():
        def handler(request):
            assert json.loads(request.content)["query"] == "问" * 4096
            return response_for(request)
        client = RemoteReranker(settings(), transport=httpx.MockTransport(handler))
        try:
            assert await client.rerank("问" * 5000, [TEXT]) == [0.0]
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["zero", "one", "shuffled", "swapped_index", "no_index", "no_document", "object_document"])
def test_maps_results_to_input_order_and_keeps_unbounded_scores(mode):
    documents = ["a", "b", "c"]
    results = [{"index": i, "document": text, "relevance_score": score}
               for i, (text, score) in enumerate(zip(documents, [-3.5, 9.2, 0.0]))]
    if mode == "one":
        for item in results:
            item["index"] += 1
    if mode == "shuffled":
        results.reverse()
    if mode == "swapped_index":
        results[0]["index"], results[1]["index"] = 1, 0
    if mode == "no_index":
        for item in results:
            item.pop("index")
    if mode == "no_document":
        for item in results:
            item.pop("document")
    if mode == "object_document":
        for item in results:
            item["document"] = {"text": item["document"]}
    expected = [9.2, -3.5, 0.0] if mode == "swapped_index" else [-3.5, 9.2, 0.0]
    assert map_results(results, documents) == expected


@pytest.mark.parametrize("echo", ["normalized", "truncated"])
def test_valid_indices_accept_modified_document_echo_and_log_only_count(echo, caplog):
    documents = [TEXT + "  first\n passage", TEXT + "\tsecond  passage"]
    def handler(request):
        results = [{"index": i, "document": {"text": " ".join(text.split()) if echo == "normalized" else text[:10]},
                    "relevance_score": score}
                   for i, text, score in [(1, documents[1], 0.9), (0, documents[0], -0.2)]]
        return httpx.Response(200, json={"results": results})
    async def run():
        client = RemoteReranker(settings(), transport=httpx.MockTransport(handler))
        try:
            assert await client.rerank(QUERY, documents) == [-0.2, 0.9]
        finally:
            await client.aclose()
    asyncio.run(run())
    records = [record for record in caplog.records if record.name == "app.retrieval.remote_reranker"]
    assert [record.getMessage() for record in records] == ["rerank_document_mismatch count=2"]
    assert all(private not in caplog.text for private in (KEY, QUERY, TEXT, *documents))


@pytest.mark.parametrize("indices", [[2, 0], [0, 0], [None, 0], [True, 0]])
@pytest.mark.parametrize("matching_text", [True, False])
def test_invalid_indices_fall_back_to_text_or_raise_bad_response(indices, matching_text):
    results = [{"document": text, "relevance_score": score}
               for text, score in [(TEXT + " second" if matching_text else "unmatched", 0.9), (TEXT, 0.2)]]
    for result, index in zip(results, indices):
        if index is not None:
            result["index"] = index
    async def run():
        client = RemoteReranker(settings(), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": results})))
        try:
            if matching_text:
                assert await client.rerank(QUERY, [TEXT, TEXT + " second"]) == [0.2, 0.9]
            else:
                with pytest.raises(AppError) as caught:
                    await client.rerank(QUERY, [TEXT, TEXT + " second"])
                assert caught.value.diagnostic["category"] == "bad_response"
                assert (caught.value.code, caught.value.http_status) == (50201, 502)
        finally:
            await client.aclose()
    asyncio.run(run())


def test_duplicate_texts_consumed_once_in_response_order():
    results = [{"index": 99, "document": text, "relevance_score": score}
               for text, score in [("same", 0.8), ("other", 0.3), ("same", 0.2)]]
    assert map_results(results, ["same", "same", "other"]) == [0.8, 0.2, 0.3]


@pytest.mark.parametrize("results", [None, [], [{"index": 0, "relevance_score": 1}],
    [{"index": 1, "relevance_score": 1}, {"index": 2, "relevance_score": 2}],
    [{"index": 0, "document": "wrong", "relevance_score": 1}, {"index": 0, "relevance_score": 2}],
    [{"index": 0, "document": "a", "relevance_score": 1}, {"index": 0, "document": "a", "relevance_score": 2}],
    [{"index": 0, "relevance_score": True}, {"index": 1, "relevance_score": 2}],
    [{"index": 0, "relevance_score": "0.5"}, {"index": 1, "relevance_score": 2}],
    [{"index": 0, "relevance_score": float("inf")}, {"index": 1, "relevance_score": 2}],
    [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": 2}],
    [{"index": 0, "document": 123, "relevance_score": 1}, {"index": 1, "relevance_score": 2}],
])
def test_ambiguous_missing_or_invalid_results_rejected(results):
    with pytest.raises(ValueError):
        map_results(results, ["a", "b"])


@pytest.mark.parametrize("mode", ["429", "500", "503", "connect", "reset", "refused"])
def test_retries_only_transient_failures_then_succeeds(mode):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if mode == "connect":
                raise httpx.ConnectError(KEY + QUERY + TEXT)
            if mode == "reset":
                raise ConnectionResetError(KEY + QUERY + TEXT)
            if mode == "refused":
                raise ConnectionRefusedError(KEY + QUERY + TEXT)
            return httpx.Response(int(mode), json={"error": {"code": "retry", "message": KEY + QUERY + TEXT}})
        return response_for(request)
    sleep = AsyncMock()
    async def run():
        client = RemoteReranker(settings(rerank_remote_retry_backoff_s=0.5),
                                transport=httpx.MockTransport(handler), sleep=sleep)
        try:
            assert await client.rerank(QUERY, [TEXT]) == [0.0]
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.parametrize("mode,category,code,status,calls_expected", [
    ("429", "http_status", SERVICE_BUSY, 503, 3), ("503", "http_status", 50201, 502, 3),
    ("connect", "network", 50201, 502, 3), ("400", "http_status", 50201, 502, 1),
    ("401", "http_status", 50201, 502, 1), ("404", "http_status", 50201, 502, 1),
    ("302", "http_status", 50201, 502, 1), ("timeout", "timeout", 50401, 504, 1),
    ("wall_timeout", "timeout", 50401, 504, 1), ("bad_json", "bad_response", 50201, 502, 1),
    ("missing", "bad_response", 50201, 502, 1), ("write", "network", 50201, 502, 1),
])
def test_exhaustion_nonretryable_errors_and_safe_diagnostics(mode, category, code, status, calls_expected, caplog):
    calls = []
    async def handler(request):
        calls.append(request)
        if mode == "connect":
            raise httpx.ConnectError(KEY + QUERY + TEXT)
        if mode == "timeout":
            raise httpx.ReadTimeout(KEY + QUERY + TEXT)
        if mode == "wall_timeout":
            await asyncio.Event().wait()
        if mode == "write":
            raise httpx.WriteError(KEY + QUERY + TEXT)
        if mode == "bad_json":
            return httpx.Response(200, text=KEY + QUERY + TEXT)
        if mode == "missing":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(int(mode), json={"error": {
            "code": "VendorCode", "message": KEY + QUERY + TEXT + "长" * 300}})
    async def run():
        client = RemoteReranker(settings(rerank_remote_timeout_s=0.01, rerank_remote_max_retries=2),
                                transport=httpx.MockTransport(handler), sleep=AsyncMock())
        try:
            with pytest.raises(AppError) as caught:
                await client.rerank(QUERY, [TEXT])
            error = caught.value
            assert (error.code, error.http_status, error.diagnostic["category"]) == (code, status, category)
            assert error.diagnostic["host"] == "vendor.example"
            assert error.diagnostic["model"] == "rerank"
            if mode.isdigit():
                assert error.diagnostic["error_code"] == "VendorCode"
                assert len(error.diagnostic["error_message"]) == 200
            assert all(private not in str(error) + str(error.diagnostic) for private in (KEY, QUERY, TEXT))
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == calls_expected
    assert all(private not in caplog.text for private in (KEY, QUERY, TEXT))


def test_shared_concurrency_across_instances_and_cancellation_releases_slot():
    async def run():
        active = peak = 0
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            entered.set()
            try:
                await release.wait()
                return response_for(request)
            finally:
                active -= 1
        clients = [RemoteReranker(settings(rerank_remote_max_concurrency=1), transport=httpx.MockTransport(handler))
                   for _ in range(2)]
        tasks = []
        try:
            first = asyncio.create_task(clients[0].rerank(QUERY, [TEXT]))
            tasks.append(first)
            await entered.wait()
            second = asyncio.create_task(clients[1].rerank(QUERY, [TEXT]))
            tasks.append(second)
            await asyncio.sleep(0)
            assert active == peak == 1 and not second.done()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert await second == [0.0]
            assert await clients[0].rerank(QUERY, [TEXT]) == [0.0]
            assert peak == 1
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for client in clients:
                await client.aclose()
    asyncio.run(asyncio.wait_for(run(), 2))


def test_queued_request_obeys_shared_deadline_and_does_not_leak_permit(monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        limits = _semaphore(1)
        deadlines = ControlledDeadlines(monkeypatch, limits)
        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return response_for(request)
        client = RemoteReranker(settings(rerank_remote_max_concurrency=1, request_timeout_s=60),
                                transport=httpx.MockTransport(handler))
        tasks = []
        try:
            holder = asyncio.create_task(client.rerank(QUERY, [TEXT]))
            tasks.append(holder)
            await entered.wait()
            queued = asyncio.create_task(client.rerank(QUERY, [TEXT]))
            tasks.append(queued)
            assert await deadlines.waiters.get() is queued
            deadlines.expire(queued)
            with pytest.raises(AppError) as caught:
                await queued
            assert caught.value.code == 50401 and len(calls) == 1
            release.set()
            assert await holder == [0.0]
            assert await client.rerank(QUERY, [TEXT]) == [0.0]
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
    asyncio.run(asyncio.wait_for(run(), 2))


def test_unconfigured_and_empty_inputs_do_not_call_transport():
    async def run():
        client = RemoteReranker(settings(llm_api_key=""),
                                transport=httpx.MockTransport(lambda r: pytest.fail("network forbidden")))
        try:
            assert await client.rerank(QUERY, []) == []
            with pytest.raises(AppError) as caught:
                await client.rerank(QUERY, [TEXT])
            assert caught.value.diagnostic["category"] == "unconfigured"
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("rerank_provider", "invalid"), ("rerank_fallback", "invalid"), ("rerank_remote_base_url", "ftp://bad"),
    ("rerank_remote_base_url", "https://user:password@example.com"), ("rerank_remote_base_url", "https://[bad"),
    ("rerank_remote_model", " "), ("rerank_remote_timeout_s", 0), ("rerank_remote_timeout_s", float("inf")),
    ("rerank_remote_max_retries", -1), ("rerank_remote_retry_backoff_s", -1),
    ("rerank_remote_retry_backoff_s", float("nan")), ("rerank_remote_max_concurrency", 0),
    ("rerank_remote_extra_body", "[]"), ("rerank_remote_extra_body", "{bad"),
    ("rerank_remote_extra_body", '{"a":NaN}'), ("remote_refuse_threshold", float("nan")),
    ("remote_rag_context_min_score", float("nan")), ("remote_rag_context_min_score", float("inf")),
    ("remote_rag_context_min_score", "invalid"),
])
def test_invalid_remote_configuration(field, value):
    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_remote_config_defaults_overrides_and_thresholds(monkeypatch):
    default = Settings(_env_file=None)
    assert (default.rerank_provider, default.rerank_fallback) == ("local", "rrf")
    assert default.rerank_remote_model == "rerank"
    assert default.rerank_remote_timeout_s == 10 and default.rerank_remote_max_retries == 1
    assert default.rerank_remote_retry_backoff_s == 0.5 and default.rerank_remote_max_concurrency == 50
    assert default.rerank_remote_extra_body == {} and default.remote_refuse_threshold is None
    assert default.remote_rag_context_min_score is None
    configured = settings(rerank_provider="remote", remote_refuse_threshold="", refuse_threshold=0.3)
    assert configured.remote_rerank_key == KEY
    assert configured.remote_rerank_base_url == configured.llm_base_url
    assert configured.rerank_refuse_threshold == 0.3
    configured = settings(rerank_provider="remote", remote_refuse_threshold=0.7,
                          rerank_remote_api_key="override", rerank_remote_base_url="https://override.example")
    assert configured.remote_rerank_key == "override" and configured.remote_rerank_base_url == "https://override.example"
    assert configured.rerank_refuse_threshold == 0.7
    with pytest.raises(ValidationError):
        settings(rerank_provider="remote", remote_refuse_threshold=3)
    monkeypatch.setenv("RERANK_REMOTE_EXTRA_BODY", '{"return_raw_scores":true}')
    monkeypatch.setenv("REMOTE_REFUSE_THRESHOLD", "0.5")
    assert settings(rerank_provider="remote").rerank_refuse_threshold == 0.5
    assert settings().rerank_remote_extra_body == {"return_raw_scores": True}


@pytest.mark.parametrize("value", [-2, -0.001, 1.001, 1.5, 3, float("inf"), float("-inf"), float("nan")])
def test_remote_refuse_threshold_rejects_out_of_range_values(monkeypatch, value):
    with pytest.raises(ValidationError):
        settings(rerank_provider="remote", remote_refuse_threshold=value)
    monkeypatch.setenv("REMOTE_REFUSE_THRESHOLD", str(value))
    with pytest.raises(ValidationError):
        settings(rerank_provider="remote")


@pytest.mark.parametrize("value,expected", [(None, 0.3), ("", 0.3), ("  ", 0.3), (0, 0.0), (1, 1.0)])
def test_remote_refuse_threshold_accepts_boundaries_and_empty_fallback(monkeypatch, value, expected):
    assert settings(rerank_provider="remote", remote_refuse_threshold=value,
                    refuse_threshold=0.3).rerank_refuse_threshold == expected
    if value is not None:
        monkeypatch.setenv("REMOTE_REFUSE_THRESHOLD", str(value))
        assert settings(rerank_provider="remote", refuse_threshold=0.3).rerank_refuse_threshold == expected


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("  ", None),
                                           (-2.0, -2.0), ("1.5", 1.5), (0, 0.0)])
def test_remote_context_threshold_accepts_empty_and_unbounded_finite_values(monkeypatch, value, expected):
    assert settings(remote_rag_context_min_score=value).remote_rag_context_min_score == expected
    if value is not None:
        monkeypatch.setenv("REMOTE_RAG_CONTEXT_MIN_SCORE", str(value))
        assert settings().remote_rag_context_min_score == expected


def test_retry_backoff_shares_deadline_and_releases_concurrency(monkeypatch):
    async def run():
        entered = asyncio.Event()
        calls = []
        deadlines = ControlledDeadlines(monkeypatch, _semaphore(1))
        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()
        def handler(request):
            calls.append(request)
            return httpx.Response(503) if len(calls) == 1 else response_for(request)
        client = RemoteReranker(settings(rerank_remote_max_concurrency=1, request_timeout_s=60,
                                         rerank_remote_retry_backoff_s=0.5),
                                transport=httpx.MockTransport(handler), sleep=sleep)
        task = asyncio.create_task(client.rerank(QUERY, [TEXT]))
        try:
            await entered.wait()
            deadlines.expire(task)
            with pytest.raises(AppError) as caught:
                await task
            assert caught.value.code == 50401 and len(calls) == 1
            assert await client.rerank(QUERY, [TEXT]) == [0.0]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
    asyncio.run(asyncio.wait_for(run(), 2))
