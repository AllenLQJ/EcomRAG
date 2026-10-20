import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from account_fakes import HEADERS, login, register, sse_events
from app.config import Settings
from app.schemas import ChatRequest
from app.streaming import SSEStreamingResponse, chat_events
from chat_fakes import FakeServices


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/conversations", None), ("POST", "/conversations", {}),
    ("GET", "/conversations/unknown", None), ("PATCH", "/conversations/unknown", {"title": "new"}),
    ("DELETE", "/conversations/unknown", None),
])
def test_history_requires_login(account_factory, method, path, body):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        result = api.request(method, "/api/v1" + path, headers=HEADERS, json=body)
        assert result.status_code == 401 and result.json()["code"] == 40101


def test_history_crud_order_and_title_validation(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        assert api.get("/api/v1/conversations").json() == {"items": []}
        one = api.post("/api/v1/conversations", headers=HEADERS, json={})
        assert one.status_code == 201
        first = one.json()
        assert first["title"] == "" and first["message_count"] == 0 and len(first["id"]) == 32
        second = api.post("/api/v1/conversations", headers=HEADERS, json={"title": "二"}).json()
        assert [item["id"] for item in api.get("/api/v1/conversations").json()["items"]] == [second["id"], first["id"]]
        path = "/api/v1/conversations/" + first["id"]
        assert api.get(path).json() == dict(id=first["id"], title="", messages=[])
        renamed = api.patch(path, headers=HEADERS, json={"title": "重命名"})
        assert renamed.status_code == 200 and renamed.json()["title"] == "重命名"
        assert api.get("/api/v1/conversations").json()["items"][0]["id"] == first["id"]
        assert api.patch(path, headers=HEADERS, json={"title": "字" * 101}).status_code == 400
        assert api.post("/api/v1/conversations", headers=HEADERS, json={"title": "字" * 101}).status_code == 400
        assert api.delete(path, headers=HEADERS).json() == {}
        assert api.get(path).status_code == 404 and first["id"] not in repo.conversations


@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE", "chat", "stream"])
def test_foreign_and_missing_ids_are_indistinguishable(account_factory, method):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        foreign = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        register(api, "Other_01")
        for cid in (foreign, "nonexistent", "' OR 1=1 --"):
            if method in {"chat", "stream"}:
                path = "/api/v1/chat/stream" if method == "stream" else "/api/v1/chat"
                result = api.post(path, json={"session_id": "s", "message": "你好", "conversation_id": cid})
            else:
                result = api.request(method, "/api/v1/conversations/" + cid, headers=HEADERS, json={"title": "盗取"})
            assert result.status_code == 404 and result.json()["code"] == 40401


def test_conversation_count_limit(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        for _ in range(100):
            repo.create_conversation(1)
        result = api.post("/api/v1/conversations", headers=HEADERS, json={})
        assert result.status_code == 400 and result.json()["code"] == 40001
        assert result.json()["message"] == "对话数量已达上限，请先删除不用的对话"


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_chat_persists_title_sources_and_private_text_only_in_history(account_factory, path, caplog):
    from app.contracts import FaqHit
    caplog.set_level(logging.INFO)
    app, repo, _, _ = account_factory(chat_log_enabled=True)
    question = "PRIVATE_QUESTION隐私问题" * 2
    app.state.services.faq_lookup.return_value = FaqHit(
        faq_id=1, question=question, answer="PRIVATE_ANSWER", score=1)
    with TestClient(app) as api:
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        result = api.post(path, json={"session_id": api.cookies["ecomrag_session"], "message": question,
                                      "conversation_id": cid, "debug": True})
        assert result.status_code == 200
        if path.endswith("stream"):
            assert sse_events(result)[-1][0] == "done"
        detail = api.get("/api/v1/conversations/" + cid).json()
        assert detail["title"] == question[:20]
        assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
        user, assistant = detail["messages"]
        assert user["content"] == question and assistant["content"] == "PRIVATE_ANSWER"
        assert assistant["route"] == "faq"
        assert set(assistant["sources"][0]) == {"index", "doc_id", "title", "snippet", "score"}
        assert api.get("/api/v1/conversations").json()["items"][0]["message_count"] == 2
        app.state.chat_logs.write.assert_not_called()
        assert len(repo.messages[cid]) == 2
    assert question not in caplog.text and "PRIVATE_ANSWER" not in caplog.text


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_guest_ignores_conversation_id_and_save_failure_is_nonfatal(account_factory, path, caplog):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        result = api.post(path, json={"session_id": "s", "message": "你好", "conversation_id": "not-mine"})
        assert result.status_code == 200
        if path.endswith("stream"):
            assert sse_events(result)[-1][0] == "done"
        assert not repo.messages
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        repo.save_fails = True
        result = api.post(path, json={"session_id": "s", "message": "你好", "conversation_id": cid})
        assert result.status_code == 200
        if path.endswith("stream"):
            assert sse_events(result)[-1][0] == "done"
        assert not repo.messages[cid]
    assert "服务器端历史保存失败" in caplog.text and "PRIVATE_" not in caplog.text


def test_stream_disconnect_while_sending_done_does_not_save_assistant():
    async def run():
        finished = AsyncMock()
        stream = chat_events(ChatRequest(session_id="s", message="你好"), "r",
                             settings=Settings(_env_file=None, speculative_retrieval=False, chat_log_enabled=False),
                             services=FakeServices(), before_chat=AsyncMock(), after_chat=finished,
                             chat_logs=SimpleNamespace(write=AsyncMock()))
        response = SSEStreamingResponse(stream)
        disconnected = asyncio.Event()
        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if b"event: done" in message.get("body", b""):
                disconnected.set()
                await asyncio.Event().wait()
        await asyncio.wait_for(response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}},
                                        receive, send), 2)
        finished.assert_not_awaited()
    asyncio.run(run())


