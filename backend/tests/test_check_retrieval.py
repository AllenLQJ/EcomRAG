import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from eval.check_retrieval import evaluate, print_report


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

    def test_no_rag_rows_does_not_divide_by_zero(self):
        report = evaluate([], FakeRetriever({}), refuse_threshold=0.5)
        self.assertEqual(report["rag_total"], 0)
        self.assertEqual((report["hit_at_5"], report["hit_at_1"]), (0.0, 0.0))
        self.assertEqual(report["fallback"], [])

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
