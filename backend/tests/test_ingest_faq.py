import io
import unittest
from pathlib import Path
from unittest.mock import patch

from ingest.load_faq import FaqRow, read_faq, upsert_faq

ROOT = Path(__file__).resolve().parents[2]


class FakeConnection:
    def __init__(self):
        self.rows = {}
        self.commits = 0
        self.rollbacks = 0
        self.locked = False
        self.last = None
        self.fail_write = False
        self.lock_available = True
        self.executions = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.executions.append((sql, params))
        if "GET_LOCK" in sql:
            self.locked = self.lock_available
            self.last = (int(self.lock_available),)
        elif "RELEASE_LOCK" in sql:
            self.locked = False
        elif sql.startswith("SELECT id"):
            self.last = (1,) if params[0] in self.rows else None
        elif sql.startswith("INSERT"):
            if self.fail_write:
                raise RuntimeError("模拟写入失败")
            question, answer, category = params
            self.rows[question] = (answer, category)
        elif sql.startswith("UPDATE"):
            answer, category, question = params
            self.rows[question] = (answer, category)
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.last

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FaqImportTests(unittest.TestCase):
    def test_reimport_is_idempotent_and_updates_answer(self):
        rows = read_faq(ROOT / "data/faq.csv")
        connection = FakeConnection()
        self.assertEqual(upsert_faq(connection, rows), 37)
        self.assertEqual(upsert_faq(connection, rows), 37)
        self.assertEqual(len(connection.rows), 37)
        upsert_faq(connection, [FaqRow("怎么申请退货？", "更新后的虚构样例答案", "after_sales")])
        self.assertEqual(connection.rows["怎么申请退货？"][0], "更新后的虚构样例答案")
        self.assertFalse(connection.locked)

    def test_failure_rolls_back_and_releases_lock(self):
        connection = FakeConnection()
        connection.fail_write = True
        with self.assertRaisesRegex(RuntimeError, "模拟写入失败"):
            upsert_faq(connection, [FaqRow("测试问题", "测试答案", "product")])
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)
        self.assertFalse(connection.locked)

    def test_sql_values_are_always_bound_parameters(self):
        connection = FakeConnection()
        question = "问题'); DROP TABLE faq_pairs; --"
        row = FaqRow(question, "答案'也不是 SQL", "product")
        upsert_faq(connection, [row])
        upsert_faq(connection, [row])
        self.assertEqual(len(connection.rows), 1)
        for sql, params in connection.executions:
            self.assertNotIn(question, sql)
            self.assertNotIn(row.answer, sql)
            self.assertEqual(sql.count("%s"), len(params))
        self.assertTrue(any(sql.startswith("INSERT") and params == (question, row.answer, "product")
                            for sql, params in connection.executions))
        self.assertTrue(any(sql.startswith("UPDATE") and params == (row.answer, "product", question)
                            for sql, params in connection.executions))

    def test_lock_failure_does_not_write_or_release_unowned_lock(self):
        connection = FakeConnection()
        connection.lock_available = False
        with self.assertRaisesRegex(RuntimeError, "无法获取 FAQ 导入锁"):
            upsert_faq(connection, [FaqRow("问题", "答案", "product")])
        self.assertEqual(connection.rows, {})
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(any("RELEASE_LOCK" in sql for sql, _ in connection.executions))

    def test_csv_validation_and_duplicate_question(self):
        content = "question,answer,category\n问题,旧答案,product\n问题,新答案,product\n"
        with patch("builtins.open", return_value=io.StringIO(content)):
            self.assertEqual(read_faq("faq.csv"), [FaqRow("问题", "新答案", "product")])
        for content in ("answer,question,category\n", "question,answer,category\n问题,答案,invalid\n",
                        "question,answer,category\n问题,,product\n", "question,answer,category\n问题,答案\n"):
            with patch("builtins.open", return_value=io.StringIO(content)):
                with self.subTest(content=content), self.assertRaises(ValueError):
                    read_faq("faq.csv")
