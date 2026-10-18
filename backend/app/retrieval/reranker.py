from __future__ import annotations

import importlib
import logging
import math
import os
from pathlib import Path
from time import perf_counter

from app.retrieval.embedder import select_device

logger = logging.getLogger(__name__)


def rerank_text(doc, mode="child", max_chars=600):
    """snippet 是已带标题的最高分子块；截断仅用于 parent 模式。"""
    if mode == "child":
        return doc.snippet
    if mode == "parent":
        return doc.content[:max_chars] if max_chars else doc.content
    raise ValueError("重排文本模式必须为 child 或 parent")


class _TimedTokenizer:
    def __init__(self, tokenizer, timings):
        self.tokenizer, self.timings = tokenizer, timings

    def __getattr__(self, name):
        return getattr(self.tokenizer, name)

    def _call(self, method, *args, **kwargs):
        started = perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            self.timings["tokenize_ms"] += (perf_counter() - started) * 1000

    def __call__(self, *args, **kwargs):
        return self._call(self.tokenizer, *args, **kwargs)

    def pad(self, *args, **kwargs):
        return self._call(self.tokenizer.pad, *args, **kwargs)


class BgeReranker:
    """仅在首次重排时加载本地 BAAI/bge-reranker-v2-m3，禁止下载。"""

    def __init__(self, model_path: str | Path = "models/bge-reranker-v2-m3", *,
                 max_length=512, batch_size=16, fp16=False, model_factory=None, torch_module=None):
        if max_length <= 0 or batch_size <= 0:
            raise ValueError("重排 token 上限和批大小必须大于零")
        self.model_path = Path(model_path)
        self.model_factory = model_factory
        self.max_length, self.batch_size = max_length, batch_size
        self.fp16 = fp16
        self._torch = torch_module
        self.device, self.dtype = None, None
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
            use_fp16 = self.fp16 and device == "mps"
            model = factory(str(self.model_path.resolve()), devices=device, use_fp16=use_fp16)
            if use_fp16:
                # 部分 FlagEmbedding 版本在首次推理才 half；这里显式转换以记录真实 dtype。
                model.model.half()
            self._model = model
            self.device = device
            default_dtype = "float16" if use_fp16 else "float32"
            dtype = str(getattr(getattr(self._model, "model", None), "dtype", default_dtype))
            self.dtype = dtype.removeprefix("torch.") if not dtype.startswith("<") else default_dtype
            logger.info("reranker_loaded device=%s dtype=%s", self.device, self.dtype)
        return self._model

    def _torch_module(self):
        if self._torch is None:
            self._torch = importlib.import_module("torch")
        return self._torch

    def rerank(self, query: str, texts: list[str], *, batch_size=None) -> list[float]:
        if not texts:
            return []
        batch_size = self.batch_size if batch_size is None else batch_size
        if batch_size <= 0:
            raise ValueError("重排批大小必须大于零")
        model = self._load_model()
        ordered = sorted(enumerate(texts), key=lambda pair: len(pair[1]))
        scores = [0.0] * len(texts)
        with self._torch_module().inference_mode():
            for offset in range(0, len(ordered), batch_size):
                batch = ordered[offset:offset + batch_size]
                values = model.compute_score([[query, text] for _, text in batch], normalize=True,
                                             max_length=self.max_length, batch_size=batch_size)
                if not hasattr(values, "__iter__"):
                    values = [values]
                values = [float(value) for value in values]
                if len(values) != len(batch) or not all(math.isfinite(value) for value in values):
                    raise ValueError("reranker 必须为每个候选返回一个有限分数")
                for (index, _), value in zip(batch, values):
                    scores[index] = value
        return scores

    def profile(self, query, texts, *, batch_size=None):
        """测量实际 compute_score 路径；设备同步用于排除异步前向计时误差。"""
        model = self._load_model()
        torch = self._torch_module()
        timings = {"tokenize_ms": 0.0, "forward_ms": 0.0}
        tokenizer = model.tokenizer
        handles, starts = [], []

        def synchronize():
            if self.device == "mps":
                torch.mps.synchronize()

        def before(*args):
            synchronize()
            starts.append(perf_counter())

        def after(*args):
            synchronize()
            timings["forward_ms"] += (perf_counter() - starts.pop()) * 1000

        started = perf_counter()
        try:
            model.tokenizer = _TimedTokenizer(tokenizer, timings)
            handles.append(model.model.register_forward_pre_hook(before))
            handles.append(model.model.register_forward_hook(after))
            scores = self.rerank(query, texts, batch_size=batch_size)
            synchronize()
        finally:
            model.tokenizer = tokenizer
            for handle in handles:
                handle.remove()
        return {**timings, "total_ms": (perf_counter() - started) * 1000,
                "device": self.device, "dtype": self.dtype, "scores": scores}


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
