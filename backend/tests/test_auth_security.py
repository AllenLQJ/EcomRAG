"""账号安全审计回归：仅使用离线账号/MySQL/Redis 替身。"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from account_fakes import HEADERS, login, register, sse_events
from app.auth import AccountService, SESSION_COOKIE, hash_password, verify_password
from app.account_store import digest
from app.config import Settings
from app.contracts import AppError
from app.main import create_app


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/auth/register"), ("POST", "/api/v1/auth/login"),
    ("DELETE", "/api/v1/auth/account"),
])
@pytest.mark.parametrize("password", ["x" * 65, "私" * 4096])
def test_overlong_password_rejected_at_http_entry_before_any_hash(account_factory, method, path, password, caplog):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        if method == "DELETE":
            assert register(api).status_code == 201
        with patch("app.auth.hash_password") as hash_call, patch("app.auth.verify_password") as verify_call:
            result = api.request(method, path, headers=HEADERS,
                                 json={"username": "Alice_01", "password": password})
        assert result.status_code == 400 and result.json()["code"] == 40003
        assert "64" in result.json()["message"]
        hash_call.assert_not_called()
        verify_call.assert_not_called()
        assert password not in result.text + caplog.text


def test_64_character_password_still_works_for_register_login_and_delete(account_factory):
    app, _, _, _ = account_factory()
    password = "aB!" + "x" * 61
    with TestClient(app) as api:
        assert register(api, password=password).status_code == 201
        assert login(api, password=password).status_code == 200
        assert api.request("DELETE", "/api/v1/auth/account", headers=HEADERS,
                           json={"password": password}).status_code == 200


def test_service_and_hash_helpers_cannot_bypass_length_guard(account_factory):
    async def run():
        app, _, _, _ = account_factory()
        service = app.state.accounts
        password = "p" * 65
        user = dict(id=1, username="Alice_01", status=1, password_hash="unused")
        with patch("app.auth.hashlib.scrypt", create=True) as scrypt:
            for invoke in (
                lambda: hash_password(password), lambda: verify_password(password, "unused"),
                lambda: service._password_check(password, user),
                lambda: service._register("Alice_01", password, "ip", None),
                lambda: service._login("Alice_01", password, "ip", None),
            ):
                with pytest.raises(AppError) as caught:
                    invoke()
                assert caught.value.code == 40003
            for function, args in (
                (service.register, ("Alice_01", password, "ip")),
                (service.login, ("Alice_01", password, "ip")),
                (service.delete_account, (user, password, "ip")),
            ):
                with pytest.raises(AppError) as caught:
                    await function(*args)
                assert caught.value.code == 40003
            scrypt.assert_not_called()
        await service.aclose()
        await app.state.quota.aclose()
        await app.state.rate_limiter.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
@pytest.mark.parametrize("cookie_kind", ["missing", "malformed", "shaped", "expired", "valid"])
def test_redis_down_session_cookie_falls_back_to_guest_and_logs(account_factory, path, cookie_kind):
    app, _, redis, clock = account_factory(chat_log_enabled=True)
    with TestClient(app) as api:
        token = None
        if cookie_kind in {"valid", "expired"}:
            assert register(api).status_code == 201
            token = api.cookies[SESSION_COOKIE]
            if cookie_kind == "expired":
                clock[0] += 604801
        elif cookie_kind == "shaped":
            token = "a" * 43
        elif cookie_kind == "malformed":
            token = "not-a-session"
        api.cookies.clear()
        if token:
            api.cookies.set(SESSION_COOKIE, token)
        redis.down = True
        me = api.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["user"] is None
        assert me.json()["quota"]["role"] == "guest"
        result = api.post(path, json={"session_id": "s", "message": "你好", "conversation_id": "ignored"})
        assert result.status_code == 200
        if path.endswith("stream"):
            assert sse_events(result)[-1][0] == "done"
        app.state.chat_logs.write.assert_awaited_once()
        assert api.get("/api/v1/auth/me").json()["quota"]["used"] == 1
        assert api.get("/api/v1/conversations").status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_invalid_cookie_cannot_skip_chat_logs_when_redis_is_healthy(account_factory, path, caplog):
    app, _, _, _ = account_factory(chat_log_enabled=True)
    token = "a" * 43
    with TestClient(app) as api:
        api.cookies.set(SESSION_COOKIE, token)
        result = api.post(path, json={"session_id": "s", "message": "PRIVATE_MESSAGE"})
        assert result.status_code == 200
        if path.endswith("stream"):
            assert sse_events(result)[-1][0] == "done"
        app.state.chat_logs.write.assert_awaited_once()
        assert app.state.chat_logs.write.await_args.args[0].question == "PRIVATE_MESSAGE"
    assert "PRIVATE_MESSAGE" not in caplog.text and token not in caplog.text


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
def test_chat_origin_rejected_before_quota_model_or_history(account_factory, path):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        body = {"session_id": "s", "message": "你好", "conversation_id": cid}
        for origin in ("https://evil.example", "http://sub.localhost:3000", "null"):
            response = api.post(path, headers={"Origin": origin}, json=body)
            assert response.status_code == 403 and response.json()["code"] == 40301
        assert api.get("/api/v1/auth/me").json()["quota"]["used"] == 0
        assert not repo.messages[cid]
        app.state.services.faq_lookup.assert_not_called()
        # 保留无 Origin 客户端；允许列表中的浏览器来源也无需新增请求头。
        for headers in ({}, {"Origin": "http://localhost:3000"}):
            response = api.post(path, headers=headers, json=body)
            assert response.status_code == 200
            if path.endswith("stream"):
                assert sse_events(response)[-1][0] == "done"


@pytest.mark.parametrize("path", ["/chat", "/chat/stream", "/auth/login", "/auth/register"])
@pytest.mark.parametrize("origin,host,allowed", [
    ("http://127.0.0.1:8080", "127.0.0.1:8080", True),
    ("https://rag.example:8443", "rag.example:8443", True),  # TLS terminated at proxy
    ("https://rag.example", "rag.example:443", True),
    ("http://LOCALHOST:8080", "localhost:8080", True),
    ("http://[::1]:8080", "[::1]:8080", True),
    (None, "127.0.0.1:8080", True),
    ("http://localhost:3000", "127.0.0.1:8010", True),  # explicit whitelist
    ("https://evil.example", "127.0.0.1:8080", False),
    ("http://127.0.0.1:8080", "evil.example:8080", False),  # forged Host mismatch
    ("http://127.0.0.1:8080", "127.0.0.1:8010", False),
    ("http://127.0.0.1:8080.evil.example", "127.0.0.1:8080", False),
    ("http://127.0.0.1:8080/path", "127.0.0.1:8080", False),
    ("http://user@127.0.0.1:8080", "127.0.0.1:8080", False),
    ("http://127.0.0.1:8080", "127.0.0.1:8080@evil.example", False),
    ("null", "127.0.0.1:8080", False),
])
def test_proxy_authority_origin_policy(account_factory, path, origin, host, allowed):
    app, repo, _, _ = account_factory(trust_proxy_headers=False)
    headers = {**HEADERS, "Host": host}
    if origin is not None:
        headers["Origin"] = origin
    with TestClient(app) as api:
        response = api.post("/api/v1" + path, headers=headers, json={
            "session_id": "s", "message": "你好", "username": "Alice_01", "password": "A-unique-password!"})
        if allowed:
            assert response.status_code == (201 if path.endswith("register") else 401 if path.endswith("login") else 200)
        else:
            assert response.status_code == 403 and response.json()["code"] == 40301
            assert not repo.users
            app.state.services.faq_lookup.assert_not_called()


@pytest.mark.parametrize("trust", [False, True])
def test_forwarded_headers_cannot_override_raw_host_for_origin(account_factory, trust):
    app, _, _, _ = account_factory(trust_proxy_headers=trust)
    with TestClient(app, client=("127.0.0.1", 50000)) as api:
        response = api.post("/api/v1/auth/register", headers={
            **HEADERS, "Host": "127.0.0.1:8080", "Origin": "https://evil.example",
            "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https",
            "Forwarded": "host=evil.example;proto=https",
        }, json={"username": "Alice_01", "password": "A-unique-password!"})
        assert response.status_code == 403
        cookie = response.headers["set-cookie"]
        assert ("Secure" in cookie) == trust


def test_ambiguous_host_and_origin_headers_rejected(account_factory):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        for headers in (
            [("Host", "testserver"), ("Host", "evil.example"), ("Origin", "http://testserver")],
            [("Origin", "http://testserver"), ("Origin", "https://evil.example")],
        ):
            assert api.post("/api/v1/chat", headers=headers, json={"session_id": "s", "message": "你好"}).status_code == 403


def test_missing_username_never_accumulates_locks_but_still_checks_dummy_hash(account_factory):
    app, _, redis, _ = account_factory()
    store = app.state.accounts.store
    with TestClient(app) as api:
        with patch("app.auth.verify_password", wraps=verify_password) as verify, \
                patch.object(store, "check_lock", wraps=store.check_lock) as check_lock, \
                patch.object(store, "login_failure", wraps=store.login_failure) as failure:
            for _ in range(6):
                response = login(api, "Future_01", "incorrect")
                assert response.status_code == 401 and response.json()["message"] == "账号不存在" and response.json()["code"] == 40102
            assert verify.call_count == 6
            check_lock.assert_not_called()
            failure.assert_not_called()
        assert not store._failures
        assert not any(key.startswith("auth:failure:") for key in redis.values)
        # 注册后不会继承此前对同名未知用户名的猜测。
        assert register(api, "Future_01").status_code == 201
        assert login(api, "FUTURE_01").status_code == 200
        with patch.object(store, "login_failure", wraps=store.login_failure) as failure:
            assert login(api, "future_01", "incorrect").status_code == 401
            failure.assert_called_once_with(1)


def test_legacy_username_locks_cannot_lock_an_account_with_matching_numeric_id(account_factory):
    app, _, redis, _ = account_factory()
    with TestClient(app) as api:
        assert register(api).status_code == 201
        # 旧版不存在用户名 "1" 的锁，与真实 user_id=1 必须分属命名空间。
        redis.set("auth:failure:" + digest("1"), 5, ex=900)
        assert login(api).status_code == 200


@pytest.mark.parametrize("secret", ["", "   "])
def test_production_requires_secret_before_initializing_services(secret):
    settings = Settings(_env_file=None, debug=False, auth_secret=secret)
    with patch("app.main.Services") as services, patch("app.main.ChatLogWriter") as logs:
        with pytest.raises(ValueError, match="AUTH_SECRET"):
            create_app(settings)
        services.assert_not_called()
        logs.assert_not_called()


def test_production_accepts_shared_secret_and_development_warns(account_factory, caplog):
    app, _, _, _ = account_factory(debug=False, auth_secret="persistent-shared-secret")
    with TestClient(app) as api:
        assert api.get("/api/v1/auth/me").status_code == 200
    dev, _, _, _ = account_factory(debug=True, auth_secret="")
    with TestClient(dev):
        assert dev.state.accounts.secret
    assert "随机密钥不支持多 worker" in caplog.text


def test_independent_workers_share_guest_signature_with_configured_secret():
    settings = Settings(_env_file=None, debug=False, auth_secret="shared-secret")
    first, second = AccountService(settings), AccountService(settings)
    async def run():
        try:
            token = first.guest(None)
            assert second.guest(token) == token
        finally:
            await first.aclose()
            await second.aclose()
    asyncio.run(run())
