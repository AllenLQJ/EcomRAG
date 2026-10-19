"""FAQ 专用发布信号；不包含数据库、问题或凭据日志。"""
import logging
import threading
from time import monotonic

from app.faq.cache import clear_cache

logger = logging.getLogger(__name__)
VERSION_KEY = "faq:data:version"


def publish(client):
    """先通知，再清理；清理失败仍可凭快照命名空间隔离旧答案。"""
    version = int(client.incr(VERSION_KEY))
    clear_cache(client)
    return version


class FaqVersion:
    def __init__(self, cache, *, interval_s=3, warning_interval_s=60, clock=monotonic):
        self.cache = cache
        self.interval_s = interval_s
        self.warning_interval_s = warning_interval_s
        self.clock = clock
        self._next_check = self._next_warning = 0
        self._value = None
        self._lock = threading.Lock()

    def read(self, *, force=False):
        now = self.clock()
        if (not force and now < self._next_check) or not self._lock.acquire(blocking=False):
            return self._value
        try:
            self._next_check = now + self.interval_s
            try:
                client = self.cache._client(warn_missing=False)
                if client is None:
                    raise ConnectionError
                self._value = int(client.get(VERSION_KEY) or 0)
                if self._value < 0:
                    raise ValueError
            except (*self.cache._redis_errors, ValueError, TypeError):
                self._value = None
                if now >= self._next_warning:
                    logger.warning("FAQ 版本读取失败，保留当前快照，稍后重试")
                    self._next_warning = now + self.warning_interval_s
            return self._value
        finally:
            self._lock.release()
