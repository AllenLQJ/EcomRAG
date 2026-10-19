"""IP、session 分别按固定分钟/自然日窗口计数；Redis 故障退化为本进程。"""
import hashlib
from ipaddress import ip_address, ip_network
import threading
import time
from collections import OrderedDict

from app.contracts import AppError, DAILY_RATE_LIMITED, RATE_LIMITED
from app.executor import BoundedExecutor
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
        self._executor = BoundedExecutor(settings.rate_limit_io_workers, settings.rate_limit_io_max_in_flight,
                                         name="rate-limit-io")

    def check(self, ip, session_id):
        now = self.clock()
        entries = []
        identities = [("session", session_id)]
        if self.settings.rate_limit_ip_enabled:
            identities.append(("ip", ip))
        for kind, value in identities:
            digest = hashlib.sha256(value.encode()).hexdigest()
            for window, limit in ((60, self.settings.rate_limit_per_min),
                                  (86400, self.settings.rate_limit_per_day)):
                bucket = int(now // window)
                end = (bucket + 1) * window
                entries.append((f"chat:rate:{kind}:{digest}:{window}:{bucket}",
                                max(1, int(end - now) + 1), end, limit, window))
        # 即使 Redis 正常也保留本地计数，断线后沿用已接受请求的计数。
        with self._lock:
            for key in list(self._counts):
                if self._counts[key][1] <= now:
                    del self._counts[key]
            local = []
            for key, _, end, _, _ in entries:
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
                    for key, ttl, _, _, _ in entries:
                        pipe.incr(key)
                        pipe.expire(key, ttl)
                    counts = pipe.execute()[::2]
        except self.cache._redis_errors:
            pass
        exceeded = {entry[4] for count, entry in zip(counts, entries) if count > entry[3]}
        if 86400 in exceeded:
            raise AppError(DAILY_RATE_LIMITED, 429, "今天提问次数已达上限，请明天再试")
        if 60 in exceeded:
            raise AppError(RATE_LIMITED, 429, "提问有点快，休息一分钟再问吧")

    async def check_async(self, ip, session_id):
        await self._executor.run(self.check, ip, session_id)

    async def aclose(self):
        def close():
            if self.cache.client is not None:
                self.cache.client.close()
        await self._executor.aclose(close)


def client_ip(request, settings):
    peer = request.client.host if request.client else "unknown"
    if not settings.trust_proxy_headers:
        return peer
    try:
        address = ip_address(peer)
        trusted = any(address in ip_network(network, strict=False) for network in settings.trusted_proxies)
    except ValueError:
        trusted = False
    if trusted:
        header = settings.client_ip_header
        forwarded = request.headers.get(header, "").strip()
        if header == "x-forwarded-for":
            forwarded = forwarded.split(",", 1)[0].strip()
        try:
            return str(ip_address(forwarded))
        except ValueError:
            pass
    return peer
