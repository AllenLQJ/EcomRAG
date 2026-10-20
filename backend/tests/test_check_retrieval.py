import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from eval.check_retrieval import evaluate, print_report
from chat_fakes import document


def doc(source, score):
    return SimpleNamespace(source=source, score=score)


class FakeRetriever:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def retrieve(self, queries, rerank_query, top_k=5):
        self.calls.append((queries, rerank_query, top_k))
        return self.results[rerank_query]


class RetrievalCheckTests(unittest.TestCase):
    def test_multiple_expected_sources_keep_first_matching_rank_and_top5_boundary(self):
        rows = [dict(question=q, expected_route="rag", expected_doc=["original.md", "case.md"])
                for q in ("命中", "不相关", "第六名")]
        retriever = FakeRetriever({
            "命中": [doc("other.md", 0.99), doc("data/docs/case.md", 0.9), doc("original.md", 0.8)],
            "不相关": [doc("prefix-case.md", 0.9)],
            "第六名": [doc("other.md", 0.9)] * 5 + [doc("case.md", 0.8)],
        })
        report = evaluate(rows, retriever, refuse_threshold=None)
        self.assertEqual([row["rank"] for row in report["rag"]], [2, None, None])
        self.assertEqual(report["hit_at_5"], 1 / 3)
        self.assertEqual(report["hit_at_1"], 0)
        self.assertEqual(report["rag"][0]["expected_doc"], rows[0]["expected_doc"])

    def test_invalid_expected_source_fails_before_retrieval(self):
        for expected in ([], "", " ", 0, False, {}, ["a.md", 1], [["a.md"]], None):
            retriever = FakeRetriever({})
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "expected_doc"):
                evaluate([dict(question="无效", expected_route="rag", expected_doc=expected)],
                         retriever, refuse_threshold=None)
            self.assertEqual(retriever.calls, [])

    def test_summary_ranks_scores_top5_boundary_and_refusal_threshold(self):
        rows = [
            dict(question="第一", expected_route="rag", expected_doc="a.md"),
            dict(question="第二", expected_route="rag", expected_doc="b.md"),
            dict(question="未命中", expected_route="rag", expected_doc="c.md"),
            *[dict(question=q, expected_route="fallback", expected_doc=None)
              for q in ("低分", "边界", "高分", "空结果")],
            dict(question="跳过FAQ", expected_route="faq", expected_doc=None),
            dict(question="跳过闲聊", expected_route="general", expected_doc=None),
        ]
        retriever = FakeRetriever({
            "第一": [doc("a.md", 0.9), doc("a.md", 0.8)],
            "第二": [doc("a.md", 0.95), doc("data/docs/b.md", 0.894)],
            "未命中": [doc("other.md", 0.6)] * 5 + [doc("c.md", 0.1)],
            "低分": [doc("other.md", 0.261)], "边界": [doc("other.md", 0.5)],
            "高分": [doc("other.md", 0.894)], "空结果": [],
        })
        report = evaluate(rows, retriever, refuse_threshold=0.5)
        self.assertEqual(report["rag_total"], 3)
        self.assertAlmostEqual(report["hit_at_5"], 2 / 3)
        self.assertAlmostEqual(report["hit_at_1"], 1 / 3)
        self.assertEqual([item["rank"] for item in report["rag"]], [1, 2, None])
        self.assertEqual([item["score"] for item in report["rag"]], [0.9, 0.894, None])
        self.assertEqual([item["refused"] for item in report["fallback"]], [True, False, False, True])
        self.assertEqual([item["top_score"] for item in report["fallback"]], [0.261, 0.5, 0.894, None])
        self.assertEqual(len(retriever.calls), 7)
        self.assertTrue(all(queries == [question] and top_k == 5
                            for queries, question, top_k in retriever.calls))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_report(report)
        text = output.getvalue()
        for label in ("Hit@1", "Hit@5", "rerank_ms"):
            self.assertIn(label, text)
        for question in ("第一", "第二", "低分", "空结果"):
            self.assertIn(question, text)
        for line in (
            '[FALLBACK] 低分 top_score=0.261000 REFUSE_THRESHOLD=0.5 (0.261000 < 0.5) 是否会被拒答=是 rerank_ms=-',
            '[FALLBACK] 边界 top_score=0.500000 REFUSE_THRESHOLD=0.5 (0.500000 >= 0.5) 是否会被拒答=否 rerank_ms=-',
            '[FALLBACK] 空结果 top_score=无结果 REFUSE_THRESHOLD=0.5 (无检索结果) 是否会被拒答=是 rerank_ms=-',
            'RAG 最低分=0.894 / 拒答最高分=0.894 / 建议阈值=0.894（两者中点，仅供参考）',
        ):
            self.assertIn(line, text.splitlines())
        self.assertNotIn("云端分数", text)

    def test_remote_without_threshold_reports_scores_without_refusal_or_rewrite(self):
        from app.config import Settings

        rows = [dict(question="商品", rewritten_query="商品参数", expected_route="rag", expected_doc="a.md"),
                *[dict(question=q, rewritten_query=q + "改写", expected_route="fallback")
                  for q in ("高分", "低分", "空结果")]]
        results = {"商品": [doc("a.md", 1.0)], "高分": [doc("other.md", 1.0)],
                   "低分": [doc("other.md", 0.05)], "空结果": []}
        for docs in results.values():
            for item in docs:
                item.score_provider = "remote"

        class RemoteRetriever:
            def __init__(self):
                self.reranks = []
            def retrieve_candidates(self, queries):
                return results[queries[0]]
            async def rerank_candidates(self, question, candidates, top_k):
                self.reranks.append(question)
                return candidates[:top_k]

        settings = Settings(_env_file=None, rerank_provider="remote")
        self.assertIsNone(settings.rerank_refuse_threshold)
        for retriever in (RemoteRetriever(), FakeRetriever(results)):
            with self.subTest(retriever=type(retriever).__name__):
                with patch("eval.check_retrieval.needs_rewrite_rerank",
                           side_effect=AssertionError("未设阈值时不得触发改写补救比较")):
                    report = evaluate(rows, retriever, refuse_threshold=settings.rerank_refuse_threshold)
                self.assertEqual([item["top_score"] for item in report["fallback"]], [1.0, 0.05, None])
                self.assertEqual([item["refused"] for item in report["fallback"]], [None, None, None])
                self.assertEqual((report["rag_min_score"], report["fallback_max_score"],
                                  report["suggested_threshold"]), (1.0, 1.0, 1.0))
                self.assertEqual((report["hit_at_1"], report["hit_at_5"]), (1.0, 1.0))
                if isinstance(retriever, RemoteRetriever):
                    self.assertEqual(retriever.reranks, [row["question"] for row in rows])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    print_report(report)
                text = output.getvalue()
                self.assertEqual(text.count("云端分数不用于拒答（阈值未设）"), 3)
                for score in ("1.000000", "0.050000", "无结果"):
                    self.assertIn("top_score=" + score, text)
                self.assertNotIn("是否会被拒答", text)
                self.assertNotIn("REFUSE_THRESHOLD=", text)
                self.assertIn("RAG 最低分=1.0 / 拒答最高分=1.0 / 建议阈值=1.0", text)
                self.assertIn("云端分数无区分度时，该建议仅供参考", text)

    def test_no_rag_rows_does_not_divide_by_zero(self):
        report = evaluate([], FakeRetriever({}), refuse_threshold=0.5)
        self.assertEqual(report["rag_total"], 0)
        self.assertEqual((report["hit_at_5"], report["hit_at_1"]), (0.0, 0.0))
        self.assertEqual(report["fallback"], [])

    def test_optional_rewrites_rescue_low_scores_and_preserve_refusals_without_new_searches(self):
        class RewriteRetriever:
            def __init__(self):
                self.searches, self.reranks = [], []
            def retrieve_candidates(self, queries):
                self.searches.append(queries)
                return [document().model_copy(update={"source": "payment_invoices.md"})]
            def rerank_candidates(self, question, candidates, top_k):
                self.reranks.append(question)
                scores = {"发票怎么开": 0.14, "如何申请电子发票": 0.66,
                          "寄到火星": 0.1, "火星配送规则": 0.13,
                          "高分原问": 0.8, "另一个完整问法": 0.99}
                return [item.model_copy(update={"score": scores[question]}) for item in candidates[:top_k]]
        rows = [
            dict(question="发票怎么开", rewritten_query="如何申请电子发票",
                 expected_route="rag", expected_doc="payment_invoices.md"),
            dict(question="寄到火星", rewritten_query="火星配送规则", expected_route="fallback"),
            dict(question="高分原问", rewritten_query="另一个完整问法",
                 expected_route="rag", expected_doc="payment_invoices.md"),
        ]
        retriever = RewriteRetriever()
        report = evaluate(rows, retriever, refuse_threshold=0.5)
        self.assertEqual(report["rag"][0]["score"], 0.66)
        self.assertEqual(report["rag"][0]["rank"], 1)
        self.assertEqual(report["fallback"][0]["top_score"], 0.13)
        self.assertTrue(report["fallback"][0]["refused"])
        self.assertEqual(report["rag"][1]["score"], 0.8)
        self.assertEqual(len(retriever.searches), 3)
        self.assertEqual(retriever.reranks, ["发票怎么开", "如何申请电子发票", "寄到火星", "火星配送规则", "高分原问"])

    def test_split_mode_comparison_measures_only_rerank_and_summarizes_each_mode(self):
        rows = [dict(question="商品", expected_route="rag", expected_doc="product.md"),
                dict(question="不支持", expected_route="fallback", expected_doc=None)]
        class SplitFake:
            candidate_limit = 6
            def __init__(self, mode):
                self.rerank_text_mode = mode
                self.calls = []
            def retrieve_candidates(self, queries):
                self.calls.append(("retrieve", queries))
                return queries
            def rerank_candidates(self, question, candidates, top_k):
                self.calls.append(("rerank", question, candidates, top_k))
                if question == "不支持":
                    return [doc("irrelevant.md", 0.2)]
                expected = doc("product.md", 0.8)
                return [expected] if self.rerank_text_mode == "child" else [doc("other.md", 0.9), expected]
        for mode, hit1, rank in (("child", 1, 1), ("parent", 0, 2)):
            retriever = SplitFake(mode)
            with patch("eval.check_retrieval.perf_counter", side_effect=[0, 0.002, 1, 1.004]):
                report = evaluate(rows, retriever, refuse_threshold=0.5)
            self.assertEqual((report["text_mode"], report["candidates"]), (mode, 6))
            self.assertEqual((report["hit_at_1"], report["hit_at_5"]), (hit1, 1))
            self.assertEqual((report["rag"][0]["rank"], report["rag"][0]["score"]), (rank, 0.8))
            self.assertAlmostEqual(report["rag"][0]["rerank_ms"], 2)
            self.assertAlmostEqual(report["fallback"][0]["rerank_ms"], 4)
            self.assertEqual(report["fallback"][0]["top_score"], 0.2)
            self.assertEqual([call[0] for call in retriever.calls], ["retrieve", "rerank"] * 2)

    def test_small_rewrite_reranks_but_normalized_identical_query_does_not(self):
        question = "支持哪些付款方式"
        for rewritten, expected_score, expected_queries in (
            ("店铺支持哪些付款方式？", 0.688, [question, "店铺支持哪些付款方式？"]),
            (" 支持 哪些付款方式？！ ", 0.083, [question]),
        ):
            with self.subTest(rewritten=rewritten):
                class RewriteRetriever:
                    def __init__(self):
                        self.searches, self.reranks = [], []
                        self.candidates = [document().model_copy(update={"source": "payment_methods.md"})]
                    def retrieve_candidates(self, queries):
                        self.searches.append(queries)
                        return self.candidates
                    def rerank_candidates(self, query, candidates, top_k):
                        assert candidates is self.candidates
                        self.reranks.append(query)
                        score = 0.083 if query == question else 0.688
                        return [item.model_copy(update={"score": score}) for item in candidates[:top_k]]
                retriever = RewriteRetriever()
                rows = [dict(question=question, rewritten_query=rewritten,
                             expected_route="rag", expected_doc="payment_methods.md")]
                report = evaluate(rows, retriever, refuse_threshold=0.2)
                self.assertEqual(retriever.searches, [[question]])
                self.assertEqual(retriever.reranks, expected_queries)
                self.assertEqual(report["rag"][0]["score"], expected_score)
                self.assertEqual(report["rag"][0]["rank"], 1)
                self.assertEqual((report["hit_at_1"], report["hit_at_5"]), (1, 1))
