import unittest
from pathlib import Path
from unittest.mock import patch

from ingest.loaders import LoaderError, load_directory, load_document

FIXTURES = Path(__file__).parent / "fixtures"


class LoaderTests(unittest.TestCase):
    def test_md_txt_and_stable_identity(self):
        first = load_directory(FIXTURES)
        second = load_directory(FIXTURES)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0].title, "售后标题")
        self.assertEqual(first[0].category, "after_sales")
        self.assertEqual(first[1].category, "logistics")
        self.assertEqual(first[1].text.strip(), "配送说明。")
        mapped = load_document(FIXTURES / "logistics_note.txt", metadata={"category": "payment", "title": "账单"})
        self.assertEqual((mapped.title, mapped.category), ("账单", "payment"))

    def test_missing_optional_pdf_docx_dependencies(self):
        for suffix, expected in [(".pdf", "PyMuPDF"), (".docx", "python-docx")]:
            with self.subTest(suffix=suffix):
                with patch("ingest.loaders.importlib.import_module", side_effect=ModuleNotFoundError("模拟缺包")) as load:
                    with self.assertRaisesRegex(LoaderError, expected):
                        load_document("not-opened" + suffix)
                    load.assert_called_once()

    def test_empty_unsupported_and_unknown_category(self):
        with patch.object(Path, "read_text", return_value=" "):
            with self.assertRaisesRegex(LoaderError, "没有可读取文本"):
                load_document("empty.md")
        with self.assertRaisesRegex(LoaderError, "未知文档分类"):
            load_document(FIXTURES / "logistics_note.txt", metadata={"category": "unknown"})
        with self.assertRaisesRegex(LoaderError, "不支持"):
            load_document("demo.zip")
