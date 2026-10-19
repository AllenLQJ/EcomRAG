"""纯标准库假连接测试：不导入数据库 SDK，不读取环境文件。"""
import asyncio
import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.chat_log import ChatLog, ChatLogWriter, ensure_chat_logs_schema, insert_chat_log, persist
from eval import chat_logs_view
from ingest import migrate_db, purge_chat_logs


class FakeConnection:
    def __init__(self):
        self.columns = {"id"}
        self.calls = []
        self.commits = 0
        self.closed = False
        self.rowcount = 7
        self.last = ""
        self.description = [(name,) for name in ("id", "created_at", "session_id", "route", "latency_ms", "question", "answer")]

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.last = sql
        if sql.startswith("ALTER"):
            self.columns.add(sql.split()[5])

    def fetchone(self):
        return (7,) if "COUNT" in self.last else (1,)

    def fetchall(self):
        if "information_schema" in self.last:
            return [(column,) for column in self.columns]
        return [(1, "2026-10-18", "session", "rag", 12, "Q" * 60, "A" * 60)]

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def config(enabled=True):
    return SimpleNamespace(chat_log_enabled=enabled)


class ChatLogStorageTests(unittest.TestCase):
    def test_insert_preserves_original_text_only_in_bound_values(self):
        connection = FakeConnection()
        row = ChatLog("r", "s", "question'); DROP TABLE chat_logs; --", "原始答案", "rag", 12.6,
                      strategy="direct", sources=[{"doc_id": "d", "index": 1}], history_count=4)
        insert_chat_log(connection, row)
        sql, params = connection.calls[0]
        self.assertEqual(sql.count("%s"), 11)
        self.assertEqual(params[:8], ("r", "s", None, row.question, row.answer, "rag", "direct", 13))
        self.assertEqual(json.loads(params[8]), row.sources)
        self.assertIsNone(params[9])
        self.assertIn("history_count", sql)
        self.assertEqual(params[10], 4)
        self.assertNotIn(row.question, sql)
        self.assertNotIn(row.answer, sql)
        self.assertEqual(connection.commits, 1)

    def test_error_row(self):
        connection = FakeConnection()
        insert_chat_log(connection, ChatLog("r", "s", "原始问题", "", "error", 3, error_code=50201))
        params = connection.calls[0][1]
        self.assertEqual((params[4], params[5], params[9]), ("", "error", 50201))
        self.assertEqual(params[10], 0)

    def test_schema_idempotent_and_lock_released(self):
        connection = FakeConnection()
        ensure_chat_logs_schema(connection)
        ensure_chat_logs_schema(connection)
        alters = [sql for sql, _ in connection.calls if sql.startswith("ALTER")]
        self.assertEqual(alters, ["ALTER TABLE chat_logs ADD COLUMN sources_json JSON NULL",
                                  "ALTER TABLE chat_logs ADD COLUMN error_code INT NULL",
                                  "ALTER TABLE chat_logs ADD COLUMN history_count TINYINT UNSIGNED NOT NULL DEFAULT 0"])
        self.assertEqual(sum("RELEASE_LOCK" in sql for sql, _ in connection.calls), 2)
        self.assertEqual(connection.commits, 2)

    def test_schema_lock_failure_does_not_alter_or_release(self):
        connection = FakeConnection()
        connection.fetchone = lambda: (0,)
        with self.assertRaises(RuntimeError):
            ensure_chat_logs_schema(connection)
        self.assertFalse(any("ALTER" in sql or "RELEASE_LOCK" in sql for sql, _ in connection.calls))

    def test_writer_runs_in_thread_and_closes_connection(self):
        import threading
        connection = FakeConnection()
        caller = threading.get_ident()
        seen = []
        def factory():
            seen.append(threading.get_ident())
            return connection
        writer = ChatLogWriter(config(), connection_factory=factory)
        async def run():
            await writer.write(ChatLog("r", "s", "q", "a", "general", 1))
            await writer.aclose()
        asyncio.run(run())
        self.assertNotEqual(seen[0], caller)
        self.assertTrue(connection.closed)

    def test_write_and_schema_failures_do_not_leak(self):
        writer = ChatLogWriter(config(), connection_factory=Mock(side_effect=RuntimeError("PRIVATE_Q PRIVATE_A FAKE_KEY")))
        async def run():
            await writer.write(ChatLog("r", "s", "PRIVATE_Q", "PRIVATE_A", "general", 1))
            await writer.ensure_schema()
            await writer.aclose()
        with self.assertLogs("app.chat_log", "WARNING") as logs:
            asyncio.run(run())
        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.assertNotIn("FAKE_KEY", "".join(logs.output))

    def test_disabled_skips_connection_and_migration(self):
        factory = Mock(side_effect=AssertionError("不得连接"))
        writer = ChatLogWriter(config(False), connection_factory=factory)
        writer.start()
        self.assertEqual(writer.queue.maxsize, 1000)
        async def run():
            await writer.write(ChatLog("r", "s", "q", "a", "general", 1))
            await writer.ensure_schema()
        asyncio.run(run())
        factory.assert_not_called()
        self.assertIsNone(writer._thread)

    def test_injected_writer_failure_is_contained(self):
        writer = SimpleNamespace(write=AsyncMock(side_effect=ValueError("private")))
        with self.assertLogs("app.chat_log", "WARNING") as logs:
            asyncio.run(persist(writer, object()))
        self.assertNotIn("private", "".join(logs.output))

    def test_view_filters_and_full_are_parameterized(self):
        connection = FakeConnection()
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        rows = chat_logs_view.read_logs(connection, route="rag", session="s' OR 1=1", since=timedelta(hours=2), limit=3, now=now)
        sql, params = connection.calls[-1]
        self.assertIn("route=%s AND session_id=%s AND created_at >= %s", sql)
        self.assertNotIn("s' OR", sql)
        self.assertEqual(params, ("rag", "s' OR 1=1", datetime(2026, 10, 17, 22), 3))
        self.assertEqual(rows[0]["id"], 1)
        chat_logs_view.read_logs(connection, full=42)
        self.assertEqual(connection.calls[-1], ("SELECT * FROM chat_logs WHERE id=%s", (42,)))

    def test_view_cli_preview_and_full(self):
        for args in ([], ["--full", "1"]):
            connection = FakeConnection()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(chat_logs_view.main(args, settings=config(), connection_factory=lambda: connection), 0)
            self.assertTrue(connection.closed)
            if args:
                self.assertEqual(json.loads(output.getvalue())["question"], "Q" * 60)
            else:
                self.assertIn("Q" * 40, output.getvalue())
                self.assertNotIn("Q" * 41, output.getvalue())

    def test_purge_dry_run_and_confirm_use_same_bound_cutoff(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        for confirm in (False, True):
            connection = FakeConnection()
            self.assertEqual(purge_chat_logs.purge(connection, days=30, confirm=confirm, now=now), 7)
            self.assertEqual(connection.commits, int(confirm))
            values = [params for sql, params in connection.calls if "WHERE created_at" in sql]
            self.assertEqual(values, [(datetime(2026, 9, 18),)] * (2 if confirm else 1))
            self.assertEqual(any(sql.startswith("DELETE") for sql, _ in connection.calls), confirm)

    def test_purge_cli_requires_yes(self):
        for args in (["--days", "30"], ["--days", "30", "--yes"]):
            connection = FakeConnection()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(purge_chat_logs.main(args, settings=config(), connection_factory=lambda: connection), 0)
            self.assertEqual(connection.commits, int("--yes" in args))
            self.assertTrue(connection.closed)

    def test_cli_invalid_arguments_exit_before_settings_or_connections(self):
        cases = [(chat_logs_view, ["--since", "2w"]), (chat_logs_view, ["--limit", "0"]),
                 (chat_logs_view, ["--route", "bad"]), (chat_logs_view, ["--full", "0"]),
                 (chat_logs_view, ["--full", "1", "--session", "s"]),
                 (purge_chat_logs, ["--days", "-1"]), (purge_chat_logs, ["--days", "x"]),
                 (purge_chat_logs, [])]
        for module, args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                module.main(args)
            self.assertEqual(exc.exception.code, 2)

    def test_migrate_cli_uses_helper_and_closes(self):
        connection = FakeConnection()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(migrate_db.main([], settings=config(), connection_factory=lambda: connection), 0)
        self.assertIn("sources_json", connection.columns)
        self.assertIn("history_count", connection.columns)
        self.assertTrue(connection.closed)

    def test_cli_failure_messages_are_safe_and_nonzero(self):
        factory = Mock(side_effect=ConnectionError("FAKE_KEY PRIVATE_TEXT"))
        for module, args in ((migrate_db, []), (chat_logs_view, []), (purge_chat_logs, ["--days", "3"])):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(module.main(args, settings=config(), connection_factory=factory), 1)
            self.assertNotIn("FAKE_KEY", output.getvalue())
            self.assertNotIn("PRIVATE_TEXT", output.getvalue())

    def test_mysql_connector_uses_explicit_runtime_settings(self):
        from app.chat_log import connect_mysql
        driver = SimpleNamespace(connect=Mock())
        settings = SimpleNamespace(mysql_host="test", mysql_port=3307, mysql_user="user", mysql_database="db",
                                   mysql_password=SimpleNamespace(get_secret_value=lambda: "FAKE_PASSWORD"))
        with patch("app.chat_log.importlib.import_module", return_value=driver) as load:
            connect_mysql(settings)
        load.assert_called_once_with("pymysql")
        driver.connect.assert_called_once_with(host="test", port=3307, user="user", database="db",
            password="FAKE_PASSWORD", charset="utf8mb4", autocommit=False,
            connect_timeout=5, read_timeout=5, write_timeout=5)

    def test_runtime_cli_settings_are_loaded_once_without_reading_env(self):
        import sys
        from ingest import clear_cache as cache_cli
        for module, args in ((cache_cli, []), (migrate_db, []), (chat_logs_view, []),
                             (purge_chat_logs, ["--days", "1"])):
            settings = config()
            settings.redis_url = "redis://fake"
            getter = Mock(return_value=settings)
            connection = FakeConnection()
            kwargs = {"client": Mock()} if module is cache_cli else {"connection_factory": lambda: connection}
            if module is cache_cli:
                kwargs["client"].scan.return_value = (0, [])
            with patch.dict(sys.modules, {"app.config": SimpleNamespace(get_settings=getter)}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(args, **kwargs), 0)
            getter.assert_called_once_with()
