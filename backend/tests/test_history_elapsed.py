import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from account_fakes import HEADERS, login, register, sse_events
from app.account_api import AssistantMessageBody
from app.account_repo import AccountRepo
from app.contracts import AppError
from app.message_limits import MAX_ELAPSED_MS, MAX_MESSAGE_BYTES, bounded_message, json_bytes, message_metadata
from test_auth_storage import ScriptedMySQL


NOW = datetime(2026, 1, 1)
CONVERSATION = dict(id="c", title="title", updated_at=NOW)


@pytest.mark.parametrize("elapsed", [0, 6000, MAX_ELAPSED_MS])
def test_repository_round_trips_elapsed_in_existing_json_column(elapsed):
    db = ScriptedMySQL([CONVERSATION])
    repo = AccountRepo(connection_factory=lambda: db)
    repo.append_message(1, "c", "assistant", "answer", "faq", [], elapsed_ms=elapsed, request_id="r")
    sql, params = db.calls[2]
    assert sql.startswith("INSERT INTO messages")
    assert json.loads(params[4]) == dict(sources=[], elapsed_ms=elapsed, request_id="r")
    row = dict(role="assistant", content="answer", route="faq", sources=params[4], created_at=NOW)
    read = ScriptedMySQL([CONVERSATION, [row]])
    detail = AccountRepo(connection_factory=lambda: read).conversation(1, "c")
    assert detail["messages"][0]["elapsed_ms"] == elapsed
    assert detail["messages"][0]["sources"] == []
    assert "request_id" not in detail["messages"][0]


@pytest.mark.parametrize("sources", [None, "null", "[]", [], '{"sources":[],"request_id":"old"}'])
def test_repository_reads_old_history_without_inventing_elapsed(sources):
    row = dict(role="assistant", content="old", route="faq", sources=sources, created_at=NOW)
    db = ScriptedMySQL([CONVERSATION, [row]])
    message = AccountRepo(connection_factory=lambda: db).conversation(1, "c")["messages"][0]
    assert "elapsed_ms" not in message
    assert message["sources"] in (None, [])


@pytest.mark.parametrize("elapsed", [-1, MAX_ELAPSED_MS + 1, 1.5, "6000", True, float("inf")])
def test_elapsed_rejects_invalid_values_at_model_and_repository_boundaries(elapsed):
    with pytest.raises(ValidationError):
        AssistantMessageBody(request_id="r", content="answer", elapsed_ms=elapsed)
    db = ScriptedMySQL()
    with pytest.raises(AppError) as error:
        AccountRepo(connection_factory=lambda: db).append_message(1, "c", "assistant", "answer", elapsed_ms=elapsed)
    assert error.value.code == 40001 and not db.calls


def test_elapsed_metadata_counts_toward_serialized_message_budget(monkeypatch):
    metadata = message_metadata([], 6000, "r")
    size = len(json_bytes(dict(role="assistant", content="answer", route=None, sources=metadata)))
    monkeypatch.setattr("app.message_limits.MAX_MESSAGE_BYTES", size)
    bounded_message("answer", sources=[], elapsed_ms=6000, request_id="r")
    monkeypatch.setattr("app.message_limits.MAX_MESSAGE_BYTES", size - 1)
    with pytest.raises(AppError):
        bounded_message("answer", sources=[], elapsed_ms=6000, request_id="r")
    assert size < MAX_MESSAGE_BYTES


@pytest.mark.parametrize("client_first", [False, True])
def test_repository_merges_auto_save_and_client_timing_under_same_lock(client_first):
    stored = dict(id=7, content="original", route="faq", sources=json.dumps(
        message_metadata([], 6000 if client_first else None, "r")))
    db = ScriptedMySQL([CONVERSATION, stored])
    AccountRepo(connection_factory=lambda: db).append_message(
        1, "c", "assistant", "replacement", "rag", [], elapsed_ms=None if client_first else 6000, request_id="r")
    assert "FOR UPDATE" in db.calls[0][0]
    assert "JSON_EXTRACT" in db.calls[1][0] and db.calls[1][1] == ("c", "r")
    assert not any(sql.startswith("INSERT") for sql, _ in db.calls)
    if client_first:
        assert len(db.calls) == 2  # 自动保存不能抹掉先到的真实耗时。
    else:
        sql, params = db.calls[2]
        assert sql.startswith("UPDATE messages SET sources=")
        assert json.loads(params[0])["elapsed_ms"] == 6000 and params[1:] == (7, "c")
    assert db.committed


