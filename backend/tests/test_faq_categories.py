import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ingest.load_faq import read_faq
from ingest.loaders import CATEGORIES, LoaderError, load_document
from ingest.migrate_faq_categories import ensure_faq_categories_schema, main


class FakeConnection:
    def __init__(self, kind="enum('after_sales','logistics','payment','product','promotion')"):
        self.kind = kind
        self.sql = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.sql.append(sql)
        if sql.startswith("ALTER"):
            self.kind = "varchar(100)"

    def fetchone(self):
        return ("category", self.kind) if self.kind else None

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FaqCategoryTests(unittest.TestCase):
    def test_old_five_and_new_two_categories_load_and_validate(self):
        expected = {"after_sales", "logistics", "payment", "product", "promotion", "account", "store"}
        self.assertEqual(CATEGORIES, expected)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for category in sorted(expected):
                path = folder / (category + "_note.md")
                path.write_text("# 分类标题\n\n说明内容", encoding="utf-8")
                self.assertEqual(load_document(path).category, category)
            path = folder / "account_security_password.md"
            path.write_text("# 账号安全\n说明内容", encoding="utf-8")
            self.assertEqual(load_document(path).category, "account")
            with self.assertRaises(LoaderError):
                load_document(path, metadata={"category": "invalid"})
            faq = folder / "faq.csv"
            with faq.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(("question", "answer", "category"))
                writer.writerows((category + "问题", "答案", category) for category in sorted(expected))
            self.assertEqual({row.category for row in read_faq(faq)}, expected)
            faq.write_text("question,answer,category\n问题,答案,invalid\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "分类不合法"):
                read_faq(faq)

    def test_enum_migration_is_idempotent_and_varchar_does_not_alter(self):
        connection = FakeConnection()
        self.assertTrue(ensure_faq_categories_schema(connection))
        self.assertFalse(ensure_faq_categories_schema(connection))
        self.assertEqual(sum(sql.startswith("ALTER") for sql in connection.sql), 1)
        self.assertIn("VARCHAR(100)", connection.sql[1])
        self.assertEqual(connection.commits, 2)
        self.assertFalse(ensure_faq_categories_schema(FakeConnection("varchar(100)")))
        with self.assertRaisesRegex(ValueError, "不存在"):
            ensure_faq_categories_schema(FakeConnection(None))

    def test_migration_injected_connection_does_not_load_settings_or_connect(self):
        connection = FakeConnection()
        with patch("socket.socket", side_effect=AssertionError("禁止网络")), redirect_stdout(io.StringIO()):
            self.assertEqual(main([], connection_factory=lambda: connection), 0)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
