from __future__ import annotations

import importlib
import unicodedata

from app.faq.cache import normalize


class Bm25Index:
    """启动时加载；分词器与 BM25 工厂可注入，测试无需安装可选依赖。"""

    def __init__(self, repo, *, tokenizer=None, index_factory=None):
        self.repo = repo
        self.tokenizer = tokenizer
        self.index_factory = index_factory
        self.reload()

    def _tokens(self, question):
        if self.tokenizer is None:
            self.tokenizer = importlib.import_module("jieba").lcut
        return [token for token in self.tokenizer(normalize(question))
                if token.strip() and not all(unicodedata.category(c).startswith("P") for c in token)]

    def reload(self) -> None:
        records = self.repo.load_enabled()
        index = None
        if records:
            factory = self.index_factory
            if factory is None:
                factory = importlib.import_module("rank_bm25").BM25Okapi
            # 即便问句全为标点，也避免 BM25 的空词表/零长度异常。
            index = factory([self._tokens(row.question) or ["\0"] for row in records])
        self.records = {row.id: row for row in records}
        self._ids = [row.id for row in records]
        self._index = index

    def top_k(self, question: str, k: int = 5) -> list[tuple[int, float]]:
        if k <= 0 or self._index is None:
            return []
        scores = self._index.get_scores(self._tokens(question))
        return sorted(zip(self._ids, map(float, scores)), key=lambda row: row[1], reverse=True)[:k]
