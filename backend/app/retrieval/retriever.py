from __future__ import annotations

import importlib
import math

from app.contracts import RetrievedDoc

OUTPUT_FIELDS = ["parent_id", "doc_id", "title", "source", "parent_text", "child_text"]


class Retriever:
    """多查询混合检索，父块去重后按需重排；不自行加载配置或模型。"""

    def __init__(self, client, embedder, reranker=None, *,
                 uri="http://127.0.0.1:19530", collection_name="kb_chunks",
                 rerank_enabled=True, rerank_candidates=20,
                 timeout_s=10,
                 request_factory=None, rrf_factory=None):
        if rerank_candidates <= 0:
            raise ValueError("重排候选数量必须大于零")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Milvus 超时必须是正的有限秒数")
        self.client, self.embedder, self.reranker = client, embedder, reranker
        self.uri, self.collection_name = uri, collection_name
        self.timeout_s = timeout_s
        self.rerank_enabled, self.rerank_candidates = rerank_enabled, rerank_candidates
        self.request_factory, self.rrf_factory = request_factory, rrf_factory

    def _connect(self):
        if self.client is None or self.request_factory is None or self.rrf_factory is None:
            module = importlib.import_module("pymilvus")
            if self.client is None:
                self.client = module.MilvusClient(uri=self.uri, timeout=self.timeout_s)
            if self.request_factory is None:
                self.request_factory = module.AnnSearchRequest
            if self.rrf_factory is None:
                self.rrf_factory = module.RRFRanker
        return self.client

    def retrieve(self, queries: list[str], rerank_query: str, top_k: int = 5) -> list[RetrievedDoc]:
        if top_k <= 0 or not queries:
            return []
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
            results = client.hybrid_search(
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
        if self.rerank_enabled and candidates:
            if self.reranker is None:
                raise ValueError("开启重排时必须注入 reranker")
            candidates = candidates[:self.rerank_candidates]
            scores = self.reranker.rerank(rerank_query, [doc.content for doc in candidates])
            if len(scores) != len(candidates) or not all(math.isfinite(score) for score in scores):
                raise ValueError("reranker 必须为每个候选返回一个有限分数")
            candidates = [doc.model_copy(update={"score": float(score)})
                          for doc, score in zip(candidates, scores)]
            candidates.sort(key=lambda doc: doc.score, reverse=True)
        return candidates[:top_k]
