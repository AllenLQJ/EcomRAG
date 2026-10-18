from __future__ import annotations

import importlib
import math
from time import perf_counter

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, RetrievedDoc
from app.tracing import record
from app.retrieval.reranker import rerank_text

OUTPUT_FIELDS = ["parent_id", "doc_id", "title", "source", "parent_text", "child_text"]


class Retriever:
    """多查询混合检索，父块去重后按需重排；不自行加载配置或模型。"""

    def __init__(self, client, embedder, reranker=None, *,
                 uri="http://127.0.0.1:19530", collection_name="kb_chunks",
                 rerank_enabled=True, rerank_candidates=10, rerank_max_chars=600,
                 rerank_text_mode="child",
                 timeout_s=10,
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
        self.uri, self.collection_name = uri, collection_name
        self.timeout_s = timeout_s
        # 名称与 rerank_candidates() 方法区分，避免实例属性遮蔽方法。
        self.rerank_enabled, self.candidate_limit = rerank_enabled, rerank_candidates
        self.rerank_max_chars = rerank_max_chars
        self.rerank_text_mode = rerank_text_mode
        self.request_factory, self.rrf_factory = request_factory, rrf_factory

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

    @staticmethod
    def _search(client, **kwargs):
        try:
            return client.hybrid_search(**kwargs)
        except OSError:
            raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试") from None

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
                ranker=self.rrf_factory(k=60), limit=40, output_fields=OUTPUT_FIELDS,
                timeout=self.timeout_s,
            )
            for hit in results[0] if results else []:
                entity = hit["entity"]
                doc = RetrievedDoc(
                    parent_id=entity["parent_id"], doc_id=entity["doc_id"],
                    title=entity["title"], source=entity["source"],
                    content=entity["parent_text"], snippet=entity["child_text"],
                    score=float(hit["distance"]),
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
            if len(scores) != len(candidates) or not all(math.isfinite(score) for score in scores):
                raise ValueError("reranker 必须为每个候选返回一个有限分数")
            candidates = [doc.model_copy(update={"score": float(score)})
                          for doc, score in zip(candidates, scores)]
            candidates.sort(key=lambda doc: doc.score, reverse=True)
        record("rerank", started, kept=len(candidates[:top_k]))
        return candidates[:top_k]
