from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.contracts import AppError
from app.rate_limit import RateLimiter, client_ip


class FakeRedis:
    def __init__(self):
        self.counts, self.ttls = {}, {}
        self.commands = []

    def pipeline(self, transaction):
        assert transaction is True
        return self

    def __enter__(self):
        self.commands = []
        return self

    def __exit__(self, *args):
        pass

    def incr(self, key):
        self.commands.append(("incr", key, None))

    def expire(self, key, ttl):
        self.commands.append(("expire", key, ttl))

    def execute(self):
        results = []
        for command, key, ttl in self.commands:
            if command == "incr":
                self.counts[key] = self.counts.get(key, 0) + 1
                results.append(self.counts[key])
            else:
                self.ttls[key] = ttl
                results.append(True)
        return results


def limiter(*, minute=2, day=3, redis=None):
    clock = [100000.0]
    instance = RateLimiter(Settings(_env_file=None, rate_limit_per_min=minute, rate_limit_per_day=day),
                           redis=redis, clock=lambda: clock[0])
    instance.cache._client = lambda: redis
    return instance, clock


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_minute_and_daily_limits_with_window_reset(backend):
    redis = FakeRedis() if backend == "redis" else None
    instance, clock = limiter(redis=redis)
    instance.check("ip", "s")
    instance.check("ip", "s")
    with pytest.raises(AppError) as caught:
        instance.check("ip", "s")
    assert (caught.value.code, caught.value.http_status) == (42901, 429)
    # 超限请求本身也计数；换分钟后仍受每日上限约束。
    clock[0] += 60
    with pytest.raises(AppError):
        instance.check("ip", "s")
    clock[0] += 86400
    instance.check("ip", "s")
    if redis:
        assert len(redis.ttls) == len(redis.counts)
        assert all(ttl > 0 for ttl in redis.ttls.values())
        assert all("chat:rate:" in key for key in redis.counts)


@pytest.mark.parametrize("same", ["ip", "session"])
def test_ip_and_session_independently_block(same):
    instance, _ = limiter(minute=1)
    instance.check("ip1", "session1")
    with pytest.raises(AppError):
        instance.check("ip1" if same == "ip" else "ip2", "session1" if same == "session" else "session2")
    instance.check("ip3", "session3")


def test_minute_reset_still_allows_under_daily_limit():
    instance, clock = limiter(minute=1, day=3)
    instance.check("ip", "s")
    clock[0] += 60
    instance.check("ip", "s")


def test_redis_failure_falls_back_and_preserves_previous_counts():
    redis = FakeRedis()
    instance, _ = limiter(minute=1, redis=redis)
    instance.check("ip", "s")
    def down():
        raise ConnectionError("Redis down")
    instance.cache._client = down
    with pytest.raises(AppError):
        instance.check("ip", "s")
    instance.check("another", "another")


def test_redis_sdk_error_falls_back_but_programming_error_propagates():
    class RedisError(Exception):
        pass
    instance, _ = limiter(minute=1)
    instance.cache._redis_errors = (OSError, RedisError)
    for exc in (RedisError(), TypeError("bug")):
        def fail():
            raise exc
        instance.cache._client = fail
        if isinstance(exc, RedisError):
            instance.check("one", "one")
            with pytest.raises(AppError):
                instance.check("one", "one")
        else:
            with pytest.raises(TypeError):
                instance.check("two", "two")


def test_local_counter_is_atomic_and_instances_are_isolated():
    instance, _ = limiter(minute=5, day=100)
    def check(_):
        try:
            instance.check("ip", "s")
            return True
        except AppError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(check, range(30))) == 5
    fresh, _ = limiter(minute=1)
    fresh.check("ip", "s")


@pytest.mark.parametrize("trusted,expected", [(False, "socket-ip"), (True, "proxy-ip")])
def test_forwarded_header_only_when_trusted(trusted, expected):
    request = SimpleNamespace(client=SimpleNamespace(host="socket-ip"),
                              headers={"x-forwarded-for": "proxy-ip, proxy2"})
    assert client_ip(request, Settings(_env_file=None, trust_proxy_headers=trusted)) == expected


def test_api_429_body_and_independent_app_state(app_factory):
    from fastapi.testclient import TestClient
    app = app_factory(rate_limit_per_min=1)
    with TestClient(app) as client:
        assert client.post("/api/v1/chat", json={"session_id": "s", "message": "问题"}).status_code == 200
        result = client.post("/api/v1/chat", json={"session_id": "s2", "message": "问题"})
    assert result.status_code == 429
    assert result.json() == {"code": 42901, "message": "提问有点快，休息一分钟再问吧",
                             "request_id": result.headers["X-Request-ID"]}
    with TestClient(app_factory(rate_limit_per_min=1)) as client:
        assert client.post("/api/v1/chat", json={"session_id": "s", "message": "问题"}).status_code == 200


def test_api_trusted_proxy_and_session_limits(app_factory):
    from fastapi.testclient import TestClient
    with TestClient(app_factory(rate_limit_per_min=1, trust_proxy_headers=True)) as client:
        def ask(ip, session):
            return client.post("/api/v1/chat", headers={"X-Forwarded-For": ip},
                               json={"session_id": session, "message": "问题"})
        assert ask("proxy1", "one").status_code == 200
        assert ask("proxy2", "two").status_code == 200
        assert ask("proxy3", "one").status_code == 429
        assert ask("proxy2", "three").status_code == 429
