"""服务间内部接口（供 EcomOps 等调用，需 X-Internal-Token；INTERNAL_TOKEN 为空时整组接口返回 404）。

  POST /api/v1/rag/search      纯检索：向量+稀疏混合检索 + 重排，不经过 LLM、不占游客/账号配额
  POST /internal/v1/kb/upsert  把一条知识写进当前发布的集合（就地 upsert，稳定主键，重复调用不新增行）

返回的 score 是校准后的相关度（见 calibrated），不是重排分；rerank_score 是原始重排分，仅供参考。

租户/类目/生效期过滤取决于集合是否带规则元数据字段（`KB_RULE_METADATA=true` 的集合）：
  - 带字段：检索只看 公共规则 + 该租户规则（+ 类目），且当前时间在生效期内；响应 `filtered=true`。
  - 不带字段（现有的 kb_chunks）：tenant_id / catalog_category_id 被接受但**不参与过滤**，响应 `filtered=false`，
    调用方不能当作已经做了租户隔离。upsert 同理，元数据不会被存下来（响应 `rule_metadata_stored=false`）。
迁移到带字段的集合见开发文档第 9 节。
"""
import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import inspect
import math
import json
import logging
import re
from functools import partial

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, INVALID_ARGUMENT, NOT_FOUND, UNAUTHORIZED
from app.pipeline import call
from app.retrieval.retriever import rule_scope
from app.retrieval.store import NEVER_EXPIRES

logger = logging.getLogger(__name__)
KB_CATEGORY = "after_sales"          # 运营补录的是售后规则；分类集合见 ingest.loaders.CATEGORIES
SOURCE_PREFIX = "ecomops/"           # 文档来源前缀：doc_id = sha256(source)，同一 chunk_id 永远落在同一文档


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    tenant_id: str | None = Field(default=None, max_length=64)
    catalog_category_id: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("tenant_id", "catalog_category_id")
    @classmethod
    def safe_id(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", value):
            raise ValueError("只能含字母数字和 _ . : -，长度 1–64")
        return value

    @field_validator("query")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("query 不能为空白")
        return value


class UpsertBody(BaseModel):
    chunk_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=64)
    catalog_category_id: str | None = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=6000)
    rule_source: str = Field(min_length=1, max_length=200)
    is_demo_rule: bool = False
    version: int = Field(default=0, ge=0)
    effective_from: int | None = Field(default=None, ge=0)
    effective_to: int | None = Field(default=None, ge=0)

    @field_validator("tenant_id", "catalog_category_id")
    @classmethod
    def safe_id(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", value):
            raise ValueError("只能含字母数字和 _ . : -，长度 1–64")
        return value

    @field_validator("effective_to")
    @classmethod
    def ordered(cls, value, info):
        start = info.data.get("effective_from")
        if value is not None and start is not None and start > value:
            raise ValueError("effective_from 不能晚于 effective_to")
        return value

    @field_validator("text", "rule_source")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class KbWriter:
    """把一条知识切块、编码并就地写入当前发布的集合；沿用 ingest.run 的发布锁与"就地修改"登记。"""

    def __init__(self, settings, services, *, store_factory=None, manager_factory=None):
        self.settings, self.services = settings, services
        self._store_factory, self._manager_factory = store_factory, manager_factory

    def _store(self):
        if self._store_factory:
            return self._store_factory()
        from app.retrieval.store import MilvusStore
        return MilvusStore(self.settings.milvus_uri, timeout_s=self.settings.milvus_timeout_s,
                           skus_metadata=self.settings.product_skus_metadata,
                           rule_metadata=self.settings.kb_rule_metadata)

    def _manager(self, store):
        if self._manager_factory:
            return self._manager_factory(store)
        from ingest.versioning import VersionManager
        return VersionManager(store)

    @staticmethod
    def source_of(chunk_id: str) -> str:
        return SOURCE_PREFIX + re.sub(r"[^0-9A-Za-z_.-]", "_", chunk_id)[:100] + ".md"

    def upsert(self, body: UpsertBody) -> dict:
        from ingest.loaders import Document
        from ingest.splitter import split_document

        source = self.source_of(body.chunk_id)
        doc_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]    # 与 ingest.loaders 同一规则
        first, _, rest = body.text.strip().partition("\n")
        title = (first.strip() if rest.strip() else body.rule_source.strip())[:120]
        content = (rest.strip() or first.strip()) + f"\n\n来源：{body.rule_source.strip()}"
        document = Document(doc_id, title, content, source, KB_CATEGORY, None)
        s = self.settings
        chunks = split_document(document, parent_max_chars=s.parent_max_chars, child_chunk_size=s.child_chunk_size,
                                child_chunk_overlap=s.child_chunk_overlap)
        stored_rule = bool(s.kb_rule_metadata)
        if stored_rule:
            rule = {"tenant_id": body.tenant_id, "catalog_category_id": body.catalog_category_id or "",
                    "rule_source": body.rule_source.strip()[:256], "is_demo_rule": body.is_demo_rule,
                    "effective_from": body.effective_from or 0, "effective_to": body.effective_to or NEVER_EXPIRES,
                    "version": body.version}
            chunks = [dataclasses.replace(chunk, rule=rule) for chunk in chunks]
        store = self._store()
        manager = self._manager(store)
        with manager.locked():
            manager.recover()
            current = manager.current()
            if current:
                store = store.for_collection(current)
                record = manager.state["versions"].get(current)
                if record:      # 就地修改会使原发布清单失效，登记后拒绝用陈旧清单切回
                    record["status"] = "modified-in-place"
                    manager.save()
            for start in range(0, len(chunks), s.embed_batch_size):
                batch = chunks[start:start + s.embed_batch_size]
                lock = getattr(self.services, "_model_lock", None)
                if lock is not None:
                    with lock:
                        vectors = self.services.embedder.encode([c.child_text for c in batch])
                else:
                    vectors = self.services.embedder.encode([c.child_text for c in batch])
                store.upsert(batch, vectors)
            # 文本变短时旧的高位子块不能留下，否则重新发布后检索会同时命中新旧两版
            stale = self._stale_ids(store, doc_id, {c.chunk_id for c in chunks})
            if stale:
                store.delete_ids(stale)
            store.flush_load()
        return {"chunk_id": body.chunk_id, "milvus_pk": doc_id, "chunks": len(chunks), "removed_stale": len(stale),
                "rule_metadata_stored": stored_rule}

    @staticmethod
    def _stale_ids(store, doc_id: str, keep: set[str]) -> list[str]:
        client = store._connect()
        rows = client.query(collection_name=store.collection_name, filter=f'doc_id == "{doc_id}"',
                            output_fields=["chunk_id"], timeout=store.timeout_s)
        return [row["chunk_id"] for row in rows if row["chunk_id"] not in keep]


