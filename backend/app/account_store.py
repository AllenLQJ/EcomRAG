"""Redis 会话与防暴力破解；仅计数允许降级，绝不在本地建立会话。"""
import hashlib
import hmac
import math
import secrets
import threading
import time
from collections import deque

from app.contracts import AppError, AUTH_RATE_LIMITED, DEPENDENCY_UNAVAILABLE, SERVICE_BUSY
from app.faq.cache import FaqCache

SESSION_TTL = 7 * 86400
ATTEMPT_SCRIPT = """local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 600)
local count = redis.call('ZCARD', KEYS[1])
if count < 20 then
  redis.call('ZADD', KEYS[1], now, ARGV[2])
  redis.call('EXPIRE', KEYS[1], 600)
  return 0
end
local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return math.max(1, math.ceil(tonumber(first[2]) + 600 - now))
"""
FAIL_SCRIPT = """local count = redis.call('INCR', KEYS[1])
if count == 5 then redis.call('EXPIRE', KEYS[1], 900) end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""
LOCK_SCRIPT = """local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def unavailable():
    return AppError(DEPENDENCY_UNAVAILABLE, 503, "账号服务暂时不可用，请稍后再试")


def throttled(seconds):
    return AppError(AUTH_RATE_LIMITED, 429, "尝试次数过多，请稍后再试",
                    retry_after_seconds=max(1, math.ceil(seconds)))


class AccountStore:
    def __init__(self, settings, *, redis=None, clock=time.time, capacity=10000):
        self.cache = FaqCache(redis, url=settings.redis_url)
        self.clock, self.capacity = clock, capacity
        self._lock = threading.RLock()
        self._attempts, self._failures = {}, {}

    def client(self):
        client = self.cache._client()
        if client is None:
            raise OSError("Redis unavailable")
        return client

    def session(self, token, signature):
        if not token or len(token) > 200:
            return None
        try:
            client = self.client()
            guard = client.get("auth:session_guard:" + digest(token))
            if isinstance(guard, bytes):
                guard = guard.decode("ascii", errors="replace")
            if guard is None or not hmac.compare_digest(guard, signature):
                return None
            raw = client.get("auth:session:" + digest(token))
            return int(raw) if raw is not None else None
        except self.cache._redis_errors:
            # 无法确认会话时不授予账号身份；游客聊天仍受游客/IP 配额约束。
            self.cache._warn("会话 Redis 不可用，降级为游客")
            return None
        except (ValueError, TypeError):
            return None

    def new_session(self, user_id, token, old_token=None, *, signature):
        key = "auth:session:" + digest(token)
        try:
            client = self.client()
            # 用户索引让注销账号可原子撤销全部会话。
            with client.pipeline(transaction=True) as pipe:
                pipe.set(key, str(user_id), ex=SESSION_TTL)
                pipe.set("auth:session_guard:" + digest(token), signature, ex=SESSION_TTL)
                pipe.sadd(f"auth:user_sessions:{user_id}", key)
                pipe.expire(f"auth:user_sessions:{user_id}", SESSION_TTL)
                if old_token:
                    pipe.delete("auth:session:" + digest(old_token))
                    pipe.delete("auth:session_guard:" + digest(old_token))
                    pipe.srem(f"auth:user_sessions:{user_id}", "auth:session:" + digest(old_token))
                pipe.execute()
        except self.cache._redis_errors:
            raise unavailable() from None

    def remove_session(self, token):
        if not token:
            return
        try:
            client = self.client()
            key = "auth:session:" + digest(token)
            user_id = client.get(key)
            with client.pipeline(transaction=True) as pipe:
                pipe.delete(key, "auth:session_guard:" + digest(token))
                if user_id is not None:
                    if isinstance(user_id, bytes):
                        user_id = user_id.decode("ascii")
                    pipe.srem(f"auth:user_sessions:{int(user_id)}", key)
                pipe.execute()
        except self.cache._redis_errors:
            raise unavailable() from None

    def revoke_all(self, user_id):
        try:
            client = self.client()
            index = f"auth:user_sessions:{user_id}"
            # 不拼接令牌或用户输入到 Lua；集合只保存会话哈希 key。
            client.eval("local keys=redis.call('SMEMBERS',KEYS[1]); "
                        "for _,key in ipairs(keys) do redis.call('DEL',key); "
                        "redis.call('DEL','auth:session_guard:'..string.sub(key,14)) end; "
                        "return redis.call('DEL',KEYS[1])", 1, index)
        except self.cache._redis_errors:
            raise unavailable() from None

    def _room(self, values, key):
        if key not in values and len(values) >= self.capacity:
            # 不逐出仍有效的安全计数，避免用随机用户名/IP 冲掉锁定。
            raise AppError(SERVICE_BUSY, 503, "服务有点忙，请稍后再试")

    def attempt(self, ip):
        now = self.clock()
        key = "auth:attempt:" + digest(ip)
        with self._lock:
            for identity in list(self._attempts):
                entries = self._attempts[identity]
                while entries and entries[0] <= now - 600:
                    entries.popleft()
                if not entries:
                    del self._attempts[identity]
            self._room(self._attempts, key)
            entries = self._attempts.setdefault(key, deque())
            retry = max(1, math.ceil(entries[0] + 600 - now)) if len(entries) >= 20 else 0
            if not retry:
                entries.append(now)
            try:
                retry = max(retry, int(self.client().eval(ATTEMPT_SCRIPT, 1, key, now, secrets.token_hex(16))))
            except self.cache._redis_errors:
                self.cache._warn("账号防暴力破解 Redis 不可用，使用本进程计数")
            if retry:
                raise throttled(retry)

    def _failure_state(self, key, *, increment):
        now = self.clock()
        for identity, (_, end) in list(self._failures.items()):
            if end <= now:
                del self._failures[identity]
        self._room(self._failures, key)
        count, end = self._failures.get(key, (0, float("inf")))
        if increment:
            count += 1
            if count == 5:
                end = now + 900
        try:
            remote, ttl = self.client().eval(FAIL_SCRIPT if increment else LOCK_SCRIPT, 1, key)
            count = max(count, int(remote))
            if int(remote) >= 5:
                end = min(end, now + max(1, int(ttl)))
        except self.cache._redis_errors:
            self.cache._warn("账号防暴力破解 Redis 不可用，使用本进程计数")
        if count:
            self._failures[key] = (count, end)
        return max(1, math.ceil(end - now)) if count >= 5 and math.isfinite(end) else 0

    def check_lock(self, identity):
        with self._lock:
            retry = self._failure_state("auth:failure:user:" + digest(identity), increment=False)
            if retry:
                raise throttled(retry)

    def login_failure(self, identity):
        with self._lock:
            retry = self._failure_state("auth:failure:user:" + digest(identity), increment=True)
            if retry:
                raise throttled(retry)

    def login_success(self, identity):
        key = "auth:failure:user:" + digest(identity)
        with self._lock:
            try:
                self.client().delete(key)
            except self.cache._redis_errors:
                raise unavailable() from None
            self._failures.pop(key, None)
