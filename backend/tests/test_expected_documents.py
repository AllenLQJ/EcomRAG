"""来源标注与冒烟判定的离线边界；只用标准库，不读取配置。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ingest.eval_expectations import expected_documents, matches_expected_document
from ingest.kb_common import smoke_check


INVALID_EXPECTED = ([], "", "  ", 0, False, {}, ("a.md",), ["a.md", 1],
                    [None], [["a.md"]], ["", " \t "])


class ExpectedDocumentTests(unittest.TestCase):
    def test_single_list_and_absent_reference(self):
        self.assertEqual(expected_documents("a.md"), ("a.md",))
        self.assertEqual(expected_documents(["a.md", "case.md"]), ("a.md", "case.md"))
        self.assertEqual(expected_documents(None), ())
        with self.assertRaisesRegex(ValueError, "expected_doc"):
            expected_documents(None, required=True)

    def test_invalid_reference_is_never_silently_skipped(self):
        for value in INVALID_EXPECTED:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "expected_doc"):
                expected_documents(value)

    def test_normalization_strips_drops_empty_and_deduplicates_in_order(self):
        self.assertEqual(expected_documents(" \ta.md\n"), ("a.md",))
        self.assertEqual(expected_documents([" ", " b.md ", "a.md", "b.md", "", " a.md ", "c.md"]),
                         ("b.md", "a.md", "c.md"))

    def test_shared_matcher_accepts_paths_filenames_and_ids(self):
        for source, doc_id, expected, hit in (
            ("data/docs/a.md", "id", "data/docs/a.md", True),
            ("data/docs/a.md", "id", "a.md", True),
            ("a.md", "id", "data/docs/a.md", True),
            ("other/a.md", "id", "data/docs/a.md", True),
            ("unrelated.md", "id", "id", True),
            ("prefix-a.md", "id", "a.md", False),
            ("unrelated.md", "prefix-id", "id", False),
        ):
            with self.subTest(source=source, doc_id=doc_id, expected=expected):
                self.assertEqual(matches_expected_document(SimpleNamespace(source=source, doc_id=doc_id),
                                                          expected_documents(expected)), hit)

    def test_smoke_and_retrieval_share_normalized_source_and_id_matching(self):
        from eval.check_retrieval import evaluate

        cases = [
            ("data/docs/a.md", "id", " data/docs/a.md ", True),
            ("data/docs/a.md", "id", " a.md ", True),
            ("a.md", "id", ["", " data/docs/a.md ", "data/docs/a.md"], True),
            ("unrelated.md", "doc-id", ["original.md", " doc-id "], True),
            ("prefix-a.md", "prefix-id", ["a.md", "id"], False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            for source, doc_id, expected, hit in cases:
                with self.subTest(source=source, expected=expected):
                    row = dict(question="事实", expected_route="rag", answerable=True, expected_doc=expected)
                    path.write_text(json.dumps(row), encoding="utf-8")
                    doc = SimpleNamespace(source=source, doc_id=doc_id, score=0.9)
                    retriever = SimpleNamespace(retrieve=lambda *a, **kw: [doc])
                    retrieval = evaluate([row], retriever, refuse_threshold=None)
                    smoke = smoke_check("kb_chunks_v1", None, eval_file=str(path), retriever=retriever)
                    self.assertEqual(retrieval["hit_at_5"], float(hit))
                    self.assertEqual(smoke["hit5"], retrieval["hit_at_5"])
                    self.assertEqual(retrieval["rag"][0]["rank"], 1 if hit else None)

    def test_smoke_accepts_source_filename_or_id_but_never_sixth_result(self):
        cases = [
            ("original.md", "original.md", True),
            (["original.md", "case.md"], "case.md", True),
            (["original.md", "case.md"], "original.md", True),
            (["original.md", "case.md"], "other.md", False),
            (["original.md", "case.md"], "prefix-case.md", False),
            (["original.md", "case.md"], "subdir/case.md", True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            for expected, source, hit in cases:
                for by_id in (False, True):
                    if by_id and source == "subdir/case.md":
                        continue  # doc_id 只做全等比较，不比较其路径文件名。
                    with self.subTest(expected=expected, source=source, by_id=by_id):
                        path.write_text(json.dumps({"question": "事实", "answerable": True,
                                                    "expected_doc": expected}), encoding="utf-8")
                        doc = SimpleNamespace(source="unrelated" if by_id else source,
                                              doc_id=source if by_id else "unrelated")
                        retriever = SimpleNamespace(retrieve=lambda *a, **kw: [doc])
                        report = smoke_check("kb_chunks_v1", None, eval_file=str(path), retriever=retriever)
                        self.assertEqual(report["results"][0]["hit5"], hit)
                        self.assertEqual(report["results"][0]["any_hit"], hit)
                        primary_hit = hit and source == "original.md"
                        self.assertEqual(report["results"][0]["primary_hit"], primary_hit)
                        self.assertEqual(report["primary_hit5"], float(primary_hit))
                        self.assertEqual(report["results"][0]["expected_doc"], expected)
                        retriever.retrieve = lambda *a, **kw: [SimpleNamespace(source="other", doc_id="other")] * 5 + [doc]
                        report = smoke_check("kb_chunks_v1", None, eval_file=str(path), retriever=retriever)
                        self.assertEqual((report["hit5"], report["primary_hit5"]), (0, 0))

    def test_smoke_reports_primary_and_any_rates_with_same_denominator(self):
        rows = [dict(question=question, answerable=True,
                     expected_doc=["", " original.md ", "case.md", "original.md"])
                for question in ("主文档第五名", "仅案例", "未命中", "无结果")]
        other = SimpleNamespace(source="other.md", doc_id="other")
        retrieved = {
            "主文档第五名": [other] * 4 + [SimpleNamespace(source="docs/original.md", doc_id="original")],
            "仅案例": [SimpleNamespace(source="docs/case.md", doc_id="case")],
            "未命中": [other], "无结果": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            retriever = SimpleNamespace(retrieve=lambda queries, question, **kw: retrieved[question])
            report = smoke_check("kb_chunks_v1", None, eval_file=str(path), retriever=retriever)
        self.assertEqual((report["queries"], report["hit5"], report["primary_hit5"]), (4, 0.5, 0.25))
        self.assertEqual([row["any_hit"] for row in report["results"]], [True, True, False, False])
        self.assertEqual([row["primary_hit"] for row in report["results"]], [True, False, False, False])

    def test_smoke_validates_even_unsampled_or_unanswerable_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            for invalid in INVALID_EXPECTED:
                if isinstance(invalid, tuple):
                    continue  # JSON 数组加载后是合法 list；tuple 仅用于 Python 接口测试。
                path.write_text("\n".join(json.dumps(row) for row in [
                    {"question": "有效", "answerable": True, "expected_doc": "a.md"},
                    {"question": "无效", "answerable": False, "expected_doc": invalid},
                ]), encoding="utf-8")
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "expected_doc"):
                    smoke_check("kb_chunks_v1", None, eval_file=str(path), limit=1, retriever=object())


if __name__ == "__main__":
    unittest.main()
