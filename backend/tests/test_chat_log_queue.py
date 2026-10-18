import asyncio
import threading
import unittest
from unittest.mock import Mock

from app.chat_log import ChatLog, ChatLogWriter
from test_chat_log_storage import FakeConnection, config


def row(index):
    return ChatLog(str(index), "session", "PRIVATE_QUESTION", "PRIVATE_ANSWER", "general", 1)


class ChatLogQueueTests(unittest.TestCase):
    def test_persistent_connection_single_worker_and_shutdown_drain(self):
        connection = FakeConnection()
        connection.ping = Mock()
        factory = Mock(return_value=connection)
        writer = ChatLogWriter(config(), connection_factory=factory)
        thread_ids = []
        original = connection.execute
        def execute(*args):
            thread_ids.append(threading.get_ident())
            return original(*args)
        connection.execute = execute
        async def run():
            await writer.ensure_schema()
            for i in range(20):
                self.assertTrue(await writer.write(row(i)))
            await writer.aclose()
        asyncio.run(run())
        factory.assert_called_once()
        self.assertEqual(connection.ping.call_count, 20)
        connection.ping.assert_called_with(reconnect=True)
        self.assertEqual(sum(sql.startswith("INSERT") for sql, _ in connection.calls), 20)
        self.assertEqual(len(set(thread_ids)), 1)
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.assertTrue(connection.closed)
        self.assertFalse(writer._thread.is_alive())

    def test_full_queue_drops_without_waiting_for_blocked_connection(self):
        entered, release = threading.Event(), threading.Event()
        connection = FakeConnection()
        def factory():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test must release worker")
            return connection
        writer = ChatLogWriter(config(), capacity=1, connection_factory=factory)
        async def run():
            try:
                self.assertTrue(await writer.write(row(0)))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertTrue(await writer.write(row(1)))
                with self.assertLogs("app.chat_log", "WARNING") as logs:
                    self.assertFalse(await writer.write(row(2)))
                self.assertIn("队列已满", logs.output[0])
                self.assertNotIn("PRIVATE", "".join(logs.output))
                self.assertEqual(writer.queue.qsize(), 1)
            finally:
                release.set()
                await writer.aclose()
        asyncio.run(run())
        self.assertEqual(sum(sql.startswith("INSERT") for sql, _ in connection.calls), 2)

    def test_failure_discards_uncertain_row_and_next_row_reconnects(self):
        broken, healthy = FakeConnection(), FakeConnection()
        broken.execute = Mock(side_effect=ConnectionError("PRIVATE_QUESTION FAKE_KEY"))
        factory = Mock(side_effect=[broken, healthy])
        writer = ChatLogWriter(config(), connection_factory=factory)
        async def run():
            await writer.write(row(0))
            await writer.write(row(1))
            await writer.aclose()
        with self.assertLogs("app.chat_log", "WARNING") as logs:
            asyncio.run(run())
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(broken.closed and healthy.closed)
        self.assertEqual(len(healthy.calls), 1)
        self.assertEqual(healthy.calls[0][1][0], "1")
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.assertNotIn("FAKE_KEY", "".join(logs.output))

    def test_shutdown_timeout_drops_pending_rows_and_returns(self):
        entered, release = threading.Event(), threading.Event()
        connection = FakeConnection()
        original = connection.execute
        def execute(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        connection.execute = execute
        writer = ChatLogWriter(config(), connection_factory=lambda: connection)
        async def run():
            try:
                await writer.write(row(0))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await writer.write(row(1))
                with self.assertLogs("app.chat_log", "WARNING") as logs:
                    await writer.aclose(timeout=0.01)
                self.assertIn("关闭超时", "".join(logs.output))
                self.assertEqual(writer.queue.qsize(), 0)
            finally:
                release.set()
                await asyncio.to_thread(writer._thread.join, 1)
        asyncio.run(run())
        self.assertEqual(len(connection.calls), 1)
        self.assertTrue(connection.closed)

    def test_shutdown_abandon_skips_current_row_after_connect_or_ping_returns(self):
        for stage in ("connect", "ping"):
            with self.subTest(stage=stage):
                entered, release = threading.Event(), threading.Event()
                connection = FakeConnection()
                def blocked():
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test must release worker")
                    return connection
                factory = blocked if stage == "connect" else lambda: connection
                writer = ChatLogWriter(config(), connection_factory=factory)
                async def run():
                    try:
                        if stage == "ping":
                            await writer.ensure_schema()
                            connection.ping = lambda **kwargs: blocked()
                        self.assertTrue(await writer.write(row(0)))
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        self.assertTrue(await writer.write(row(1)))
                        with self.assertLogs("app.chat_log", "WARNING") as logs:
                            await writer.aclose(timeout=0.01)
                        self.assertNotIn("PRIVATE", "".join(logs.output))
                        self.assertEqual(writer.queue.qsize(), 0)
                    finally:
                        release.set()
                        await asyncio.to_thread(writer._thread.join, 1)
                asyncio.run(run())
                self.assertFalse(any(sql.startswith("INSERT") for sql, _ in connection.calls))
                self.assertEqual(writer.queue.unfinished_tasks, 0)
                self.assertTrue(connection.closed)
