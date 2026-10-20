import asyncio
import base64
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from account_fakes import HEADERS, PASSWORD, login, register
from app.account_store import digest
from app.auth import GUEST_COOKIE, SESSION_COOKIE, hash_password, verify_password
from app.config import Settings


def test_register_login_rotate_me_logout(account_factory, caplog):
    app, repo, redis, _ = account_factory()
    with TestClient(app) as api:
        initial = api.get("/api/v1/auth/me").json()
        assert initial["user"] is None and initial["quota"]["role"] == "guest"
        result = register(api)
        assert result.status_code == 201
        assert result.json() == {"user": {"id": 1, "username": "Alice_01"}}
        old = api.cookies[SESSION_COOKIE]
        assert len(base64.urlsafe_b64decode(old + "=")) == 32
        assert redis.get("auth:session:" + digest(old)) == "1"
        assert redis.expires["auth:session:" + digest(old)] - redis.clock() == 604800
        stored = repo.users[1]["password_hash"]
        assert stored.startswith("scrypt$32768$8$1$") and PASSWORD not in stored
        assert verify_password(PASSWORD, stored) and not verify_password("incorrect", stored)
        current = api.get("/api/v1/auth/me").json()
        assert current["user"] == result.json()["user"] and current["quota"]["limit"] == 50
        signed_in = login(api, username="ALICE_01")
        assert signed_in.status_code == 200 and signed_in.json() == result.json()
        token = api.cookies[SESSION_COOKIE]
        assert token != old and redis.get("auth:session:" + digest(old)) is None
        assert api.post("/api/v1/auth/logout", headers=HEADERS).json() == {}
        assert SESSION_COOKIE not in api.cookies
        assert redis.get("auth:session:" + digest(token)) is None
        assert api.get("/api/v1/auth/me").json()["user"] is None
        assert api.post("/api/v1/auth/logout", headers=HEADERS).status_code == 200
    assert all(secret not in caplog.text for secret in (PASSWORD, old, token, stored))
    assert all(secret not in result.text + signed_in.text for secret in (PASSWORD, old, token, stored))


@pytest.mark.parametrize("username,password,reason", [
    ("ab", PASSWORD, "用户名"), ("a" * 21, PASSWORD, "用户名"),
    ("用户名", PASSWORD, "用户名"), ("abc-123", PASSWORD, "用户名"), (" abc", PASSWORD, "用户名"),
    ("Alice_01", "short", "8–64"), ("Alice_01", "a" * 65, "8–64"),
    ("Alice_01", "ALICE_01", "不能与用户名相同"), ("Alice_01", "12345678901", "纯数字"),
    ("Alice_01", "Password123", "过于常见"), ("Alice_01", "qwerty123", "过于常见"),
])
def test_registration_rules_return_displayable_chinese(account_factory, username, password, reason):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        result = register(api, username, password)
        assert result.status_code == 400 and result.json()["code"] == (40002 if reason == "用户名" else 40003)
        assert reason in result.json()["message"]
        assert not repo.users and password not in result.text


def test_case_insensitive_unique_preserves_display(account_factory):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        assert register(api).status_code == 201
        duplicate = register(api, "aLiCe_01")
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == 40901 and duplicate.json()["message"] == "用户名已被使用"
        assert len(repo.users) == 1


@pytest.mark.parametrize("field,value,code", [
    ("username", None, 40002), ("username", "a" * 4097, 40002),
    ("password", None, 40003), ("password", 12345678, 40003),
])
def test_registration_http_field_errors_have_specific_codes(account_factory, field, value, code):
    app, repo, _, _ = account_factory()
    with TestClient(app) as api:
        body = {"username": "Alice_01", "password": PASSWORD, field: value}
        with patch("app.auth.hash_password") as hash_call:
            response = api.post("/api/v1/auth/register", headers=HEADERS, json=body)
        assert response.status_code == 400 and response.json()["code"] == code
        assert ("用户名" if field == "username" else "密码") in response.json()["message"]
        assert not repo.users
        hash_call.assert_not_called()


def test_failures_are_distinguishable_and_both_hash(account_factory):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        with patch("app.auth.verify_password", wraps=verify_password) as verify:
            missing = login(api, "Unknown_01", "incorrect")
            wrong = login(api, "Alice_01", "incorrect")
        assert verify.call_count == 2
        assert all(call.args[1].startswith("scrypt$") for call in verify.call_args_list)
        for result, code, message in ((missing, 40102, "账号不存在"), (wrong, 40103, "密码错误")):
            assert result.status_code == 401
            assert result.json() == {"code": code, "message": message,
                                     "request_id": result.headers["X-Request-ID"]}


@pytest.mark.parametrize("redis_down", [False, True])
def test_lock_fallback_retry_and_success_reset(account_factory, redis_down):
    app, _, redis, clock = account_factory()
    store = app.state.accounts.store
    # 直接验证安全计数的降级，真实登录在 Redis 故障时必须拒绝建立会话。
    redis.down = redis_down
    for _ in range(4):
        store.login_failure("alice")
    with pytest.raises(Exception) as caught:
        store.login_failure("alice")
    assert caught.value.code == 42903 and caught.value.details["retry_after_seconds"] == 900
    clock[0] += 100
    with pytest.raises(Exception) as caught:
        store.check_lock("alice")
    assert caught.value.details["retry_after_seconds"] == 800
    clock[0] += 801
    store.check_lock("alice")
    redis.down = False
    store.login_failure("alice")
    store.login_success("alice")
    assert not store._failures
    asyncio.run(app.state.accounts.aclose())
    asyncio.run(app.state.quota.aclose())
    asyncio.run(app.state.rate_limiter.aclose())


