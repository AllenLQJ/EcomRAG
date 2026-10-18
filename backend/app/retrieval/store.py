from __future__ import annotations

import importlib
import math
from dataclasses import asdict

from app.retrieval.embedder import DENSE_DIM, EmbeddingBatch, validate_embeddings
from ingest.splitter import Chunk

# VARCHAR 长度单位为 UTF-8 字节，不是中文字符数。
TEXT_FIELDS = {
    "chunk_id": 64, "child_text": 2048, "parent_id": 64,
    "parent_text": 8192, "doc_id": 64, "title": 512,
    "source": 1024, "category": 32,
}


class MilvusStore:
    """显式写入时才加载 SDK；测试可同时注入客户端和字段类型。"""

    def __init__(self, uri: str = "http://127.0.0.1:19530", *,
                 collection_name: str = "kb_chunks", client=None, data_type=None, timeout_s=10):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Milvus 超时必须是正的有限秒数")
        self.timeout_s = timeout_s
        self.uri = uri
        self.collection_name = collection_name
        self.client = client
        self.data_type = data_type
        self._ready = False

    def _connect(self):
        if self.client is None or self.data_type is None:
            try:
                module = importlib.import_module("pymilvus")
            except ImportError as exc:
                raise RuntimeError("写入 Milvus 需要 pymilvus，请通过 uv 安装声明的依赖") from exc
            if self.client is None:
                self.client = module.MilvusClient(uri=self.uri, timeout=self.timeout_s)
            if self.data_type is None:
                self.data_type = module.DataType
        return self.client

    def ensure_collection(self) -> None:
        if self._ready:
            return
        client = self._connect()
        if not client.has_collection(collection_name=self.collection_name):
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            for name, length in TEXT_FIELDS.items():
                schema.add_field(field_name=name, datatype=self.data_type.VARCHAR,
                                 max_length=length, is_primary=name == "chunk_id")
            schema.add_field(field_name="dense", datatype=self.data_type.FLOAT_VECTOR, dim=DENSE_DIM)
            schema.add_field(field_name="sparse", datatype=self.data_type.SPARSE_FLOAT_VECTOR)
            indexes = client.prepare_index_params()
            indexes.add_index(field_name="dense", index_type="HNSW", metric_type="COSINE",
                              params={"M": 16, "efConstruction": 200})
            indexes.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
            client.create_collection(collection_name=self.collection_name, schema=schema,
                                     index_params=indexes, consistency_level="Bounded")
        self._ready = True

    def upsert(self, chunks: list[Chunk], embeddings: EmbeddingBatch):
        """以稳定 chunk_id 为主键覆盖写入，同一输入重复导入不会新增行。"""
        validate_embeddings(embeddings, len(chunks))
        if not chunks:
            return {"upsert_count": 0}
        rows = []
        for chunk, dense, sparse in zip(chunks, embeddings.dense, embeddings.sparse):
            row = asdict(chunk)
            for name, limit in TEXT_FIELDS.items():
                if len(row[name].encode("utf-8")) > limit:
                    raise ValueError(f"字段 {name} 超出 Milvus 的 {limit} 字节限制")
            rows.append({**row, "dense": dense, "sparse": sparse})
        self.ensure_collection()
        return self.client.upsert(collection_name=self.collection_name, data=rows, timeout=self.timeout_s)
