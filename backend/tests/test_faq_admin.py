"""发布 CLI 的离线替身测试，不读取配置文件、不连接服务。"""
import contextlib
import copy
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.faq.cache import FaqCache
from app.faq.version import FaqVersion, VERSION_KEY, publish
from faq_fakes import FakeRedis
from ingest import faq_reload, faq_restore, load_faq
from ingest.faq_admin import backup_state, import_rows, read_backup, restore_rows
from ingest.load_faq import FaqRow


class AdminRedis(FakeRedis):
    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]


class AdminConnection:
    def __init__(self):
        self.rows = [dict(id=1, question="问题", answer="原答案", category="product", enabled=1,
                          updated_at="2026-10-19 12:00:00.123"),
                     dict(id=2, question="已停用", answer="停用答案", category="payment", enabled=0,
                          updated_at="2026-10-18 12:00:00.000")]
        self.before = copy.deepcopy(self.rows)
        self.executions = []
        self.commits = self.rollbacks = 0
        self.last = None
        self.fail_write = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.executions.append((sql, params))
        if sql.startswith("SELECT GET_LOCK"):
            self.last = (1,)
        elif sql.startswith("SELECT RELEASE_LOCK"):
            pass
        elif sql.startswith("SELECT id, question"):
            self.result = copy.deepcopy(self.rows)
        elif sql.startswith("SELECT id FROM"):
            self.last = next(((row["id"],) for row in self.rows if row["question"] == params[0]), None)
        elif self.fail_write:
            raise RuntimeError("PRIVATE database failure")
        elif sql.startswith("UPDATE"):
            for row in self.rows:
                if row["question"] == params[2]:
                    row.update(answer=params[0], category=params[1])
        elif sql == "DELETE FROM faq_pairs":
            self.rows.clear()
        elif sql.startswith("DELETE"):
            self.rows = [row for row in self.rows if row["question"] != params[0]]
        elif sql.startswith("INSERT INTO faq_pairs (id,"):
            from ingest.faq_admin import BACKUP_FIELDS
            self.rows.append(dict(zip(BACKUP_FIELDS, params)))
        elif sql.startswith("INSERT"):
            self.rows.append(dict(id=max((r["id"] for r in self.rows), default=0) + 1,
                                  question=params[0], answer=params[1], category=params[2], enabled=1,
                                  updated_at="2026-10-19 13:00:00.000"))
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.last

    def fetchall(self):
        return self.result

    def commit(self):
        self.commits += 1
        self.before = copy.deepcopy(self.rows)

    def rollback(self):
        self.rollbacks += 1
        self.rows = copy.deepcopy(self.before)


class FaqAdminTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.csv = self.directory / "input.csv"
        self.csv.write_text("question,answer,category\n问题,新答案,product\n新增,答案,payment\n", encoding="utf-8")
        self.connection = AdminConnection()
        self.redis = AdminRedis()
        self.settings = SimpleNamespace(redis_url="redis://fake")
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_dry_run_only_reads_and_reports_differences(self):
        original = copy.deepcopy(self.connection.rows)
        with patch("ingest.faq_admin.notify") as notify, patch("ingest.faq_admin.backup_state") as backup:
            code = load_faq.main([str(self.csv), "--dry-run", "--prune"], settings=self.settings,
                                 connection=self.connection, client=self.redis)
        self.assertEqual(code, 0)
        self.assertEqual(self.connection.rows, original)
        self.assertEqual(self.connection.commits + self.connection.rollbacks, 0)
        self.assertTrue(all(sql.startswith("SELECT id, question") for sql, _ in self.connection.executions))
        notify.assert_not_called()
        backup.assert_not_called()
        self.assertIn("新增 1，修改 1，未变化 0，将被忽略 0", self.output.getvalue())

    def test_prune_without_yes_prints_list_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            import_rows(self.connection, [FaqRow("问题", "新", "product")], prune=True,
                        backup_dir=self.directory)
        self.assertIn("已停用", self.output.getvalue())
        self.assertEqual(self.connection.commits, 0)
        self.assertFalse(any(sql.startswith(("DELETE", "UPDATE", "INSERT"))
                             for sql, _ in self.connection.executions))
        self.assertEqual(list(self.directory.glob("faq-*.csv")), [])

    def test_import_backup_restore_roundtrip_and_publish(self):
        original = copy.deepcopy(self.connection.rows)
        self.redis.values["rate:keep"] = "4"
        self.redis.values["faq:cache:old"] = "old"
        self.assertEqual(load_faq.main([str(self.csv), "--prune", "--yes"], settings=self.settings,
                                      connection=self.connection, client=self.redis,
                                      backup_dir=self.directory), 0)
        backup = next(self.directory.glob("faq-*.csv"))
        self.assertEqual(read_backup(backup), original)
        self.assertNotIn("已停用", {r["question"] for r in self.connection.rows})
        self.redis.values["faq:cache:new"] = "new"
        self.assertEqual(faq_restore.main(["--from", str(backup), "--yes"], settings=self.settings,
                                         connection=self.connection, client=self.redis,
                                         backup_dir=self.directory), 0)
        self.assertEqual(self.connection.rows, original)
        self.assertEqual(len(list(self.directory.glob("faq-*.csv"))), 2)
        self.assertEqual(self.redis.values, {"rate:keep": "4", VERSION_KEY: 2})

    def test_restore_without_yes_never_connects_or_writes(self):
        backup = backup_state(self.connection.rows, directory=self.directory)
        with patch("ingest.faq_restore.connect_mysql") as connect, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(faq_restore.main(["--from", str(backup)]), 1)
        connect.assert_not_called()
        self.assertEqual(self.connection.executions, [])

    def test_write_failure_rolls_back_and_does_not_publish(self):
        self.connection.fail_write = True
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = load_faq.main([str(self.csv)], settings=self.settings, connection=self.connection,
                                 client=self.redis, backup_dir=self.directory)
        self.assertEqual(code, 1)
        self.assertEqual(self.connection.rows, self.connection.before)
        self.assertEqual(self.redis.values, {})
        self.assertNotIn("PRIVATE", err.getvalue())

    def test_backup_failure_prevents_mutation(self):
        with patch("ingest.faq_admin.backup_state", side_effect=OSError("PRIVATE")):
            with self.assertRaises(OSError):
                import_rows(self.connection, [FaqRow("问题", "新", "product")])
        self.assertEqual(self.connection.commits, 0)
        self.assertFalse(any(sql.startswith(("DELETE", "UPDATE", "INSERT"))
                             for sql, _ in self.connection.executions))

    def test_redis_failure_after_commit_is_distinct_and_sanitized(self):
        self.redis.incr = Mock(side_effect=ConnectionError("PRIVATE credentials"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = load_faq.main([str(self.csv)], settings=self.settings, connection=self.connection,
                                 client=self.redis, backup_dir=self.directory)
        self.assertEqual(code, 2)
        self.assertEqual(self.connection.commits, 1)
        self.assertIn("faq_reload", err.getvalue())
        self.assertNotIn("PRIVATE", err.getvalue())

    def test_publish_and_reload_never_delete_other_keys(self):
        self.redis.values.update({"faq:cache:hash": "a", "chat:rate:ip:keep": "1", "other": "2"})
        self.assertEqual(publish(self.redis), 1)
        self.redis.values["faq:cache:untouched"] = "a"
        self.assertEqual(faq_reload.main([], settings=self.settings, client=self.redis), 0)
        self.assertEqual(self.redis.values, {VERSION_KEY: 2, "chat:rate:ip:keep": "1", "other": "2",
                                            "faq:cache:untouched": "a"})

    def test_strict_validation_precedes_database_access(self):
        for content in ("answer,question,category\n", "question,answer,category\nq,a,INVALID\n",
                        "question,answer,category\nq,,product\n"):
            self.csv.write_text(content, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(load_faq.main([str(self.csv), "--dry-run"], connection=self.connection), 1)
        self.assertEqual(self.connection.executions, [])

    def test_duplicates_and_normalized_near_duplicates_are_reported(self):
        self.csv.write_text("question,answer,category\nＡ？,旧,product\nＡ？,新,product\na,近似,product\n", encoding="utf-8")
        report = {}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rows = load_faq.read_faq(self.csv, report=report)
        self.assertEqual(report["ignored"], 1)
        self.assertEqual(rows[0].answer, "新")
        self.assertIn("规范化后相同", err.getvalue())

    def test_unchanged_and_default_import_retains_absent_questions(self):
        rows = [FaqRow("问题", "原答案", "product")]
        self.assertFalse(import_rows(self.connection, rows, backup_dir=self.directory))
        self.assertEqual(len(self.connection.rows), 2)
        self.assertIn("未变化 1", self.output.getvalue())
        self.assertIn("无变化，未发布", self.output.getvalue())

    def test_unchanged_import_never_notifies_or_clears_cache(self):
        self.csv.write_text("question,answer,category\n问题,原答案,product\n", encoding="utf-8")
        self.redis.values.update({VERSION_KEY: 7, "faq:cache:keep": "cached", "chat:rate:ip:keep": "3"})
        original = copy.deepcopy(self.redis.values)
        with patch("ingest.faq_admin.notify") as notify:
            self.assertEqual(load_faq.main([str(self.csv)], settings=self.settings,
                                          connection=self.connection, client=self.redis,
                                          backup_dir=self.directory), 0)
        notify.assert_not_called()
        self.assertEqual(self.redis.values, original)
        self.assertFalse(any(sql.startswith(("INSERT", "UPDATE", "DELETE"))
                             for sql, _ in self.connection.executions))
        self.assertIn("无变化，未发布", self.output.getvalue())

    def test_prune_only_change_still_publishes(self):
        self.csv.write_text("question,answer,category\n问题,原答案,product\n", encoding="utf-8")
        self.redis.values.update({VERSION_KEY: 7, "faq:cache:old": "cached"})
        self.assertEqual(load_faq.main([str(self.csv), "--prune", "--yes"], settings=self.settings,
                                      connection=self.connection, client=self.redis,
                                      backup_dir=self.directory), 0)
        self.assertEqual(self.redis.values, {VERSION_KEY: 8})
        self.assertEqual(len(self.connection.rows), 1)

    def test_version_reads_are_ttl_cached_and_outage_recovers(self):
        clock = Mock(return_value=100)
        version = FaqVersion(FaqCache(self.redis), interval_s=3, clock=clock)
        self.redis.get = Mock(wraps=self.redis.get)
        self.assertEqual(version.read(), 0)
        self.redis.incr(VERSION_KEY)
        self.assertEqual(version.read(), 0)
        self.assertEqual(self.redis.get.call_count, 1)
        clock.return_value = 104
        self.assertEqual(version.read(), 1)
        self.redis.get.side_effect = ConnectionError("PRIVATE")
        with self.assertLogs("app.faq.version", "WARNING") as logs:
            for now in (108, 112, 116):
                clock.return_value = now
                self.assertIsNone(version.read())
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.redis.get.side_effect = None
        clock.return_value = 120
        self.assertEqual(version.read(), 1)

    def test_cache_outage_warnings_are_rate_limited_per_operation(self):
        cache = FaqCache(self.redis)
        self.redis.get = Mock(side_effect=ConnectionError("PRIVATE"))
        with self.assertLogs("app.faq.cache", "WARNING") as logs:
            for _ in range(5):
                self.assertIsNone(cache.get("query"))
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("PRIVATE", "".join(logs.output))
