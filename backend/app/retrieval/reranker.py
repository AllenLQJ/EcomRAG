from __future__ import annotations

import importlib
import math
import os
from pathlib import Path

from app.retrieval.embedder import select_device


class BgeReranker:
    """仅在首次重排时加载本地 BAAI/bge-reranker-v2-m3，禁止下载。"""

    def __init__(self, model_path: str | Path = "models/bge-reranker-v2-m3", *, model_factory=None):
        self.model_path = Path(model_path)
        self.model_factory = model_factory
        self._model = None

    def _load_model(self):
        if self._model is None:
            if not self.model_path.is_dir():
                raise FileNotFoundError(f"本地 reranker 目录不存在：{self.model_path}；不会下载模型")
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            device = select_device()
            factory = self.model_factory
            if factory is None:
                factory = importlib.import_module("FlagEmbedding").FlagReranker
            self._model = factory(str(self.model_path.resolve()), devices=device, use_fp16=False)
        return self._model

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        values = self._load_model().compute_score([[query, text] for text in texts], normalize=True)
        if not hasattr(values, "__iter__"):
            values = [values]
        scores = [float(value) for value in values]
        if len(scores) != len(texts) or not all(math.isfinite(value) for value in scores):
            raise ValueError("reranker 必须为每个候选返回一个有限分数")
        return scores


class FakeReranker:
    """可按父块正文指定分数；默认使用字符重合率，不加载模型。"""

    def __init__(self, scores: dict[str, float] | None = None):
        self.scores = scores
        self.calls = []

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        if self.scores is not None:
            return [float(self.scores[text]) for text in texts]
        chars = set(query)
        return [len(chars & set(text)) / max(len(chars), 1) for text in texts]
