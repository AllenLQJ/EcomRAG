"""IP、session 分别按固定分钟/自然日窗口计数；Redis 故障退化为本进程。"""
import asyncio
import hashlib
import threading
import time
from collections import OrderedDict

from app.contracts import AppError, RATE_LIMITED
from app.faq.cache import FaqCache


class RateLimiter:
    def __init__(self, settings, *, redis=None, clock=time.time, capacity=10000):
        if capacity <= 0:
            raise ValueError("本地限流容量必须大于零")
        self.settings, self.clock = settings, clock
        self.cache = FaqCache(redis, url=settings.redis_url)
        self._counts = OrderedDict()
        self.capacity = capacity
        self._lock = threading.Lock()

    def check(self, ip, session_id):
        now = self.clock()
        entries = []
        for kind, value in (("ip", ip), ("session", session_id)):
            digest = hashlib.sha256(value.encode()).hexdigest()
            for window, limit in ((60, self.settings.rate_limit_per_min),
                                  (86400, self.settings.rate_limit_per_day)):
                bucket = int(now // window)
                end = (bucket + 1) * window
                entries.append((f"chat:rate:{kind}:{digest}:{window}:{bucket}",
                                max(1, int(end - now) + 1), end, limit))
        # 即使 Redis 正常也保留本地计数，断线后沿用已接受请求的计数。
        with self._lock:
            for key in list(self._counts):
                if self._counts[key][1] <= now:
                    del self._counts[key]
            local = []
            for key, _, end, _ in entries:
                count = self._counts.get(key, (0, end))[0] + 1
                self._counts[key] = (count, end)
                self._counts.move_to_end(key)
                local.append(count)
            while len(self._counts) > self.capacity:
                self._counts.popitem(last=False)
        counts = local
        try:
            client = self.cache._client()
            if client is not None:
                # INCR 与 EXPIRE 在同一事务内，避免进程退出留下无 TTL 的 key。
                with client.pipeline(transaction=True) as pipe:
                    for key, ttl, _, _ in entries:
                        pipe.incr(key)
                        pipe.expire(key, ttl)
                    counts = pipe.execute()[::2]
        except self.cache._redis_errors:
            pass
        if any(count > entry[3] for count, entry in zip(counts, entries)):
            raise AppError(RATE_LIMITED, 429, "提问有点快，休息一分钟再问吧")

    async def check_async(self, ip, session_id):
        await asyncio.to_thread(self.check, ip, session_id)

    async def aclose(self):
        if self.cache.client is not None:
            await asyncio.to_thread(self.cache.client.close)


def client_ip(request, settings):
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"