def test_timing_update_rechecks_actual_stored_message_size(monkeypatch):
    stored = dict(id=7, content="old oversized content", route="faq",
                  sources=json.dumps(message_metadata([], request_id="r")))
    # A legacy/direct SQL row can be larger than the currently permitted budget.
    limit = len(json_bytes(dict(role="assistant", content="", route=None,
                               sources=message_metadata([], 6000, "r"))))
    monkeypatch.setattr("app.message_limits.MAX_MESSAGE_BYTES", limit)
    db = ScriptedMySQL([CONVERSATION, stored])
    with pytest.raises(AppError) as error:
        AccountRepo(connection_factory=lambda: db).append_message(
            1, "c", "assistant", "", sources=[], elapsed_ms=6000, request_id="r")
    assert error.value.code == 40001 and db.rolled_back
    assert not any(sql.startswith("UPDATE") for sql, _ in db.calls)


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_completed_answer_timing_survives_logout_and_login(account_factory, path):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        response = api.post(path, json=dict(session_id="s", message="你好", conversation_id=cid))
        answer = sse_events(response)[-1][1] if path.endswith("stream") else response.json()
        messages_path = f"/api/v1/conversations/{cid}/messages"
        body = dict(request_id=answer["request_id"], content=answer["answer"], route=answer["route"],
                    sources=answer["sources"], elapsed_ms=6000, ignored_extra="legacy-extra-policy")
        for _ in range(2):
            assert api.post(messages_path, headers=HEADERS, json=body).status_code == 200
        api.post("/api/v1/auth/logout", headers=HEADERS)
        assert login(api).status_code == 200
        detail = api.get(f"/api/v1/conversations/{cid}").json()
        assert len(detail["messages"]) == 2
        assert detail["messages"][-1]["elapsed_ms"] == 6000
        assert detail["messages"][-1]["content"] == answer["answer"]
        assert "elapsed_ms" not in detail["messages"][0]


def test_client_timing_before_automatic_save_and_old_history(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = repo.create_conversation(1)["id"]
        repo.append_message(1, cid, "assistant", "old")
        path = f"/api/v1/conversations/{cid}"
        assert "elapsed_ms" not in api.get(path).json()["messages"][0]
        body = dict(request_id="r", content="answer", elapsed_ms=6000)
        assert api.post(path + "/messages", headers=HEADERS, json=body).status_code == 200
        repo.append_message(1, cid, "assistant", "answer", request_id="r")
        messages = api.get(path).json()["messages"]
        assert len(messages) == 2 and messages[-1]["elapsed_ms"] == 6000


@pytest.mark.parametrize("elapsed", [-1, MAX_ELAPSED_MS + 1, True, "6000", 1.5])
def test_elapsed_http_validation_does_not_write(account_factory, elapsed):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = repo.create_conversation(1)["id"]
        result = api.post(f"/api/v1/conversations/{cid}/messages", headers=HEADERS,
                          json=dict(request_id="r", content="PRIVATE_ANSWER", elapsed_ms=elapsed))
        assert result.status_code == 400 and result.json()["code"] == 40001
        assert not repo.messages[cid] and "PRIVATE_ANSWER" not in result.text


def test_message_write_requires_login_ownership_and_csrf_header(account_factory):
    app, repo, _, _ = account_factory()
    body = dict(request_id="r", content="answer", elapsed_ms=6000)
    with TestClient(app) as api:
        assert api.post("/api/v1/conversations/missing/messages", headers=HEADERS, json=body).status_code == 401
        register(api)
        cid = repo.create_conversation(1)["id"]
        path = f"/api/v1/conversations/{cid}/messages"
        assert api.post(path, json=body).status_code == 403
        register(api, "Other_01")
        for conversation_id in (cid, "missing"):
            result = api.post(f"/api/v1/conversations/{conversation_id}/messages", headers=HEADERS, json=body)
            assert result.status_code == 404 and result.json()["code"] == 40401
        assert not repo.messages[cid]


def test_timed_message_endpoint_keeps_existing_count_and_payload_limits(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = repo.create_conversation(1)["id"]
        path = f"/api/v1/conversations/{cid}/messages"
        for index in range(200):
            repo.append_message(1, cid, "assistant", str(index))
        for invalid in (dict(content="字" * 4001), dict(request_id=" " * 2),
                        dict(sources=[dict(index=1, doc_id="d", title="t", snippet="s", score=1)] * 9)):
            body = dict(request_id="r", content="answer", elapsed_ms=6000) | invalid
            assert api.post(path, headers=HEADERS, json=body).status_code == 400
            assert len(repo.messages[cid]) == 200
        assert api.post(path, headers=HEADERS, json=dict(request_id="r", content="answer", elapsed_ms=0)).status_code == 200
        messages = api.get(f"/api/v1/conversations/{cid}").json()["messages"]
        assert len(messages) == 200 and messages[0]["content"] == "1"
        assert messages[-1]["elapsed_ms"] == 0
