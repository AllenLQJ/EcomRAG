import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from account_fakes import HEADERS, login, register, sse_events
from app.auth import GUEST_COOKIE
from app.contracts import AppError


@pytest.mark.parametrize("path", ["/api/v1/chat", "/api/v1/chat/stream"])
@pytest.mark.parametrize("logged_in,limit", [(False, 5), (True, 50)])
def test_role_quotas_both_entries_and_me_does_not_count(account_factory, path, logged_in, limit):
    app, _, _, clock = account_factory()
    with TestClient(app) as api:
        if logged_in:
            register(api)
        before = api.get("/api/v1/auth/me").json()["quota"]
        assert before["used"] == 0 and before["limit"] == limit
        for index in range(limit):
            # 换客户端 session 不能改变角色身份。
            result = api.post(path, json={"session_id": f"s-{index}", "message": "你好"})
            assert result.status_code == 200
            if path.endswith("stream"):
                assert sse_events(result)[-1][0] == "done"
        result = api.post(path, json={"session_id": "new-session", "message": "你好"})
        error = sse_events(result)[-1][1] if path.endswith("stream") else result.json()
        assert (200 if path.endswith("stream") else 429) == result.status_code
        assert error["code"] == 42902 and error["limit_scope"] == ("user" if logged_in else "guest")
        assert error["message"] == ("今天提问次数已达上限，请明天再试" if logged_in else
                                    "游客每天可提问5次，今天已用完，登录后每天可提问50次")
        usage = api.get("/api/v1/auth/me").json()["quota"]
        for _ in range(3):
            assert api.get("/api/v1/auth/me").json()["quota"] == usage
        assert usage["remaining"] == 0 and usage["used"] == limit + 1
        reset = datetime.fromisoformat(usage["resets_at"])
        assert reset.hour == 0 and reset.utcoffset().total_seconds() == 8 * 3600
        clock[0] = reset.timestamp() + 1
        assert api.get("/api/v1/auth/me").json()["quota"]["used"] == 0
        result = api.post(path, json={"session_id": "new-day", "message": "你好"})
        assert result.status_code == 200
        if path.endswith("stream"):
            assert sse_events(result)[-1][0] == "done"


def test_guest_cookie_clearing_and_forging_cannot_bypass_ip_quota(account_factory):
    app, _, _, _ = account_factory(rate_limit_ip_enabled=False)
    with TestClient(app) as api:
        for _ in range(5):
            assert api.post("/api/v1/chat", json={"session_id": "s", "message": "你好"}).status_code == 200
        token = api.cookies[GUEST_COOKIE]
        api.cookies.clear()
        api.cookies.set(GUEST_COOKIE, token[:-1] + ("a" if token[-1] != "a" else "b"))
        result = api.post("/api/v1/chat", json={"session_id": "other", "message": "你好"})
        assert result.status_code == 429 and result.json()["limit_scope"] == "guest"


def test_guest_identity_remains_limited_after_ip_changes(account_factory):
    app, _, _, _ = account_factory(trust_proxy_headers=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as api:
        for index in range(6):
            response = api.post("/api/v1/chat", headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
                                json={"session_id": str(index), "message": "你好"})
            assert response.status_code == (200 if index < 5 else 429)


def test_user_quota_shared_across_sessions_and_independent_from_guest(account_factory):
    app, _, _, _ = account_factory(user_daily_limit=2, guest_daily_limit=1)
    with TestClient(app) as api:
        ask = lambda: api.post("/api/v1/chat", json={"session_id": "s", "message": "你好"})
        assert ask().status_code == 200
        assert ask().status_code == 429
        register(api)
        assert ask().status_code == 200
        api.post("/api/v1/auth/logout", headers=HEADERS)
        login(api)
        assert ask().status_code == 200
        assert ask().json()["limit_scope"] == "user"


def test_quota_redis_failure_keeps_previous_usage_and_is_atomic(account_factory):
    app, _, redis, _ = account_factory()
    quota = app.state.quota
    quota.usage(None, "guest", "ip", increment=True)
    redis.down = True
    def check(_):
        try:
            quota.usage(None, "guest", "ip", increment=True)
            return True
        except AppError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(check, range(20))) == 4
    assert quota.usage(None, "guest", "ip")["remaining"] == 0
    asyncio.run(app.state.accounts.aclose())
    asyncio.run(quota.aclose())
    asyncio.run(app.state.rate_limiter.aclose())


def test_legacy_daily_ceiling_still_applies(account_factory):
    app, _, _, _ = account_factory(rate_limit_per_day=1)
    with TestClient(app) as api:
        body = {"session_id": "s", "message": "你好"}
        assert api.post("/api/v1/chat", json=body).status_code == 200
        result = api.post("/api/v1/chat", json=body)
        assert result.status_code == 429 and result.json()["code"] == 42902


def test_quota_is_counted_on_faq_and_cache_hits(account_factory):
    from app.contracts import FaqHit
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        for index in range(6):
            app.state.services.faq_lookup.return_value = FaqHit(
                faq_id=1, question="问", answer="答", score=1, source="cache" if index % 2 else "faq")
            result = api.post("/api/v1/chat", json={"session_id": str(index), "message": "问"})
            assert result.status_code == (200 if index < 5 else 429)


def test_user_daily_error_wins_when_minute_limit_is_also_reached(account_factory):
    app, _, _, _ = account_factory(rate_limit_per_min=50)
    with TestClient(app) as api:
        register(api)
        body = {"session_id": "same-session", "message": "你好"}
        for _ in range(50):
            assert api.post("/api/v1/chat", json=body).status_code == 200
        error = api.post("/api/v1/chat", json=body)
        assert error.status_code == 429 and error.json()["code"] == 42902
        assert error.json()["limit_scope"] == "user"
