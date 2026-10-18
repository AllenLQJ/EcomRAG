from __future__ import annotations

import math
from contextvars import ContextVar

from app.contracts import FaqHit
from app.faq.bm25 import Bm25Index


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("FAQ 向量维度不一致")
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(left, right)) / norm)) if norm else 0.0


class FaqService:
    """同步服务，构造/reload 时预计算全部 FAQ 问句；后续每次仅编码查询。"""

    def __init__(self, repo, cache, embedder, *, threshold=0.8, tokenizer=None, index_factory=None):
        if not 0 <= threshold <= 1:
            raise ValueError("FAQ 阈值必须在 0 到 1 之间")
        self.repo, self.cache, self.embedder = repo, cache, embedder
        self.threshold = threshold
        self.tokenizer, self.index_factory = tokenizer, index_factory
        self._top_score = ContextVar("faq_top_score", default=None)
        self.reload()

    @property
    def top_score(self) -> float | None:
        """当前调用上下文的 trace 分数；无候选时为 None，未达阈值时仍保留。"""
        return self._top_score.get()

    def reload(self) -> None:
        index = Bm25Index(self.repo, tokenizer=self.tokenizer, index_factory=self.index_factory)
        records = list(index.records.values())
        dense = self.embedder.encode([row.question for row in records]).dense if records else []
        if len(dense) != len(records):
            raise ValueError("FAQ 问句与向量数量不一致")
        # 完整构建后一次替换，失败时保留上一个快照。
        self._snapshot = (index, {row.id: vector for row, vector in zip(records, dense)})

    def faq_lookup(self, question: str) -> FaqHit | None:
        self._top_score.set(None)
        cached = self.cache.get(question)
        if cached is not None:
            self._top_score.set(cached.score)
            return cached.model_copy(update={"source": "cache"})
        index, vectors = self._snapshot
        candidates = index.top_k(question, k=5)
        if not candidates:
            return None
        query = self.embedder.encode([question]).dense[0]
        faq_id, score = max(((faq_id, cosine(query, vectors[faq_id]))
                             for faq_id, _ in candidates), key=lambda pair: pair[1])
        self._top_score.set(score)
        if score < self.threshold:
            return None
        row = index.records[faq_id]
        hit = FaqHit(faq_id=row.id, question=row.question, answer=row.answer,
                     category=row.category, score=score, source="faq")
        self.cache.set(question, hit)
        return hit
