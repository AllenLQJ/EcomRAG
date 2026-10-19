from __future__ import annotations

import hashlib
import importlib
import logging
import math
import string
import threading
import unicodedata
from time import monotonic

logger = logging.getLogger(__name__)
COMPARE_DELETE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"


def normalize(question: str) -> str:
    # 仅转换全角 ASCII 与全角空格，避免改变其他兼容字符的含义。
    text = "".join(" " if char == "\u3000" else chr(ord(char) - 0xFEE0)
                   if 0xFF01 <= ord(char) <= 0xFF5E else char for char in question).lower()

    def edge(char):
        return char.isspace() or char in string.punctuation or unicodedata.category(char).startswith("P")

    start, end = 0, len(text)
    while start < end and edge(text[start]):
        start += 1
    while end > start and edge(text[end - 1]):
        end -= 1
    return text[start:end]


def cache_key(question: str, namespace=None) -> str:
    prefix = "faq:cache:" + (f"{namespace}:" if namespace is not None else "")
    return prefix + hashlib.md5(normalize(question).encode("utf-8")).hexdigest()


class FaqCache:
    """Redis 不可用时降级，坏数据视为 miss；程序错误继续上抛。"""

    def __init__(self, client=None, *, url="redis://127.0.0.1:6379/0", ttl_s=86400, warning_interval_s=60):
        if ttl_s <= 0:
            raise ValueError("FAQ 缓存 TTL 必须大于零")
        self.client = client
        self.url = url
        self.ttl_s = ttl_s
        self.namespace = None
        self._warning_interval_s = warning_interval_s
        self._warnings = {}
        self._warning_lock = threading.Lock()
        # ConnectionError 和 TimeoutError 都是 OSError 的子类。
        self._redis_errors = (OSError,)

    def _warn(self, message):
        now = monotonic()
        with self._warning_lock:
            if now < self._warnings.get(message, 0):
                return
            self._warnings[message] = now + self._warning_interval_s
        logger.warning(message)

    def _client(self, *, warn_missing=True):
        try:
            redis = importlib.import_module("redis")
        except ImportError:
            # 注入的假客户端可在没有 redis 包时正常工作。
            if self.client is None and warn_missing:
                self._warn("FAQ Redis 依赖不可用，跳过缓存")
            return self.client
        self._redis_errors = (OSError, redis.exceptions.RedisError)
        if self.client is None:
            self.client = redis.Redis.from_url(
                self.url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1,
            )
        return self.client

    def get(self, question: str, *, threshold=0.9, namespace=None):
        namespace = self.namespace if namespace is None else namespace
        key = cache_key(question, namespace)
        try:
            client = self._client()
            if client is None:
                return None
            raw = client.get(key)
        except self._redis_errors:
            self._warn("FAQ Redis 缓存读取失败，跳过缓存")
            return None
        if raw is None:
            return None
        from pydantic import ValidationError

        from app.contracts import FaqHit

        try:
            hit = FaqHit.model_validate_json(raw)
        except ValidationError:
            self._warn("FAQ 缓存内容无效，按未命中处理")
            self.delete(question, expected=raw, namespace=namespace)
            return None
        if not math.isfinite(hit.score) or hit.score < threshold:
            self.delete(question, expected=raw, namespace=namespace)
            return None
        return hit.model_copy(update={"source": "cache"})

    def delete(self, question, *, expected=None, namespace=None):
        key = cache_key(question, self.namespace if namespace is None else namespace)
        try:
            client = self._client()
            if client is not None:
                if expected is None:
                    client.delete(key)
                else:
                    client.eval(COMPARE_DELETE, 1, key, expected)
        except self._redis_errors:
            self._warn("FAQ 缓存删除失败，跳过删除")

    def clear(self):
        try:
            client = self._client()
            return clear_cache(client) if client is not None else 0
        except self._redis_errors:
            self._warn("FAQ 缓存清理失败，跳过清理")
            return 0

    def set(self, question: str, hit, *, namespace=None) -> None:
        # 序列化和 key 构建在降级边界外，不能把程序错误当作 Redis 故障。
        value = hit.model_dump_json()
        key = cache_key(question, self.namespace if namespace is None else namespace)
        try:
            client = self._client()
            if client is not None:
                client.set(key, value, ex=self.ttl_s)
        except self._redis_errors:
            self._warn("FAQ Redis 缓存写入失败，跳过缓存")


def clear_cache(client, *, batch_size=200):
    """SCAN 的 COUNT 只是提示，DELETE 参数仍严格按 batch_size 分批。"""
    if batch_size <= 0:
        raise ValueError("批大小必须大于零")
    cursor, deleted = 0, 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match="faq:cache:*", count=batch_size)
        for offset in range(0, len(keys), batch_size):
            deleted += client.delete(*keys[offset:offset + batch_size])
        if int(cursor) == 0:
            return deleted