def test_stream_cancel_during_generation_leaves_user_only(account_factory):
    async def run():
        app, repo, _, _ = account_factory()
        user, _ = await app.state.accounts.register("Alice_01", "a-strong-password", "ip")
        cid = repo.create_conversation(user["id"])["id"]
        stopped = asyncio.Event()
        async def source(*args):
            try:
                yield "部分答案"
                await asyncio.Event().wait()
            finally:
                stopped.set()
        app.state.services.chat_general_stream = source
        async def before():
            await app.state.accounts.save(user["id"], cid, "user", "问题")
        async def after(result):
            await app.state.accounts.save(user["id"], cid, "assistant", result.answer)
        stream = chat_events(ChatRequest(session_id="s", message="问题"), "r",
                             settings=app.state.accounts.settings, services=app.state.services,
                             before_chat=before, after_chat=after, chat_logs=app.state.chat_logs)
        try:
            async with asyncio.timeout(2):
                while "event: delta" not in await anext(stream):
                    pass
        finally:
            await stream.aclose()
            await app.state.accounts.aclose()
            await app.state.quota.aclose()
            await app.state.rate_limiter.aclose()
        assert stopped.is_set()
        assert [item["role"] for item in repo.messages[cid]] == ["user"]
    asyncio.run(run())


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_failed_chat_never_saves_assistant(account_factory, path):
    from app.contracts import AppError
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        app.state.services.faq_lookup.side_effect = AppError(50201, 502, "模型服务返回异常")
        response = api.post(path, json={"session_id": "s", "message": "问题", "conversation_id": cid})
        assert (sse_events(response)[-1][1] if path.endswith("stream") else response.json())["code"] == 50201
        assert [item["role"] for item in repo.messages[cid]] == ["user"]


def test_message_length_and_recent_200(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        for index in range(201):
            repo.append_message(1, cid, "assistant", str(index))
        result = api.get("/api/v1/conversations/" + cid).json()
        assert len(result["messages"]) == 200 and result["messages"][0]["content"] == "1"
        app.state.services.chat_general.return_value = "字" * 4500
        api.post("/api/v1/chat", json={"session_id": "s", "message": "问题", "conversation_id": cid})
        assert len(repo.messages[cid][-1]["content"]) == 4000
