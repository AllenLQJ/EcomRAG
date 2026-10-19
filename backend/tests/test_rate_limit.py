import asyncio
import threading
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


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_shared_proxy_sessions_have_independent_fifty_request_limits(backend):
    redis = FakeRedis() if backend == "redis" else None
    instance = RateLimiter(Settings(_env_file=None, rate_limit_ip_enabled=False),
                           redis=redis, clock=lambda: 100000.0)
    instance.cache._client = lambda: redis
    for index in range(50):
        instance.check("127.0.0.1", f"load-{index}")
    instance.check("127.0.0.1", "phone")
    for _ in range(49):
        instance.check("127.0.0.1", "load-0")
    with pytest.raises(AppError) as caught:
        instance.check("127.0.0.1", "load-0")
    assert (caught.value.code, caught.value.http_status) == (42901, 429)
    instance.check("127.0.0.1", "phone")
    assert not any(":ip:" in key for key in instance._counts)
    if redis:
        assert not any(":ip:" in key for key in redis.counts)


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_daily_limit_does_not_tell_user_to_wait_one_minute(backend):
    redis = FakeRedis() if backend == "redis" else None
    instance, clock = limiter(minute=1, day=1, redis=redis)
    instance.check("ip", "session")
    for elapsed in (0, 60):
        clock[0] += elapsed
        with pytest.raises(AppError) as caught:
            instance.check("ip", "session")
        assert (caught.value.code, caught.value.http_status, caught.value.message) == (
            42902, 429, "今天提问次数已达上限，请明天再试")
    clock[0] += 86400
    instance.check("ip", "session")


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


