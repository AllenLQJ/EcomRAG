import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from app.faq.bm25 import Bm25Index
from app.faq.cache import FaqCache, cache_key, normalize
from app.faq.repo import FaqRecord, FaqRepo
from ingest.load_faq import read_faq
from faq_fakes import FakeBM25, FakeRepo

ROOT = Path(__file__).resolve().parents[2]


class FaqIndexTests(unittest.TestCase):
    def test_normalize_and_md5_key(self):
        variants = [" ＡＢＣ退货？　", "abc退货", "\n!! AbC退货 。？ \t", "「ABC退货」"]
        expected = "faq:cache:" + hashlib.md5("abc退货".encode()).hexdigest()
        for text in variants:
            with self.subTest(text=text):
                self.assertEqual(normalize(text), "abc退货")
                self.assertEqual(cache_key(text), expected)
        self.assertEqual(normalize("　Ａ Ｂ，Ｃ　"), "a b,c")
        self.assertEqual(normalize("　？!\t"), "")
        self.assertNotEqual(cache_key("a b"), cache_key("ab"))

    def test_redis_get_set_exceptions_are_logged_and_ignored(self):
        hit = Mock(model_dump_json=Mock(return_value='{"faq_id":1}'))
        for error in (ConnectionError, TimeoutError, OSError):
            client = Mock()
            client.get.side_effect = error("fake")
            client.set.side_effect = error("fake")
            cache = FaqCache(client)
            with self.subTest(error=error), self.assertLogs("app.faq.cache", "WARNING") as logs:
                self.assertIsNone(cache.get("问题"))
                self.assertIsNone(cache.set("问题", hit))
            self.assertEqual(len(logs.output), 2)

    def test_redis_lazy_creation_failure_is_also_ignored(self):
        hit = Mock(model_dump_json=Mock(return_value='{"faq_id":1}'))
        for error in (ImportError, ConnectionError, TimeoutError, OSError):
            with self.subTest(error=error), patch("app.faq.cache.importlib.import_module", side_effect=error):
                cache = FaqCache()
                with self.assertLogs("app.faq.cache", "WARNING"):
                    self.assertIsNone(cache.get("问题"))
                    self.assertIsNone(cache.set("问题", hit))

    def test_redis_sdk_errors_are_logged_and_ignored(self):
        class FakeRedisError(Exception):
            pass

        client = Mock()
        client.get.side_effect = FakeRedisError("fake Redis read error")
        client.set.side_effect = FakeRedisError("fake Redis write error")
        driver = SimpleNamespace(exceptions=SimpleNamespace(RedisError=FakeRedisError),
                                 Redis=Mock())
        driver.Redis.from_url.return_value = client
        hit = Mock(model_dump_json=Mock(return_value='{"faq_id":1}'))
        for injected in (False, True):
            cache = FaqCache(client if injected else None)
            with self.subTest(injected=injected), \
                    patch("app.faq.cache.importlib.import_module", return_value=driver), \
                    self.assertLogs("app.faq.cache", "WARNING") as logs:
                self.assertIsNone(cache.get("问题"))
                self.assertIsNone(cache.set("问题", hit))
            self.assertEqual(len(logs.output), 2)

    def test_non_redis_errors_are_propagated(self):
        hit = Mock(model_dump_json=Mock(return_value='{"faq_id":1}'))
        for error in (TypeError, RuntimeError, AttributeError):
            client = Mock()
            client.get.side_effect = error("fake programming error")
            client.set.side_effect = error("fake programming error")
            cache = FaqCache(client)
            with self.subTest(error=error):
                with self.assertRaises(error):
                    cache.get("问题")
                with self.assertRaises(error):
                    cache.set("问题", hit)
            with patch("app.faq.cache.importlib.import_module", side_effect=error):
                with self.assertRaises(error):
                    FaqCache().get("问题")
                with self.assertRaises(error):
                    FaqCache().set("问题", hit)

    def test_serialization_bugs_are_propagated_before_redis_access(self):
        for error in (TypeError, ValueError, OSError):
            client = Mock()
            hit = Mock()
            hit.model_dump_json.side_effect = error("fake serialization bug")
            with self.subTest(error=error), self.assertRaises(error):
                FaqCache(client).set("问题", hit)
            client.set.assert_not_called()

    def test_repo_binds_enabled_and_preserves_injected_connection(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(1, "问题", "答案", "product")]
        self.assertEqual(FaqRepo(connection).load_enabled(), [FaqRecord(1, "问题", "答案", "product")])
        sql, params = cursor.execute.call_args.args
        self.assertIn("WHERE enabled=%s", sql)
        self.assertEqual(params, (1,))
        connection.close.assert_not_called()

    def test_repo_factory_connections_close_on_success_and_failure(self):
        for failed in (False, True):
            connection = MagicMock()
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [dict(id=2, question="Q", answer="A", category=None)]
            repo = FaqRepo(connection_factory=lambda: connection)
            if failed:
                cursor.execute.side_effect = RuntimeError("fake query failure")
                with self.assertRaises(RuntimeError):
                    repo.load_enabled()
            else:
                self.assertEqual(repo.load_enabled(), [FaqRecord(2, "Q", "A")])
            connection.close.assert_called_once_with()

    def test_csv_index_ranks_return_question_first_and_reload(self):
        rows = read_faq(ROOT / "data/faq.csv")
        records = [FaqRecord(i, row.question, row.answer, row.category) for i, row in enumerate(rows, 1)]
        repo = FakeRepo(records)
        index = Bm25Index(repo, tokenizer=list, index_factory=FakeBM25)
        hits = index.top_k("怎么申请退货？")
        self.assertEqual(len(hits), 5)
        self.assertEqual(hits[0][0], 1)
        self.assertGreater(hits[0][1], hits[1][1])
        self.assertEqual(index.top_k("问题", k=0), [])
        repo.records = [FaqRecord(99, "怎么申请退货？", "新答案")]
        index.reload()
        self.assertEqual(repo.loads, 2)
        self.assertEqual(index.top_k("怎么申请退货？")[0][0], 99)
        self.assertEqual(index.records[99].answer, "新答案")
        repo.records = []
        index.reload()
        self.assertEqual(index.top_k("问题"), [])

    def test_default_factories_are_lazy_and_use_question_tokenization(self):
        repo = FakeRepo([FaqRecord(1, "ABC？", "答案")])
        jieba = SimpleNamespace(lcut=Mock(side_effect=list))
        rank = SimpleNamespace(BM25Okapi=Mock(side_effect=FakeBM25))
        with patch("app.faq.bm25.importlib.import_module", side_effect=lambda name: {"jieba": jieba, "rank_bm25": rank}[name]):
            index = Bm25Index(repo)
            self.assertEqual(index.top_k("ＡＢＣ！")[0][0], 1)
        rank.BM25Okapi.assert_called_once_with([["a", "b", "c"]])
        self.assertEqual(jieba.lcut.call_args_list[0].args, ("abc",))