def calibrated(cosine: float, floor: float, ceiling: float) -> float:
    """把"查询与命中片段的稠密余弦"线性映射到 0..1。

    远程重排的分数几乎恒为 ~1.0（库外问题也是），不能当置信度；余弦在这个知识库上的实测分布是
    库内问题 0.68–0.76、库外问题 0.37–0.58（16 条样本，bge-m3），所以默认 0.45→0、0.70→1。"""
    if ceiling <= floor:
        raise ValueError("ceiling 必须大于 floor")
    return round(min(1.0, max(0.0, (cosine - floor) / (ceiling - floor))), 6)


def cosines(embedder, lock, query: str, texts: list[str]) -> list[float]:
    if not texts:
        return []
    def encode():
        return embedder.encode([query] + texts).dense
    if lock is not None:
        with lock:
            vectors = encode()
    else:
        vectors = encode()
    q = vectors[0]
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    out = []
    for v in vectors[1:]:
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append(sum(a * b for a, b in zip(q, v)) / (qn * vn))
    return out


def require_internal_token(settings):
    def dependency(request: Request):
        expected = settings.internal_token.get_secret_value()
        if not expected:
            raise AppError(NOT_FOUND, 404, "请求的地址不存在")      # 未配置令牌 = 接口不存在，不暴露其存在
        provided = request.headers.get("x-internal-token", "")
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            raise AppError(UNAUTHORIZED, 401, "内部令牌无效")
    return dependency


def internal_router(settings, services, writer: KbWriter):
    router = APIRouter()
    guard = Depends(require_internal_token(settings))

    @router.post("/api/v1/rag/search", dependencies=[guard])
    async def search(body: SearchBody):
        executor = getattr(services, "model_executor", None)
        filtered = bool(settings.kb_rule_metadata)
        try:
            with (rule_scope(body.tenant_id, body.catalog_category_id) if filtered else contextlib.nullcontext()):
                if executor is not None:
                    docs = await call(services.retrieve, [body.query], rerank_query=body.query, top_k=body.top_k,
                                      executor=executor)
                elif inspect.iscoroutinefunction(services.retrieve):
                    docs = await services.retrieve([body.query], rerank_query=body.query, top_k=body.top_k)
                else:
                    docs = await asyncio.to_thread(partial(services.retrieve, [body.query],
                                                           rerank_query=body.query, top_k=body.top_k))
        except AppError:
            raise
        except Exception as exc:
            from app.services import dependency_error
            if dependency_error(exc):
                raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试") from None
            raise
        sims = await asyncio.to_thread(cosines, services.embedder, getattr(services, "_model_lock", None),
                                       body.query, [d.snippet for d in docs])
        hits = []
        for d, sim in zip(docs, sims):
            rule = d.rule or {}
            hits.append({"chunk_id": d.parent_id, "text": d.snippet, "rule_source": rule.get("rule_source") or d.title,
                         "is_demo_rule": bool(rule.get("is_demo_rule", False)), "version": rule.get("version", 0),
                         "skus": d.skus, "score": calibrated(sim, settings.internal_score_floor, settings.internal_score_ceiling),
                         "cosine": round(sim, 6), "rerank_score": round(float(d.score), 6)})
        hits.sort(key=lambda h: (-h["score"], h["chunk_id"]))
        return {"hits": hits[:body.top_k], "filtered": filtered}

    @router.post("/internal/v1/kb/upsert", dependencies=[guard])
    async def upsert(body: UpsertBody):
        try:
            return await asyncio.to_thread(writer.upsert, body)
        except (ValueError, KeyError) as exc:
            raise AppError(INVALID_ARGUMENT, 400, f"知识内容无法入库：{exc}") from None
        except Exception as exc:
            from app.services import dependency_error
            if dependency_error(exc) or isinstance(exc, (OSError, RuntimeError)):
                logger.warning("kb_upsert_failed %s", type(exc).__name__)
                raise AppError(DEPENDENCY_UNAVAILABLE, 503, "知识库暂时不可用，请稍后再试") from None
            raise

    return router
