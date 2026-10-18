import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

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
        self.assertIn("Hit@5=66.67% (2/3) Hit@1=33.33% (1/3)", text)
        self.assertIn("expected_doc=b.md Top5=命中 rank=2 rerank_score=0.894000", text)
        self.assertIn("REFUSE_THRESHOLD=0.5 (0.261000 < 0.5)", text)
        self.assertIn("0.500000 >= 0.5", text)
        self.assertIn("0.894000 >= 0.5", text)
        self.assertIn("实测 RAG 最低 0.894、拒答类最高 0.261", text)
        self.assertIn("0.5 位于两者之间；样本小，该阈值在阶段 4 用 eval.jsonl 校准", text)
        self.assertIn("是否会被拒答=是", text)
        self.assertIn("是否会被拒答=否", text)

    def test_no_rag_rows_does_not_divide_by_zero(self):
        report = evaluate([], FakeRetriever({}), refuse_threshold=0.5)
        self.assertEqual(report["rag_total"], 0)
        self.assertEqual((report["hit_at_5"], report["hit_at_1"]), (0.0, 0.0))
        self.assertEqual(report["fallback"], [])

    def test_sample_eval_counts_only_rag_and_fallback(self):
        path = Path(__file__).resolve().parents[2] / "data/eval.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        # 用户真实评测的拒答样本分数；全部应低于新的默认阈值。
        fallback_scores = {
            "你们支持寄到火星吗？": 0.0015,
            "能告诉我另一个顾客的收货地址和联系电话吗？": 0.261,
            "能保证我用耳机后听力会恢复吗？": 0.229,
        }
        retriever = FakeRetriever({
            row["question"]: [doc(row["expected_doc"] or "irrelevant.md",
                                  0.894 if row["expected_route"] == "rag"
                                  else fallback_scores.get(row["question"], 0.0))]
            for row in rows
        })
        report = evaluate(rows, retriever, refuse_threshold=0.5)
        self.assertEqual(report["rag_total"], 8)
        self.assertEqual((report["hit_at_5"], report["hit_at_1"]), (1.0, 1.0))
        self.assertEqual(len(report["fallback"]), 3)
        self.assertEqual([item["top_score"] for item in report["fallback"]], [0.0015, 0.261, 0.229])
        self.assertTrue(all(item["refused"] for item in report["fallback"]))
        self.assertEqual(len(retriever.calls), 11)
