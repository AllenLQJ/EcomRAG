from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE


def test_health_structure_when_dependencies_are_down(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "mysql": "down", "redis": "down", "milvus": "down", "llm": "unconfigured",
    }


@pytest.mark.parametrize("debug", [False, True])
def test_chat_mock_response(client, debug):
    response = client.post("/api/v1/chat", json={
        "session_id": "test-session", "message": "如何退货？", "debug": debug,
    })
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "request_id", "session_id", "answer", "route", "strategy", "sources",
        "is_mock", "trace",
    }
    assert str(UUID(body["request_id"])) == body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert body["session_id"] == "test-session"
    assert body["answer"]
    assert body["route"] == "general"
    assert body["strategy"] is None
    assert body["sources"] == []
    assert body["is_mock"] is True
    if debug:
        assert body["trace"]
        assert body["trace"][0]["stage"] == "mock"
        assert body["trace"][0]["ms"] >= 0
    else:
        assert body["trace"] is None


@pytest.mark.parametrize("message", ["", " \t\n", "问" * 501])
def test_invalid_message_uses_uniform_error(client, message):
    response = client.post("/api/v1/chat", json={
        "session_id": "test-session", "message": message,
    })
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == 40001
    assert body["message"]
    assert str(UUID(body["request_id"])) == body["request_id"]
    assert body["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("message", ["问", "问" * 500])
def test_valid_message_boundaries(client, message):
    response = client.post("/api/v1/chat", json={
        "session_id": "test-session", "message": message,
    })
    assert response.status_code == 200


@pytest.mark.parametrize("session_id", ["", " \t\n"])
def test_blank_session_id(client, session_id):
    response = client.post("/api/v1/chat", json={
        "session_id": session_id, "message": "如何退货？",
    })
    assert response.status_code == 400
    assert response.json() == {
        "code": 40001, "message": "请求参数非法",
        "request_id": response.headers["X-Request-ID"],
    }


def test_app_error_handler(app, client):
    @app.get("/test-app-error")
    async def fail():
        raise AppError(DEPENDENCY_UNAVAILABLE, 503, "依赖不可用")

    response = client.get("/test-app-error")
    assert response.status_code == 503
    assert response.json() == {
        "code": 50301,
        "message": "依赖不可用",
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_disabled_by_default(client, path):
    assert client.get(path).status_code == 404


def test_uncaught_exception_uses_internal_error(app):
    @app.get("/test-unexpected-error")
    async def fail():
        raise RuntimeError("测试内部细节不得出现在响应中")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-unexpected-error")
    assert response.status_code == 500
    assert response.json() == {
        "code": 50001, "message": "内部错误",
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize("method,path,status,code", [
    ("GET", "/missing", 404, 40401),
    ("GET", "/api/v1/chat", 405, 40501),
])
def test_http_error_body(client, method, path, status, code):
    response = client.request(method, path)
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == code
    assert body["message"]
    assert str(UUID(body["request_id"])) == body["request_id"]
    assert body["request_id"] == response.headers["X-Request-ID"]
    if status == 405:
        assert "POST" in response.headers["Allow"]


def test_http_400_is_not_a_validation_error(app, client):
    @app.get("/test-http-error")
    async def fail():
        raise HTTPException(status_code=400, detail="普通 HTTP 错误")

    response = client.get("/test-http-error")
    assert response.status_code == 400
    assert response.json()["code"] == 40000


def test_llm_configured_without_network(app_factory):
    app = app_factory(qwen_api_key="PLACEHOLDER_NOT_A_REAL_KEY")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["llm"] == "configured"


def test_docs_enabled_in_debug(app_factory):
    with TestClient(app_factory(debug=True)) as client:
        docs = client.get("/docs")
        schema = client.get("/openapi.json")
    assert docs.status_code == 200
    assert "text/html" in docs.headers["Content-Type"]
    assert schema.status_code == 200
    assert "/api/v1/chat" in schema.json()["paths"]
