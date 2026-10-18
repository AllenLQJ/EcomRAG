from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, SERVICE_BUSY


def test_health_structure_when_dependencies_are_down(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "mysql": "down", "redis": "down", "milvus": "down", "llm": "unconfigured",
    }


@pytest.mark.parametrize("debug", [False, True])
def test_chat_injected_response(client, debug):
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
    assert body["is_mock"] is False
    assert body["trace"] is None  # ALLOW_TRACE 默认 false


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


@pytest.mark.parametrize("code,message", [(DEPENDENCY_UNAVAILABLE, "依赖不可用"),
                                         (SERVICE_BUSY, "服务有点忙，请稍后再试")])
def test_app_error_handler(app, client, code, message):
    @app.get("/test-app-error")
    async def fail():
        raise AppError(code, 503, message)

    response = client.get("/test-app-error")
    assert response.status_code == 503
    assert response.json() == {
        "code": code,
        "message": message,
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize("stage", ["model", "rate_limit"])
def test_busy_chat_preserves_code_message_and_request_id(app, client, stage, caplog):
    error = AppError(SERVICE_BUSY, 503, "服务有点忙，请稍后再试")
    if stage == "model":
        app.state.services.faq_lookup.side_effect = error
    else:
        async def busy(*args):
            raise error
        app.state.rate_limiter.check_async = busy
    response = client.post("/api/v1/chat", json={
        "session_id": "busy", "message": "PRIVATE_QUESTION PRIVATE_ANSWER PRIVATE_KEY"})
    assert response.status_code == 503
    assert response.json() == {"code": 50302, "message": "服务有点忙，请稍后再试",
                               "request_id": response.headers["X-Request-ID"]}
    app.state.services.route.assert_not_awaited()
    assert "PRIVATE_" not in response.text + caplog.text


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
    assert response.json()["message"] == "请求无法处理"


@pytest.mark.parametrize("status,message", [(404, "请求的地址不存在"), (405, "请求方法不允许"),
                                           (403, "请求无法处理"), (503, "服务暂时不可用")])
def test_http_exception_detail_never_echoed(app, status, message):
    @app.get("/private-http-detail")
    async def fail():
        raise HTTPException(status_code=status, detail="PRIVATE_QUESTION_KEY")
    with TestClient(app) as client:
        result = client.get("/private-http-detail")
    assert result.status_code == status and result.json()["message"] == message
    assert "PRIVATE_QUESTION_KEY" not in result.text


def test_session_id_maximum_length(client):
    for size, status in ((128, 200), (129, 400)):
        result = client.post("/api/v1/chat", json={"session_id": "s" * size, "message": "你好"})
        assert result.status_code == status
        if status == 400:
            assert result.json()["code"] == 40001


def test_llm_configured_without_network(app_factory):
    app = app_factory(llm_api_key="PLACEHOLDER_NOT_A_REAL_KEY")
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
