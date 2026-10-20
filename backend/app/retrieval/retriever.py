from __future__ import annotations

import importlib
import math
import logging
import re
import threading
from time import perf_counter

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, RetrievedDoc
from app.tracing import record
from app.retrieval.reranker import Reranker, rerank_text
from app.retrieval.store import DEFAULT_COLLECTION

OUTPUT_FIELDS = ["parent_id", "doc_id", "title", "source", "parent_text", "child_text"]
logger = logging.getLogger(__name__)


class Retriever:
    """多查询混合检索，父块去重后按需重排；不自行加载配置或模型。"""

    def __init__(self, client, embedder, reranker: Reranker | None = None, *,
                 uri="http://127.0.0.1:19530", collection_name=DEFAULT_COLLECTION,
                 rerank_enabled=True, rerank_candidates=10, rerank_max_chars=600,
                 rerank_text_mode="child",
                 rerank_fallback="rrf", local_rerank=None,
                 timeout_s=10, skus_metadata=False,
                 request_factory=None, rrf_factory=None):
        if rerank_candidates <= 0:
            raise ValueError("重排候选数量必须大于零")
        if rerank_max_chars < 0:
            raise ValueError("重排字符上限不能为负数")
        if rerank_text_mode not in {"child", "parent"}:
            raise ValueError("重排文本模式必须为 child 或 parent")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Milvus 超时必须是正的有限秒数")
        self.client, self.embedder, self.reranker = client, embedder, reranker
        # 保留逻辑 alias，每次 RPC 由服务端解析；不在进程内固定成实体版本名。
        self.uri, self.collection_name = uri, collection_name
        self.timeout_s = timeout_s
        # 名称与 rerank_candidates() 方法区分，避免实例属性遮蔽方法。
        self.rerank_enabled, self.candidate_limit = rerank_enabled, rerank_candidates
        self.rerank_max_chars = rerank_max_chars
        self.rerank_text_mode = rerank_text_mode
        self.rerank_fallback, self.local_rerank = rerank_fallback, local_rerank
        self.request_factory, self.rrf_factory = request_factory, rrf_factory
        self.skus_metadata = skus_metadata
        self._skus_schema_lock = threading.Lock()

    def _connect(self):
        if self.client is None or self.request_factory is None or self.rrf_factory is None:
            module = importlib.import_module("pymilvus")
            if self.client is None:
                try:
                    self.client = module.MilvusClient(uri=self.uri, timeout=self.timeout_s)
                except OSError:
                    raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试") from None
            if self.request_factory is None:
                self.request_factory = module.AnnSearchRequest
            if self.rrf_factory is None:
                self.rrf_factory = module.RRFRanker
        return self.client

    def _search(self, client, **kwargs):
        try:
            return client.hybrid_search(**kwargs)
        except Exception as exc:
            # alias 可在进程存活期间切回旧集合。只在明确的 skus 字段缺失错误后
            # 核对当前 schema；连接、权限、超时等错误不进入降级重试。
            message = str(exc).lower()
            missing_skus = (
                "skus" in kwargs["output_fields"]
                and re.search(r"\bskus\b", message)
                and re.search(r"\bfield", message)
                and re.search(r"not found|not exist|does not exist|doesn't exist|"
                              r"cannot (?:find|be found)|unknown field|non[- ]?existent field|not in schema", message)
            )
            if missing_skus:
                with self._skus_schema_lock:
                    if self.skus_metadata:
                        try:
                            schema = client.describe_collection(collection_name=self.collection_name,
                                                                timeout=self.timeout_s)
                        except OSError:
                            raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试") from None
                        if "skus" in {field["name"] for field in schema["fields"]}:
                            raise
                        self.skus_metadata = False
                        logger.warning("product_skus_metadata_unavailable：集合 schema 缺少 skus；"
                                       "本进程降级为不请求 skus，使用 manifest/文件名回退；"
                                       "重建集合后重启应用以恢复元数据读取")
                kwargs["output_fields"] = [field for field in kwargs["output_fields"] if field != "skus"]
                return self._search(client, **kwargs)
            if isinstance(exc, OSError):
                raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试") from None
            raise

    def retrieve(self, queries: list[str], rerank_query: str, top_k: int = 5) -> list[RetrievedDoc]:
        if top_k <= 0 or not queries:
            return []
        return self.rerank_candidates(rerank_query, self.retrieve_candidates(queries), top_k)

    def retrieve_candidates(self, queries: list[str]) -> list[RetrievedDoc]:
        if not queries:
            return []
        started = perf_counter()
        embeddings = self.embedder.encode(queries)
        if len(embeddings.dense) != len(queries) or len(embeddings.sparse) != len(queries):
            raise ValueError("查询与向量数量不一致")
        client = self._connect()
        parents = {}
        for dense, sparse in zip(embeddings.dense, embeddings.sparse):
            requests = [
                self.request_factory(data=[dense], anns_field="dense", limit=20,
                                     param={"metric_type": "COSINE", "params": {"ef": 64}}),
                self.request_factory(data=[sparse], anns_field="sparse", limit=20,
                                     param={"metric_type": "IP", "params": {}}),
            ]
            results = self._search(
                client,
                collection_name=self.collection_name, reqs=requests,
                ranker=self.rrf_factory(k=60), limit=40,
                output_fields=OUTPUT_FIELDS + (["skus"] if self.skus_metadata else []),
                timeout=self.timeout_s,
            )
            for hit in results[0] if results else []:
                entity = hit["entity"]
                doc = RetrievedDoc(
                    parent_id=entity["parent_id"], doc_id=entity["doc_id"],
                    title=entity["title"], source=entity["source"],
                    content=entity["parent_text"], snippet=entity["child_text"],
                    score=float(hit["distance"]),
                    skus=entity.get("skus"),
                )
                previous = parents.get(doc.parent_id)
                if previous is None or doc.score > previous.score:
                    parents[doc.parent_id] = doc
        candidates = sorted(parents.values(), key=lambda doc: doc.score, reverse=True)
        record("retrieve", started, candidates=len(candidates))
        return candidates

    def rerank_candidates(self, rerank_query: str, candidates: list[RetrievedDoc],
                          top_k: int = 5) -> list[RetrievedDoc]:
        if top_k <= 0:
            return []
        started = perf_counter()
        if self.rerank_enabled and candidates:
            if self.reranker is None:
                raise ValueError("开启重排时必须注入 reranker")
            candidates = candidates[:self.candidate_limit]
            texts = [rerank_text(doc, self.rerank_text_mode, self.rerank_max_chars) for doc in candidates]
            scores = self.reranker.rerank(rerank_query, texts)
            candidates = self._scored(candidates, scores, "local")
        record("rerank", started, kept=len(candidates[:top_k]), provider="local")
        return candidates[:top_k]

    @staticmethod
    def _scored(candidates, scores, provider):
        if len(scores) != len(candidates) or not all(math.isfinite(score) for score in scores):
            raise ValueError("reranker 必须为每个候选返回一个有限分数")
        return sorted([doc.model_copy(update={"score": float(score), "score_provider": provider})
                       for doc, score in zip(candidates, scores)], key=lambda doc: doc.score, reverse=True)

    async def arerank_candidates(self, rerank_query, candidates, top_k=5):
        """远程 I/O 直接 await；本地回退由 Services 注入受限线程池调用。"""
        if not self.rerank_enabled:
            return self.rerank_candidates(rerank_query, candidates, top_k)
        if top_k <= 0:
            return []
        started, provider, fallback = perf_counter(), "remote", None
        result = candidates
        if candidates:
            pool = candidates[:self.candidate_limit]
            texts = [rerank_text(doc, self.rerank_text_mode, self.rerank_max_chars) for doc in pool]
            try:
                scores = await self.reranker.rerank(rerank_query, texts)
                result = self._scored(pool, scores, provider)
            except AppError:
                if self.rerank_fallback == "error":
                    raise
                provider, fallback = "fallback_rrf", "rrf"
                if self.rerank_fallback == "local" and self.local_rerank is not None:
                    try:
                        scores = await self.local_rerank(rerank_query, texts)
                        provider, fallback = "fallback_local", "local"
                        result = self._scored(pool, scores, provider)
                    except Exception:
                        # 不回显本地模型异常里的路径、输入或供应商信息。
                        logger.warning("rerank_fallback_local_failed fallback=rrf")
                        provider, fallback = "fallback_rrf", "rrf"
                if provider == "fallback_rrf":
                    result = [doc.model_copy(update={"score_provider": provider}) for doc in candidates]
        record("rerank", started, kept=len(result[:top_k]), provider=provider, fallback=fallback)
        return result[:top_k]
