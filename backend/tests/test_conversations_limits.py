import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi.testclient import TestClient

from account_fakes import HEADERS, register
from app.account_repo import AccountRepo
from app.message_limits import (
    MAX_CONVERSATION_MESSAGES, MAX_MESSAGE_BYTES, MAX_SOURCES, MAX_SOURCES_BYTES,
    bounded_message, json_bytes,
)
from test_auth_storage import ScriptedMySQL


def oversized_sources():
    return [dict(index=index, doc_id="文" * 10000, title="私" * 10000, snippet="\0" * 10000, score=0.5,
                 unexpected="SECRET_EXTRA_FIELD") for index in range(100)]


def test_serialized_message_and_sources_have_hard_utf8_limits():
    for text in ("🙂" * 5000, "\0" * 5000, "\ud800" * 5000):
        content, route, sources = bounded_message(text, "r" * 100, oversized_sources())
        assert len(content) <= 4000 and len(route) <= 16
        assert len(sources) <= MAX_SOURCES and len(json_bytes(sources)) <= MAX_SOURCES_BYTES
        assert len(json_bytes(dict(role="assistant", content=content, route=route, sources=sources))) <= MAX_MESSAGE_BYTES
        for source in sources:
            assert set(source) == {"index", "doc_id", "title", "snippet", "score"}
            assert len(source["doc_id"]) <= 128 and len(source["title"]) <= 200 and len(source["snippet"]) <= 500


def test_sources_are_bounded_before_full_iteration_or_serialization():
    def source():
        for index in range(MAX_SOURCES):
            yield dict(index=index, doc_id="d", title="t", snippet="s", score=0.1)
        raise AssertionError("不应遍历预算之外的 sources")
    assert len(bounded_message("正文", sources=source())[2]) == MAX_SOURCES
    invalid = [dict(index=1, score=float("nan")), dict(index=2, score=float("inf")),
               dict(index=3, score=10 ** 10000), dict(index=True, score=0.1)]
    assert bounded_message("正文", sources=invalid)[2] == []


def test_repo_enforces_limits_even_when_service_layer_is_bypassed():
    db = ScriptedMySQL([dict(id="c", title="", updated_at=datetime(2026, 1, 1))])
    AccountRepo(connection_factory=lambda: db).append_message(1, "c", "assistant", "\0" * 5000,
                                                             "route" * 30, oversized_sources())
    _, params = db.calls[1]
    assert len(params[2]) == 4000 and len(params[3]) == 16
    assert len(params[4].encode("utf-8")) <= MAX_SOURCES_BYTES
    assert len(json.loads(params[4])) <= MAX_SOURCES
    assert "SECRET_EXTRA_FIELD" not in params[4]
    assert all("SECRET_EXTRA_FIELD" not in sql for sql, _ in db.calls)


def test_repo_prunes_in_same_transaction_and_conversation_lock():
    cutoff = datetime(2026, 1, 1)
    db = ScriptedMySQL([dict(id="c", title="t", updated_at=cutoff), None, None,
                        dict(id=42, created_at=cutoff), None])
    AccountRepo(connection_factory=lambda: db).append_message(7, "c", "user", "正文")
    assert "FOR UPDATE" in db.calls[0][0]
    assert db.calls[3][1] == ("c", MAX_CONVERSATION_MESSAGES - 1)
    sql, params = db.calls[-1]
    assert sql.startswith("DELETE FROM messages") and "conversation_id=%s" in sql
    assert params == ("c", cutoff, 42) and db.committed


def test_conversation_storage_stays_bounded_and_preserves_recent_messages(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        for index in range(501):
            repo.append_message(1, cid, "assistant", str(index))
        assert len(repo.messages[cid]) == MAX_CONVERSATION_MESSAGES
        result = api.get("/api/v1/conversations/" + cid).json()
        assert len(result["messages"]) == MAX_CONVERSATION_MESSAGES
        assert result["messages"][0]["content"] == "301" and result["messages"][-1]["content"] == "500"
        assert api.get("/api/v1/conversations").json()["items"][0]["message_count"] == MAX_CONVERSATION_MESSAGES
        other = repo.create_conversation(1)["id"]
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda n: repo.append_message(1, other, "assistant", str(n)), range(500)))
        assert len(repo.messages[other]) == MAX_CONVERSATION_MESSAGES
        assert len(repo.messages[cid]) == MAX_CONVERSATION_MESSAGES


def test_service_applies_size_budget_before_calling_repository(account_factory):
    async def run():
        app, repo, _, _ = account_factory()
        captured = []
        repo.append_message = lambda *args: captured.append(args)
        try:
            await app.state.accounts.save(1, "c", "assistant", "🙂" * 5000, "general", oversized_sources())
            _, _, _, content, route, sources = captured[0]
            assert len(content) == 4000 and len(json_bytes(sources)) <= MAX_SOURCES_BYTES
            assert len(json_bytes(dict(role="assistant", content=content, route=route, sources=sources))) <= MAX_MESSAGE_BYTES
        finally:
            await app.state.accounts.aclose()
            await app.state.quota.aclose()
            await app.state.rate_limiter.aclose()
    asyncio.run(run())
