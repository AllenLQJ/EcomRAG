from __future__ import annotations

import math
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import ContextVar
from time import monotonic, perf_counter

from app.contracts import FaqHit
from app.faq.bm25 import Bm25Index
from app.faq.cache import FaqCache, normalize
from app.faq.version import FaqVersion
from app.tracing import record


logger = logging.getLogger(__name__)


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("FAQ 向量维度不一致")
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(left, right)) / norm)) if norm else 0.0


class FaqService:
    """同步服务，构造/reload 时预计算全部 FAQ 问句；后续每次仅编码查询。"""

    def __init__(self, repo, cache, embedder, *, threshold=0.9, tokenizer=None, index_factory=None,
                 clear_cache_on_load=True, version_source=None, refresh_interval_s=3,
                 warning_interval_s=60, submit_refresh=None, model_lock=None):
        if not 0 <= threshold <= 1:
            raise ValueError("FAQ 阈值必须在 0 到 1 之间")
        self.repo, self.cache, self.embedder = repo, cache, embedder
        self.threshold = threshold
        self.tokenizer, self.index_factory = tokenizer, index_factory
        self._top_score = ContextVar("faq_top_score", default=None)
        self._model_lock = model_lock
        self._version_source = version_source
        if self._version_source is None and isinstance(cache, FaqCache):
            self._version_source = FaqVersion(cache, interval_s=refresh_interval_s,
                                              warning_interval_s=warning_interval_s)
        self._version = None
        self._refresh_interval_s = refresh_interval_s
        self._warning_interval_s = warning_interval_s
        self._next_retry = self._next_warning = 0
        self._refresh_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._refreshing = False
        self._closed = False
        self._pool = None
        self._query_vectors = {}
        self._submit_refresh = submit_refresh
        initial_version = self._version_source.read() if self._version_source is not None else None
        self.reload(clear_cache=clear_cache_on_load)
        if self._version_source is not None:
            current_version = self._version_source.read(force=True)
            # 启动期间版本稳定则对齐；若构建中发生发布/Redis 故障，保留构建前
            # 的版本，后续检查追赶，避免把可能过期的快照标成最新。
            self._version = current_version if current_version == initial_version else initial_version

    @property
    def top_score(self) -> float | None:
        """当前调用上下文的 trace 分数；无候选时为 None，未达阈值时仍保留。"""
        return self._top_score.get()

    def _encode(self, texts):
        with self._model_lock if self._model_lock is not None else nullcontext():
            dense = self.embedder.encode(texts).dense
        if len(dense) != len(texts):
            raise ValueError("FAQ 问句与向量数量不一致")
        return dense

    def _build(self, *, background=False):
        index = Bm25Index(self.repo, tokenizer=self.tokenizer, index_factory=self.index_factory)
        records = list(index.records.values())
        # 后台按单条编码，不在 DB/BM25 构建期间持有共享模型锁。
        dense = ([self._encode([row.question])[0] for row in records] if background
                 else self._encode([row.question for row in records]) if records else [])
        if len(dense) != len(records):
            raise ValueError("FAQ 问句与向量数量不一致")
        if dense and (not dense[0] or any(len(v) != len(dense[0]) or
                                           not all(math.isfinite(x) for x in v) for v in dense)):
            raise ValueError("FAQ 向量无效")
        # 内容指纹隔离旧 worker/在途请求的迟到写入，也隔离启动时的遗留缓存。
        payload = [(row.id, row.question, row.answer, row.category) for row in records]
        index.cache_namespace = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
        return index, {row.id: vector for row, vector in zip(records, dense)}

    def _install(self, snapshot, *, clear_cache):
        self._snapshot = snapshot
        if isinstance(self.cache, FaqCache):
            self.cache.namespace = snapshot[0].cache_namespace
        self._cache_needs_clear = not clear_cache
        if clear_cache:
            self.cache.clear()

    def reload(self, *, clear_cache=True) -> None:
        with self._build_lock:
            self._install(self._build(), clear_cache=clear_cache)

    def _check_version(self):
        if self._version_source is None or self._closed:
            return True
        version = self._version_source.read()
        if version is None:
            return False
        with self._refresh_lock:
            if version == self._version or self._refreshing or monotonic() < self._next_retry:
                return True
            self._refreshing = True
        def refresh():
            try:
                with self._build_lock:
                    snapshot = self._build(background=True)
                    self._install(snapshot, clear_cache=True)
                    self._version = version
                logger.info("FAQ 快照已生效 version=%s", version)
            except Exception:
                # 不输出异常文本、堆栈、SQL、FAQ 正文或连接地址。
                now = monotonic()
                self._next_retry = now + self._refresh_interval_s
                if now >= self._next_warning:
                    logger.warning("FAQ 快照重建失败，保留当前快照，稍后重试")
                    self._next_warning = now + self._warning_interval_s
            finally:
                with self._refresh_lock:
                    self._refreshing = False
        try:
            if self._submit_refresh is None:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="faq-refresh")
                future = self._pool.submit(refresh)
            else:
                future = self._submit_refresh(refresh)
            if future is None:
                with self._refresh_lock:
                    self._refreshing = False
            else:
                future.add_done_callback(self._refresh_done)
        except RuntimeError:
            with self._refresh_lock:
                self._refreshing = False
        return True

    def _refresh_done(self, future):
        # 尚未运行就被取消的任务不会进入 refresh 的 finally。
        if future.cancelled():
            with self._refresh_lock:
                self._refreshing = False

    def close(self):
        self._closed = True
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    def faq_lookup(self, question: str, *, use_cache=True) -> FaqHit | None:
        self._top_score.set(None)
        started = perf_counter()
        # 版本通知独立于答案缓存；多轮仍不读写答案缓存。
        available = self._check_version()
        use_cache = use_cache and available
        snapshot = self._snapshot
        namespace = ({"namespace": snapshot[0].cache_namespace}
                     if isinstance(self.cache, FaqCache) else {})
        # 首次由多轮构建索引时不能碰缓存；延迟至后续单轮查询恢复原有失效语义。
        if use_cache and self._cache_needs_clear:
            self.cache.clear()
            self._cache_needs_clear = False
        cached = self.cache.get(question, threshold=self.threshold, **namespace) if use_cache else None
        if use_cache:
            record("cache", started, hit=cached is not None)
        if cached is not None:
            self._top_score.set(cached.score)
            return cached.model_copy(update={"source": "cache"})
        started = perf_counter()
        hit = self._lookup_faq(question, use_cache=use_cache, snapshot=snapshot)
        record("faq", started, hit=hit is not None, top_score=self.top_score)
        return hit

    def _lookup_faq(self, question, *, use_cache=True, snapshot=None):
        index, vectors = snapshot if snapshot is not None else self._snapshot
        candidates = index.top_k(question, k=5)
        if not candidates:
            return None
        # 共享模型被后台编码占用时不等待：复用已有查询/精确问句向量；
        # 新语义问句按 FAQ miss 走原有后续路由，避免排在整次重建之后。
        if self._model_lock is None:
            query = self._encode([question])[0]
        elif self._model_lock.acquire(blocking=False):
            try:
                query = self.embedder.encode([question]).dense[0]
                self._query_vectors[question] = query
                if len(self._query_vectors) > 512:
                    self._query_vectors.pop(next(iter(self._query_vectors)))
            finally:
                self._model_lock.release()
        else:
            query = self._query_vectors.get(question)
            if query is None:
                query = next((vectors[faq_id] for faq_id, _ in candidates
                              if normalize(index.records[faq_id].question) == normalize(question)), None)
            if query is None:
                return None
        faq_id, score = max(((faq_id, cosine(query, vectors[faq_id]))
                             for faq_id, _ in candidates), key=lambda pair: pair[1])
        self._top_score.set(score)
        if score < self.threshold:
            return None
        row = index.records[faq_id]
        hit = FaqHit(faq_id=row.id, question=row.question, answer=row.answer,
                     category=row.category, score=score, source="faq")
        if use_cache:
            namespace = {"namespace": index.cache_namespace} if isinstance(self.cache, FaqCache) else {}
            self.cache.set(question, hit, **namespace)
        return hit