def test_fifth_failed_login_locks_case_insensitively(account_factory):
    app, _, _, clock = account_factory()
    with TestClient(app) as api:
        register(api)
        for _ in range(4):
            assert login(api, password="incorrect").status_code == 401
        locked = login(api, username="ALICE_01", password="incorrect")
        assert locked.status_code == 429 and locked.json()["code"] == 42903
        assert locked.json()["retry_after_seconds"] == 900 and locked.headers["Retry-After"] == "900"
        assert login(api).status_code == 429
        clock[0] += 901
        assert login(api).status_code == 200


def test_ip_attempts_shared_between_registration_and_login(account_factory):
    app, _, _, clock = account_factory()
    with TestClient(app) as api:
        for index in range(20):
            result = register(api, "a", "weak") if index % 2 else login(api, f"unknown_{index}")
            assert result.status_code in {400, 401}
        result = register(api)
        assert result.status_code == 429 and result.json()["code"] == 42903
        assert result.json()["retry_after_seconds"] == 600
        clock[0] += 601
        assert register(api).status_code == 201


@pytest.mark.parametrize("path,method,body", [
    ("/auth/register", "POST", {"username": "Alice_01", "password": PASSWORD}),
    ("/auth/login", "POST", {"username": "Alice_01", "password": PASSWORD}),
    ("/auth/logout", "POST", {}), ("/auth/account", "DELETE", {"password": PASSWORD}),
    ("/conversations", "POST", {}), ("/conversations/other", "PATCH", {"title": "x"}),
    ("/conversations/other", "DELETE", {}),
])
def test_csrf_headers_and_origin(account_factory, path, method, body):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        for headers in ({}, {**HEADERS, "Origin": "https://evil.example"}):
            result = api.request(method, "/api/v1" + path, headers=headers, json=body)
            assert result.status_code == 403 and result.json()["code"] == 40301
        assert register(api).status_code == 201
        assert login(api).status_code == 200


@pytest.mark.parametrize("https,proxy,peer,secure", [
    (True, False, "198.51.100.1", True), (False, False, "127.0.0.1", False),
    (False, True, "127.0.0.1", True), (False, True, "198.51.100.1", False),
])
def test_cookie_flags_and_trusted_https(account_factory, https, proxy, peer, secure):
    app, _, _, _ = account_factory(trust_proxy_headers=proxy)
    with TestClient(app, base_url="https://testserver" if https else "http://testserver",
                    client=(peer, 50000), headers={"X-Forwarded-Proto": "https"}) as api:
        result = register(api)
        for name in (SESSION_COOKIE, GUEST_COOKIE):
            cookie = next(value for value in result.headers.get_list("set-cookie") if value.startswith(name + "="))
            assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
            assert "Max-Age=604800" in cookie and ("Secure" in cookie) == secure


def test_cors_credentials_and_explicit_origin(account_factory):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        result = api.options("/api/v1/conversations/x", headers={
            "Origin": "http://localhost:3000", "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "X-Requested-With,Content-Type"})
        assert result.status_code == 200
        assert result.headers["access-control-allow-credentials"] == "true"
        assert result.headers["access-control-allow-origin"] == "http://localhost:3000"
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_origins=["*"])


def test_redis_outage_fails_closed_but_guest_chat_survives(account_factory, caplog):
    app, _, redis, _ = account_factory()
    redis.down = True
    with TestClient(app) as api:
        for response in (register(api), login(api)):
            assert response.status_code == 503 and response.json()["code"] == 50301
        assert api.post("/api/v1/chat", json={"session_id": "s", "message": "你好"}).status_code == 200
    assert "PRIVATE_REDIS_ERROR" not in caplog.text


def test_expired_or_forged_session_is_guest(account_factory):
    app, _, _, clock = account_factory()
    with TestClient(app) as api:
        register(api)
        token = api.cookies[SESSION_COOKIE]
        clock[0] += 604801
        assert api.get("/api/v1/auth/me").json()["user"] is None
        api.cookies.set(SESSION_COOKIE, token[:-1] + ("a" if token[-1] != "a" else "b"))
        assert api.get("/api/v1/auth/me").json()["user"] is None


def test_hash_salts_are_random_and_corrupt_hash_is_rejected():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and verify_password(PASSWORD, first)
    assert not verify_password(PASSWORD, "scrypt$invalid")


def test_account_deletion_cascades_and_revokes_all_tokens(account_factory):
    app, repo, redis, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        old = api.cookies[SESSION_COOKIE]
        api.cookies.clear()
        login(api)
        token = api.cookies[SESSION_COOKIE]
        cid = api.post("/api/v1/conversations", headers=HEADERS, json={}).json()["id"]
        api.post("/api/v1/chat", json={"session_id": "s", "message": "隐私正文", "conversation_id": cid})
        bad = api.request("DELETE", "/api/v1/auth/account", headers=HEADERS, json={"password": "incorrect"})
        assert bad.status_code == 401 and repo.users
        result = api.request("DELETE", "/api/v1/auth/account", headers=HEADERS, json={"password": PASSWORD})
        assert result.status_code == 200 and result.json() == {}
        assert SESSION_COOKIE not in api.cookies and not repo.users and not repo.conversations and not repo.messages
        for value in (old, token):
            assert redis.get("auth:session:" + digest(value)) is None
            assert redis.get("auth:session_guard:" + digest(value)) is None
        assert api.request("DELETE", "/api/v1/auth/account", headers=HEADERS,
                           json={"password": PASSWORD}).status_code == 401
