from __future__ import annotations

import hashlib
import importlib
import math
import os
from collections import Counter
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path

DENSE_DIM = 1024
MAX_SPARSE_ID = 2**32 - 2


@dataclass
class EmbeddingBatch:
    dense: list[list[float]]
    sparse: list[dict[int, float]]


def validate_embeddings(batch: EmbeddingBatch, count: int) -> None:
    if len(batch.dense) != count or len(batch.sparse) != count:
        raise ValueError("向量数量与文本数量不一致")
    for dense, sparse in zip(batch.dense, batch.sparse):
        if len(dense) != DENSE_DIM or not all(math.isfinite(value) for value in dense):
            raise ValueError("dense 向量必须是 1024 维有限数值")
        if not sparse or any(type(index) is not int or not 0 <= index <= MAX_SPARSE_ID
                             or not math.isfinite(value) or value <= 0
                             for index, value in sparse.items()):
            raise ValueError("sparse 向量必须包含非负整数索引和正的有限权重")


def select_device() -> str:
    """只在加载模型时检测设备，不在模块顶层导入 torch。"""
    try:
        torch = importlib.import_module("torch")
    except ImportError as exc:
        raise RuntimeError("编码需要 torch，请通过 uv 安装声明的依赖") from exc
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    return "mps" if mps is not None and mps.is_available() else "cpu"


def sparse_token_ids(weights: dict, tokenizer=None) -> dict[int, float]:
    """原生 lexical_weights 的十进制键是词表 ID；文本 token 用 tokenizer 转换。"""
    result = {}
    for token, raw_weight in weights.items():
        weight = float(raw_weight)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("稀疏权重必须是非负有限数值")
        if weight == 0:
            continue
        if isinstance(token, Integral) and not isinstance(token, bool):
            token_id = int(token)
        elif isinstance(token, str) and token.isascii() and token.isdecimal():
            token_id = int(token)
        elif isinstance(token, str) and tokenizer is not None:
            token_id = tokenizer.convert_tokens_to_ids(token)
            if (token_id == getattr(tokenizer, "unk_token_id", None)
                    and token != getattr(tokenizer, "unk_token", None)):
                raise ValueError(f"稀疏 token 不在 tokenizer 词表中：{token!r}")
        else:
            raise ValueError(f"无法将稀疏 token 转为词表 ID：{token!r}")
        if not isinstance(token_id, Integral) or isinstance(token_id, bool) or not 0 <= token_id <= MAX_SPARSE_ID:
            raise ValueError("稀疏 token ID 超出 Milvus 允许的整数范围")
        token_id = int(token_id)
        result[token_id] = max(result.get(token_id, 0.0), weight)
    return result


class BgeM3Embedder:
    """仅加载本地 BGE-M3，第一次编码才导入 FlagEmbedding 和模型。"""

    def __init__(self, model_path: str | Path = "models/bge-m3", batch_size: int = 16,
                 *, model_factory=None):
        if batch_size <= 0:
            raise ValueError("编码批大小必须大于零")
        self.model_path = Path(model_path)
        self.batch_size = batch_size
        self.model_factory = model_factory
        self._model = None

    def _load_model(self):
        if self._model is None:
            if not self.model_path.is_dir():
                raise FileNotFoundError(f"本地 BGE-M3 目录不存在：{self.model_path}；不会下载模型")
            # 即使本地文件不完整，也禁止底层库尝试联网补齐。
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            device = select_device()
            factory = self.model_factory
            if factory is None:
                try:
                    factory = importlib.import_module("FlagEmbedding").BGEM3FlagModel
                except ImportError as exc:
                    raise RuntimeError("编码需要 FlagEmbedding 和 torch，请通过 uv 安装声明的依赖") from exc
            self._model = factory(str(self.model_path.resolve()), devices=device, use_fp16=False)
        return self._model

    def encode(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch([], [])
        model = self._load_model()
        result = model.encode(
            texts, batch_size=self.batch_size, return_dense=True,
            return_sparse=True, return_colbert_vecs=False,
        )
        batch = EmbeddingBatch(
            dense=[[float(value) for value in row] for row in result["dense_vecs"]],
            sparse=[sparse_token_ids(row, getattr(model, "tokenizer", None))
                    for row in result["lexical_weights"]],
        )
        validate_embeddings(batch, len(texts))
        return batch


class FakeEmbedder:
    """生成可复现的测试向量，不导入模型库、不进行语义编码。"""

    def encode(self, texts: list[str]) -> EmbeddingBatch:
        dense = []
        sparse = []
        for text in texts:
            values = [float(value + 1) for value in hashlib.sha256(text.encode()).digest()]
            norm = math.sqrt(sum(value * value for value in values))
            dense.append([value / norm for value in values] + [0.0] * (DENSE_DIM - len(values)))
            weights = Counter()
            for char in text or " ":
                index = int.from_bytes(hashlib.blake2b(char.encode(), digest_size=4).digest(), "big") % (MAX_SPARSE_ID + 1)
                weights[index] += 1.0
            sparse.append(dict(weights))
        return EmbeddingBatch(dense, sparse)
