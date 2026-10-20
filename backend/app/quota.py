"""角色自然日配额；游客同时受签名标识和可信 IP 约束。"""
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from app.account_store import digest
from app.contracts import AppError, DAILY_RATE_LIMITED, SERVICE_BUSY
from app.executor import BoundedExecutor
from app.faq.cache import FaqCache


class DailyQuota:
    def __init__(self, settings, *, redis=None, clock=time.time, capacity=10000):
        self.settings, self.clock, self.capacity = settings, clock, capacity
        self.cache = FaqCache(redis, url=settings.redis_url)
        self._counts, self._lock = {}, threading.Lock()
        self.executor = BoundedExecutor(settings.rate_limit_io_workers, settings.rate_limit_io_max_in_flight,
                                        name="quota-io")

    def usage(self, user_id, guest_id, ip, *, increment=False):
        now = self.clock()
        offset = self.settings.rate_limit_day_tz_offset_hours * 3600
        bucket = int((now + offset) // 86400)
        end = (bucket + 1) * 86400 - offset
        role = "user" if user_id is not None else "guest"
        limit = self.settings.user_daily_limit if user_id is not None else self.settings.guest_daily_limit
        identities = [("user", user_id)] if user_id is not None else [("guest", guest_id), ("ip", ip)]
        keys = [f"chat:quota:{kind}:{digest(value)}:{bucket}" for kind, value in identities]
        ttl = max(1, math.ceil(end - now))
        with self._lock:
            for key in list(self._counts):
                if self._counts[key][1] <= now:
                    del self._counts[key]
            if increment and len(self._counts) + sum(key not in self._counts for key in keys) > self.capacity:
                raise AppError(SERVICE_BUSY, 503, "服务有点忙，请稍后再试")
            local = [self._counts.get(key, (0, end))[0] + int(increment) for key in keys]
            if increment:
                for key, count in zip(keys, local):
                    self._counts[key] = (count, end)
            counts = local
            try:
                client = self.cache._client()
                if client is not None:
                    if increment:
                        with client.pipeline(transaction=True) as pipe:
                            for key in keys:
                                pipe.incr(key)
                                pipe.expire(key, ttl)
                            remote = pipe.execute()[::2]
                    else:
                        remote = [client.get(key) or 0 for key in keys]
                    counts = [max(left, int(right)) for left, right in zip(local, remote)]
                    # 远端已有计数同样进入本地镜像，断线后不清零。
                    for key, count in zip(keys, counts):
                        if increment or (count and (key in self._counts or len(self._counts) < self.capacity)):
                            self._counts[key] = (count, end)
            except self.cache._redis_errors:
                self.cache._warn("每日配额 Redis 不可用，使用本进程计数")
        used = max(counts)
        result = dict(role=role, limit=limit, used=used, remaining=max(0, limit - used),
                      resets_at=datetime.fromtimestamp(end, timezone(timedelta(seconds=offset))).isoformat())
        if increment and used > limit:
            message = (f"游客每天可提问{limit}次，今天已用完，登录后每天可提问{self.settings.user_daily_limit}次"
                       if role == "guest" else "今天提问次数已达上限，请明天再试")
            raise AppError(DAILY_RATE_LIMITED, 429, message, limit_scope=role)
        return result

    async def read(self, user_id, guest_id, ip):
        return await self.executor.run(self.usage, user_id, guest_id, ip)

    async def check(self, user_id, guest_id, ip):
        return await self.executor.run(self.usage, user_id, guest_id, ip, increment=True)

    async def aclose(self):
        await self.executor.aclose(lambda: self.cache.client.close() if self.cache.client is not None else None)
