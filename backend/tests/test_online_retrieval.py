import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.retrieval.embedder import FakeEmbedder
from app.retrieval.reranker import FakeReranker
from app.retrieval.retriever import OUTPUT_FIELDS, Retriever


def hit(parent, score, snippet=None):
    return {"distance": score, "entity": {
        "parent_id": parent, "doc_id": "doc-" + parent,
        "title": "标题-" + parent, "source": parent + ".md",
        "parent_text": "父块-" + parent, "child_text": snippet or "子块-" + parent,
    }}


class FakeMilvus:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def hybrid_search(self, **kwargs):
        self.calls.append(kwargs)
        return [next(self.results)]


class RetrievalTests(unittest.TestCase):
    def build(self, results, *, enabled=False, limit=20, mode="parent"):
        client = FakeMilvus(results)
        encoder = FakeEmbedder()
        reranker = FakeReranker({"父块-a": 0.2, "父块-b": 0.9, "父块-c": 0.5})
        retriever = Retriever(client, encoder, reranker, rerank_enabled=enabled,
                              rerank_candidates=limit,
                              rerank_text_mode=mode,
                              request_factory=lambda **kw: SimpleNamespace(**kw),
                              rrf_factory=lambda **kw: SimpleNamespace(**kw))
        return retriever, client, reranker

    def test_hybrid_requests_are_dense20_sparse20_rrf60(self):
        retriever, client, _ = self.build([[hit("a", 0.03)]])
        docs = retriever.retrieve(["退货条件"], "原始用户问题")
        call = client.calls[0]
        self.assertEqual(call["collection_name"], "kb_chunks")
        self.assertEqual(call["limit"], 40)
        self.assertEqual(call["timeout"], 10)
        self.assertEqual(call["output_fields"], OUTPUT_FIELDS)
        self.assertEqual(call["ranker"].k, 60)
        dense, sparse = call["reqs"]
        expected = FakeEmbedder().encode(["退货条件"])
        self.assertEqual((dense.anns_field, dense.limit, dense.data), ("dense", 20, expected.dense))
        self.assertEqual(dense.param, {"metric_type": "COSINE", "params": {"ef": 64}})
        self.assertEqual((sparse.anns_field, sparse.limit, sparse.data), ("sparse", 20, expected.sparse))
        self.assertEqual(sparse.param, {"metric_type": "IP", "params": {}})
        self.assertTrue(all(type(key) is int for key in sparse.data[0]))
        self.assertEqual(docs[0].model_dump(), {
            "parent_id": "a", "doc_id": "doc-a", "title": "标题-a", "source": "a.md",
            "content": "父块-a", "snippet": "子块-a", "score": 0.03,
        })

    def test_multiquery_dedup_keeps_best_child_and_rrf_score(self):
        retriever, client, reranker = self.build([
            [hit("a", 0.01, "低分子块"), hit("b", 0.02), hit("a", 0.03, "中分子块")],
            [hit("c", 0.04), hit("a", 0.05, "最高分子块")],
        ])
        docs = retriever.retrieve(["子问题1", "子问题2"], "原始问题", top_k=2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([doc.parent_id for doc in docs], ["a", "c"])
        self.assertEqual([doc.score for doc in docs], [0.05, 0.04])
        self.assertEqual(docs[0].snippet, "最高分子块")
        self.assertEqual(docs[0].content, "父块-a")
        self.assertEqual(reranker.calls, [])

    def test_rerank_uses_original_query_parent_text_and_changes_scores(self):
        retriever, _, reranker = self.build([
            [hit("a", 0.06, "a1"), hit("a", 0.01, "a2"), hit("b", 0.05)],
            [hit("c", 0.02)],
        ], enabled=True)
        docs = retriever.retrieve(["改写问题", "子问题"], "原始用户问题", top_k=2)
        self.assertEqual(reranker.calls, [("原始用户问题", ["父块-a", "父块-b", "父块-c"])])
        self.assertEqual([doc.parent_id for doc in docs], ["b", "c"])
        self.assertEqual([doc.score for doc in docs], [0.9, 0.5])

    def test_rerank_candidate_limit_applied_after_dedup_and_before_rerank(self):
        retriever, _, reranker = self.build([
            [hit("a", 0.06), hit("a", 0.05), hit("b", 0.04), hit("c", 0.03)],
        ], enabled=True, limit=2)
        docs = retriever.retrieve(["问题"], "原始问题", top_k=10)
        self.assertEqual(reranker.calls, [("原始问题", ["父块-a", "父块-b"])])
        self.assertEqual([doc.parent_id for doc in docs], ["b", "a"])

    def test_disabled_rerank_does_not_limit_candidates(self):
        retriever, _, _ = self.build([[hit("a", 0.03), hit("b", 0.02), hit("c", 0.01)]], limit=1)
        self.assertEqual(len(retriever.retrieve(["问题"], "原始问题", top_k=5)), 3)

    def test_top_k_above_candidate_count_keeps_all_candidates_in_both_paths(self):
        for enabled in (False, True):
            with self.subTest(rerank_enabled=enabled):
                retriever, client, _ = self.build([[hit("a", 0.03), hit("b", 0.02)]], enabled=enabled)
                candidates = retriever.retrieve_candidates(["问题"])
                docs = retriever.rerank_candidates("原始问题", candidates, top_k=99)
                expected = ["b", "a"] if enabled else ["a", "b"]
                self.assertEqual([doc.parent_id for doc in docs], expected)
                self.assertEqual(len(docs), len(candidates))
                retriever, _, _ = self.build([[hit("a", 0.03), hit("b", 0.02)]], enabled=enabled)
                self.assertEqual([doc.parent_id for doc in retriever.retrieve(["问题"], "原始问题", top_k=99)],
                                 expected)

    def test_split_retrieval_preserves_dedup_and_only_truncates_rerank_inputs(self):
        retriever, client, reranker = self.build([[hit("a", 0.03), hit("b", 0.02), hit("a", 0.01)]], enabled=True)
        retriever.rerank_max_chars = 3
        reranker.scores = None
        candidates = retriever.retrieve_candidates(["问题"])
        self.assertEqual([doc.parent_id for doc in candidates], ["a", "b"])
        self.assertEqual(reranker.calls, [])
        docs = retriever.rerank_candidates("原问题", candidates, 2)
        self.assertEqual(reranker.calls, [("原问题", ["父块-", "父块-"])])
        self.assertEqual(docs[0].content, "父块-a")
        self.assertEqual(candidates[0].score, 0.03)
        retriever.rerank_max_chars = 0
        retriever.rerank_candidates("q", candidates, 2)
        self.assertEqual(reranker.calls[-1][1], ["父块-a", "父块-b"])
        self.assertEqual(len(client.calls), 1)

    def test_child_mode_uses_best_child_with_title_without_parent_truncation(self):
        retriever, _, reranker = self.build([
            [hit("a", 0.01, "标题-a\n低分子块"), hit("b", 0.02, "标题-b\n另一子块")],
            [hit("a", 0.05, "标题-a\n最高分子块")],
        ], enabled=True, mode="child")
        retriever.rerank_max_chars = 1  # child 不受 parent 字符截断配置影响。
        reranker.scores = {"标题-a\n最高分子块": 0.9, "标题-b\n另一子块": 0.7}
        docs = retriever.retrieve(["查询一", "查询二"], "原问题")
        self.assertEqual(reranker.calls, [("原问题", ["标题-a\n最高分子块", "标题-b\n另一子块"])])
        self.assertEqual(docs[0].content, "父块-a")
        self.assertEqual(docs[0].snippet, "标题-a\n最高分子块")
        self.assertEqual([doc.score for doc in docs], [0.9, 0.7])

    def test_default_text_mode_is_child(self):
        retriever = Retriever(None, FakeEmbedder(), rerank_enabled=False)
        self.assertEqual(retriever.rerank_text_mode, "child")

    def test_empty_queries_results_and_zero_top_k_skip_rerank(self):
        retriever, client, reranker = self.build([[]], enabled=True)
        self.assertEqual(retriever.retrieve([], "原始问题"), [])
        self.assertEqual(retriever.retrieve(["问题"], "原始问题", top_k=0), [])
        self.assertEqual(client.calls, [])
        self.assertEqual(retriever.retrieve(["问题"], "原始问题"), [])
        self.assertEqual(reranker.calls, [])

    def test_sdk_request_and_ranker_factories_are_loaded_lazily(self):
        client = FakeMilvus([[hit("a", 0.03)]])
        sdk = SimpleNamespace(AnnSearchRequest=Mock(side_effect=lambda **kw: kw),
                              RRFRanker=Mock(side_effect=lambda **kw: kw))
        retriever = Retriever(client, FakeEmbedder(), rerank_enabled=False)
        with patch("app.retrieval.retriever.importlib.import_module", return_value=sdk) as loader:
            loader.assert_not_called()
            retriever.retrieve(["问题"], "原始问题")
        loader.assert_called_once_with("pymilvus")
        sdk.RRFRanker.assert_called_once_with(k=60)
        self.assertEqual(sdk.AnnSearchRequest.call_count, 2)

    def test_invalid_reranker_scores_fail_explicitly(self):
        for scores in ([], [float("nan")]):
            retriever, _, _ = self.build([[hit("a", 0.03)]], enabled=True)
            retriever.reranker = Mock()
            retriever.reranker.rerank.return_value = scores
            with self.assertRaisesRegex(ValueError, "有限分数"):
                retriever.retrieve(["问题"], "原始问题")

    def test_custom_timeout_is_passed_to_connection_and_each_search(self):
        client = FakeMilvus([[hit("a", 0.03)], [hit("b", 0.02)]])
        sdk = SimpleNamespace(MilvusClient=Mock(return_value=client),
                              AnnSearchRequest=lambda **kw: kw, RRFRanker=lambda **kw: kw)
        retriever = Retriever(None, FakeEmbedder(), uri="http://milvus.test:19530",
                              rerank_enabled=False, timeout_s=2.5)
        with patch("app.retrieval.retriever.importlib.import_module", return_value=sdk):
            retriever.retrieve(["子问题1", "子问题2"], "原始问题")
        sdk.MilvusClient.assert_called_once_with(uri="http://milvus.test:19530", timeout=2.5)
        self.assertEqual([call["timeout"] for call in client.calls], [2.5, 2.5])
