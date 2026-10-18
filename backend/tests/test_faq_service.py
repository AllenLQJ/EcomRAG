import json
import unittest
from unittest.mock import Mock, patch

from app.contracts import FaqHit
from app.faq.cache import FaqCache, cache_key
from app.faq.repo import FaqRecord
from app.faq.service import FaqService, cosine
from app.retrieval.embedder import EmbeddingBatch, FakeEmbedder
from faq_fakes import FakeBM25, FakeRedis, FakeRepo


class ControlledFakeEmbedder(FakeEmbedder):
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return EmbeddingBatch([self.vectors[text] for text in texts], [{1: 1.0} for _ in texts])


class FaqServiceTests(unittest.TestCase):
    def build(self, query_vector, *, threshold=0.8):
        repo = FakeRepo([FaqRecord(1, "退货申请", "答案", "after_sales")])
        redis = FakeRedis()
        cache = FaqCache(redis)
        embedder = ControlledFakeEmbedder({"退货申请": [2.0, 0.0], "退货问题": query_vector})
        service = FaqService(repo, cache, embedder, threshold=threshold,
                             tokenizer=list, index_factory=FakeBM25)
        return service, embedder, redis

    def test_cache_round_trip_json_ttl_and_source(self):
        redis = FakeRedis()
        cache = FaqCache(redis)
        hit = FaqHit(faq_id=1, question="退货申请", answer="答案", score=0.9, category="after_sales")
        cache.set(" Ｑ？ ", hit)
        key, raw, ttl = redis.writes[0]
        self.assertEqual(key, cache_key("q"))
        self.assertEqual(ttl, 86400)
        self.assertEqual(json.loads(raw), hit.model_dump())
        result = cache.get("q!")
        self.assertEqual(result.source, "cache")
        self.assertEqual(result.category, "after_sales")
        self.assertEqual(result.model_dump(exclude={"source"}), hit.model_dump(exclude={"source"}))
        self.assertEqual(result.top_score, 0.9)
        self.assertEqual(result.id, result.faq_id)
        custom = FaqCache(redis, ttl_s=60)
        custom.set("q", hit)
        self.assertEqual(redis.writes[-1][2], 60)

    def test_corrupt_cached_json_is_a_miss(self):
        redis = FakeRedis()
        for raw in ("invalid", "null", "[]", "{}", '{"faq_id":1,"question":"Q"}',
                    '{"faq_id":1,"question":"Q","answer":"A","score":"invalid"}'):
            redis.values[cache_key("问题")] = raw
            with self.assertLogs("app.faq.cache", "WARNING"):
                self.assertIsNone(FaqCache(redis).get("问题"))

    def test_cache_validation_programming_errors_are_propagated(self):
        redis = FakeRedis()
        redis.values[cache_key("问题")] = '{"faq_id":1}'
        with patch.object(FaqHit, "model_validate_json", side_effect=TypeError("fake validation bug")):
            with self.assertRaisesRegex(TypeError, "fake validation bug"):
                FaqCache(redis).get("问题")

    def test_cache_hit_skips_bm25_and_query_encoding(self):
        service, embedder, redis = self.build([0.0, 1.0])
        service.cache.set("退货问题", FaqHit(faq_id=8, question="缓存问题", answer="缓存答案", score=0.95))
        index, _ = service._snapshot
        index.top_k = Mock(side_effect=AssertionError("缓存命中不得召回"))
        hit = service.faq_lookup("退货问题")
        self.assertEqual((hit.faq_id, hit.source, service.top_score), (8, "cache", 0.95))
        self.assertEqual(embedder.calls, [["退货申请"]])
        self.assertEqual(len(redis.writes), 1)

    def test_threshold_below_equal_and_above_and_cache_write(self):
        for vector, expected in (([0.79, 0.613106842], False), ([0.8, 0.6], True), ([4.0, 0.0], True)):
            with self.subTest(vector=vector):
                service, embedder, redis = self.build(vector)
                index, _ = service._snapshot
                index.top_k = Mock(wraps=index.top_k)
                hit = service.faq_lookup("退货问题")
                index.top_k.assert_called_once_with("退货问题", k=5)
                self.assertEqual(hit is not None, expected)
                self.assertAlmostEqual(service.top_score, cosine(vector, [2.0, 0.0]))
                self.assertEqual(len(redis.writes), int(expected))
                self.assertEqual(embedder.calls, [["退货申请"], ["退货问题"]])
                if expected:
                    self.assertEqual((hit.source, hit.answer), ("faq", "答案"))
                    self.assertEqual(hit.top_score, service.top_score)

    def test_best_dense_score_wins_over_bm25_order(self):
        repo = FakeRepo([FaqRecord(1, "退货问题", "字面匹配"), FaqRecord(2, "退货", "语义匹配")])
        embedder = ControlledFakeEmbedder({"退货问题": [0, 1], "退货": [1, 0], "退货提问": [1, 0]})
        service = FaqService(repo, FaqCache(FakeRedis()), embedder, tokenizer=list, index_factory=FakeBM25)
        self.assertEqual(service.faq_lookup("退货提问").faq_id, 2)

    def test_reload_refreshes_vectors_and_empty_index_resets_trace(self):
        service, embedder, _ = self.build([0.0, 1.0])
        self.assertIsNone(service.faq_lookup("退货问题"))
        service.repo.records = [FaqRecord(2, "退货问题", "新答案")]
        service.reload()
        self.assertEqual(embedder.calls[-1], ["退货问题"])
        self.assertEqual(service.faq_lookup("退货问题").faq_id, 2)
        service.repo.records = []
        service.reload()
        self.assertIsNone(service.faq_lookup("无缓存的新问题"))
        self.assertIsNone(service.top_score)

    def test_zero_vector_is_not_a_match(self):
        service, _, _ = self.build([0, 0])
        self.assertIsNone(service.faq_lookup("退货问题"))
        self.assertEqual(service.top_score, 0.0)
