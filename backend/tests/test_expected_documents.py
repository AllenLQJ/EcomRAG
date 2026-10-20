"""来源标注与冒烟判定的离线边界；只用标准库，不读取配置。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ingest.eval_expectations import expected_documents
from ingest.kb_common import smoke_check


INVALID_EXPECTED = ([], "", "  ", 0, False, {}, ("a.md",), ["a.md", 1],
                    [None], [["a.md"]], ["a.md", " "])


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

    def test_smoke_accepts_any_exact_source_or_id_but_never_sixth_result(self):
        cases = [
            ("original.md", "original.md", True),
            (["original.md", "case.md"], "case.md", True),
            (["original.md", "case.md"], "original.md", True),
            (["original.md", "case.md"], "other.md", False),
            (["original.md", "case.md"], "prefix-case.md", False),
            (["original.md", "case.md"], "subdir/case.md", False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            for expected, source, hit in cases:
                for by_id in (False, True):
                    with self.subTest(expected=expected, source=source, by_id=by_id):
                        path.write_text(json.dumps({"question": "事实", "answerable": True,
                                                    "expected_doc": expected}), encoding="utf-8")
                        doc = SimpleNamespace(source="unrelated" if by_id else source,
                                              doc_id=source if by_id else "unrelated")
                        retriever = SimpleNamespace(retrieve=lambda *a, **kw: [doc])
                        report = smoke_check("kb_chunks_v1", None, eval_file=str(path), retriever=retriever)
                        self.assertEqual(report["results"][0]["hit5"], hit)
                        self.assertEqual(report["results"][0]["expected_doc"], expected)
                        retriever.retrieve = lambda *a, **kw: [SimpleNamespace(source="other", doc_id="other")] * 5 + [doc]
                        self.assertEqual(smoke_check("kb_chunks_v1", None, eval_file=str(path),
                                                     retriever=retriever)["hit5"], 0)

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
