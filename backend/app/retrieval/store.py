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
DEFAULT_COLLECTION = "kb_chunks"  # 现有服务/入库共同使用的名字；迁移后同名 alias。


class MilvusStore:
    """显式写入时才加载 SDK；测试可同时注入客户端和字段类型。"""

    def __init__(self, uri: str = "http://127.0.0.1:19530", *,
                 collection_name: str = DEFAULT_COLLECTION, client=None, data_type=None, timeout_s=10):
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
        exists = client.has_collection(collection_name=self.collection_name)
        # 某些服务端 has_collection 只接受实体名；同名 alias 已存在时不能误建实体。
        if not exists:
            exists = self.alias_target(self.collection_name) is not None
        if not exists:
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

    # 以下是版本工具唯一的 SDK 边界，按 uv.lock 的 pymilvus 2.5.18 MilvusClient
    # 接口集中封装。真实服务端的 alias、rename、load、iterator 行为仍须部署前核对。
    def collections(self) -> list[str]:
        return self._connect().list_collections(timeout=self.timeout_s)

    def alias_target(self, alias: str) -> str | None:
        # 通过已存在实体的 list_aliases 查找，避免把网络/权限错误误判为 alias 不存在。
        client = self._connect()
        for name in self.collections():
            if alias in client.list_aliases(collection_name=name, timeout=self.timeout_s):
                return name
        return None

    def bind_alias(self, alias: str, version: str, *, exists: bool) -> None:
        client = self._connect()
        # alter_alias 是单次服务端原子操作；禁止 drop_alias + create_alias 的空窗方案。
        operation = client.alter_alias if exists else client.create_alias
        operation(collection_name=version, alias=alias, timeout=self.timeout_s)

    def rename(self, old: str, new: str) -> None:
        self._connect().rename_collection(old_name=old, new_name=new, timeout=self.timeout_s)

    def flush_load(self) -> None:
        client = self._connect()
        # flush 使持久化完成；load 后可检索。校验查询另外使用 Strong，不能仅凭 flush 计数。
        client.flush(collection_name=self.collection_name, timeout=self.timeout_s)
        client.load_collection(collection_name=self.collection_name, timeout=self.timeout_s)

    def release(self, version: str) -> None:
        self._connect().release_collection(collection_name=version, timeout=self.timeout_s)

    def drop(self, version: str) -> None:
        self._connect().drop_collection(collection_name=version, timeout=self.timeout_s)

    def entity_count(self, version: str) -> int:
        # stats 的 row_count 可能含尚未压实的墓碑；发布校验使用 iter_rows 的实际行数。
        return int(self._connect().get_collection_stats(
            collection_name=version, timeout=self.timeout_s)["row_count"])

    def iter_rows(self, *, fields=None):
        # 使用 iterator 而非 offset 分页，避免超过 query 的 result window 上限。
        iterator = self._connect().query_iterator(
            collection_name=self.collection_name, batch_size=256, filter="",
            output_fields=fields or [*TEXT_FIELDS, "dense"],
            consistency_level="Strong", timeout=self.timeout_s,
        )
        try:
            while True:
                rows = iterator.next()
                if not rows:
                    break
                yield from rows
        finally:
            iterator.close()

    def delete_ids(self, ids: list[str]) -> None:
        # 主键精确删除，不拼接 source 的模糊匹配表达式。
        for start in range(0, len(ids), 256):
            self._connect().delete(collection_name=self.collection_name,
                                   ids=ids[start:start + 256], timeout=self.timeout_s)

    def for_collection(self, name: str):
        return MilvusStore(self.uri, collection_name=name, client=self._connect(),
                           data_type=self.data_type, timeout_s=self.timeout_s)

    def close(self):
        if self.client is not None:
            self.client.close()