def test_cancelled_rate_limit_work_keeps_its_configured_capacity():
    async def run():
        settings = Settings(_env_file=None, rate_limit_io_workers=1, rate_limit_io_max_in_flight=1)
        instance = RateLimiter(settings)
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        def blocked(*args):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
        instance.check = blocked
        task = asyncio.create_task(instance.check_async("ip", "session"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(instance.check_async("other", "other"), 0.5)
            assert (caught.value.code, caught.value.http_status) == (50302, 503)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await instance.aclose()
    asyncio.run(run())


def test_local_capacity_lru_eviction_and_expired_windows():
    settings = Settings(_env_file=None, rate_limit_per_min=100, rate_limit_per_day=100)
    assert RateLimiter(settings).capacity == 10000
    clock = [100000.0]
    instance = RateLimiter(settings, capacity=8, clock=lambda: clock[0])
    instance.cache._client = lambda: None
    instance.check("a", "a")
    a_keys = set(instance._counts)
    instance.check("b", "b")
    b_keys = set(instance._counts) - a_keys
    instance.check("a", "a")  # 更新 a 的访问顺序。
    instance.check("c", "c")
    assert len(instance._counts) == 8
    assert a_keys <= instance._counts.keys() and not b_keys & instance._counts.keys()
    for i in range(30):
        instance.check(str(i), str(i))
        assert len(instance._counts) <= 8
    clock[0] += 86400
    instance.check("fresh", "fresh")
    assert len(instance._counts) == 4 and all(count == 1 for count, _ in instance._counts.values())


@pytest.mark.parametrize("trusted,expected", [(False, "127.0.0.1"), (True, "203.0.113.1")])
def test_forwarded_header_only_when_trusted(trusted, expected):
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                              headers={"x-forwarded-for": "203.0.113.1, 192.0.2.1"})
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
    with TestClient(app_factory(rate_limit_per_min=1, trust_proxy_headers=True),
                    client=("127.0.0.1", 50000)) as client:
        def ask(ip, session):
            return client.post("/api/v1/chat", headers={"X-Forwarded-For": ip},
                               json={"session_id": session, "message": "问题"})
        assert ask("203.0.113.1", "one").status_code == 200
        assert ask("203.0.113.2", "two").status_code == 200
        assert ask("203.0.113.3", "one").status_code == 429
        assert ask("203.0.113.2", "three").status_code == 429


def test_shared_proxy_api_does_not_charge_phone_for_another_session(app_factory):
    from fastapi.testclient import TestClient
    with TestClient(app_factory(rate_limit_per_min=1, rate_limit_ip_enabled=False),
                    client=("127.0.0.1", 50000)) as client:
        def ask(session):
            return client.post("/api/v1/chat", json={"session_id": session, "message": "问题"})
        assert ask("load-test").status_code == 200
        assert ask("phone").status_code == 200
        assert ask("load-test").status_code == 429


def test_fifty_concurrent_sessions_and_phone_succeed_behind_shared_proxy(app_factory):
    import httpx
    async def run():
        app = app_factory(rate_limit_ip_enabled=False)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                responses = await asyncio.gather(*[
                    client.post("/api/v1/chat", json={"session_id": f"load-{index}", "message": "你好"})
                    for index in range(50)])
                assert all(response.status_code == 200 for response in responses)
                phone = await client.post("/api/v1/chat", json={"session_id": "phone", "message": "你好"})
                assert phone.status_code == 200
        finally:
            await app.state.rate_limiter.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('trusted', [False, True])
@pytest.mark.parametrize('header,value,expected', [
    ('cf-connecting-ip', '203.0.113.7', '203.0.113.7'),
    ('cf-connecting-ip', '2001:db8::1', '2001:db8::1'),
    ('cf-connecting-ip', '', '127.0.0.1'),
    ('cf-connecting-ip', 'not-an-ip', '127.0.0.1'),
    ('cf-connecting-ip', '203.0.113.7, 203.0.113.8', '127.0.0.1'),
    ('x-forwarded-for', '203.0.113.8, 192.0.2.2', '203.0.113.8'),
    ('x-forwarded-for', 'invalid, 203.0.113.8', '127.0.0.1'),
])
def test_configured_ip_header_only_trusted_valid_addresses(trusted, header, value, expected):
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'), headers={header: value})
    settings = Settings(_env_file=None, trust_proxy_headers=trusted, client_ip_header=header)
    assert client_ip(request, settings) == (expected if trusted else '127.0.0.1')


def test_cloudflare_header_does_not_fall_back_to_spoofed_forwarded_header():
    request = SimpleNamespace(client=None, headers={'x-forwarded-for': '203.0.113.8'})
    assert client_ip(request, Settings(_env_file=None, trust_proxy_headers=True,
                                       client_ip_header='cf-connecting-ip')) == 'unknown'


@pytest.mark.parametrize('header', ['x-forwarded-for', 'cf-connecting-ip'])
@pytest.mark.parametrize('peer,networks,accepted', [
    ('127.0.0.1', ['127.0.0.1', '::1'], True),
    ('::1', ['127.0.0.1', '::1'], True),
    ('10.8.0.2', ['10.8.0.0/24'], True),
    ('2001:db8:1::2', ['2001:db8:1::/48'], True),
    ('10.8.1.2', ['10.8.0.0/24'], False),
    ('203.0.113.9', ['127.0.0.1', '::1'], False),
    ('127.0.0.1', [], False),
    ('not-an-address', ['127.0.0.1', '::1'], False),
])
def test_only_trusted_socket_peers_can_supply_client_ip(header, peer, networks, accepted):
    request = SimpleNamespace(client=SimpleNamespace(host=peer), headers={header: '198.51.100.7'})
    settings = Settings(_env_file=None, trust_proxy_headers=True, client_ip_header=header, trusted_proxies=networks)
    assert client_ip(request, settings) == ('198.51.100.7' if accepted else peer)


def test_proxy_allowlist_does_not_enable_headers_without_explicit_trust():
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'), headers={'x-forwarded-for': '198.51.100.7'})
    assert client_ip(request, Settings(_env_file=None, trusted_proxies=['127.0.0.0/8'])) == '127.0.0.1'
