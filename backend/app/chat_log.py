"""原文只写 MySQL；所有日志写入和迁移失败均使用固定 warning。"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from dataclasses import dataclass, field
from concurrent.futures import Future, InvalidStateError
from queue import Queue, Empty, Full
import threading

logger = logging.getLogger(__name__)


def connect_mysql(settings):
    return importlib.import_module("pymysql").connect(
        host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(), database=settings.mysql_database,
        charset="utf8mb4", autocommit=False, connect_timeout=5, read_timeout=5, write_timeout=5,
    )


def ensure_chat_logs_schema(connection):
    """MySQL 8.0 补列；命名锁避免多 worker 同时检查后重复 ALTER。"""
    lock = "ecomrag_chat_logs_schema"
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", (lock, 5))
            row = cursor.fetchone()
            acquired = bool(row and row[0] == 1)
            if not acquired:
                raise RuntimeError("无法取得对话表迁移锁")
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", ("chat_logs",))
            columns = {row[0] for row in cursor.fetchall()}
            # 只能从固定白名单生成 DDL；标识符不来自输入。
            for name, definition in (("sources_json", "JSON NULL"), ("error_code", "INT NULL")):
                if name not in columns:
                    cursor.execute(f"ALTER TABLE chat_logs ADD COLUMN {name} {definition}")
        connection.commit()
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock,))


@dataclass(frozen=True)
class ChatLog:
    request_id: str
    session_id: str
    question: str
    answer: str
    route: str
    latency_ms: float
    strategy: str | None = None
    sources: list[dict] = field(default_factory=list)
    error_code: int | None = None


def insert_chat_log(connection, row):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO chat_logs (request_id, session_id, user_id, question, answer, route, "
            "strategy, latency_ms, sources_json, error_code) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (row.request_id, row.session_id, None, row.question, row.answer, row.route, row.strategy,
             max(0, round(row.latency_ms)), json.dumps(row.sources, ensure_ascii=False), row.error_code),
        )
    connection.commit()


class ChatLogWriter:
    def __init__(self, settings, *, connection_factory=None, capacity=1000):
        if capacity <= 0:
            raise ValueError("对话记录队列容量必须大于零")
        self.settings = settings
        self.connection_factory = connection_factory or (lambda: connect_mysql(settings))
        self.queue = Queue(maxsize=capacity)
        self._thread = None
        self._lock = threading.Lock()
        self._stop, self._abandon = threading.Event(), threading.Event()
        self._closed = False

    def _start_locked(self):
        if self._thread is None:
            thread = threading.Thread(target=self._work, name="chat-log-writer", daemon=True)
            thread.start()
            self._thread = thread

    def start(self):
        if self.settings.chat_log_enabled:
            with self._lock:
                if not self._closed:
                    self._start_locked()

    def _enqueue(self, kind, row=None, future=None):
        if not self.settings.chat_log_enabled:
            return False
        with self._lock:
            if self._closed:
                logger.warning("对话记录队列已关闭，已丢弃")
                return False
            self._start_locked()
            try:
                self.queue.put_nowait((kind, row, future))
                return True
            except Full:
                logger.warning("对话记录队列已满，已丢弃")
                return False

    @staticmethod
    def _close(connection):
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.warning("对话记录连接关闭失败")

    @staticmethod
    def _resolve(future, value):
        if future is not None:
            try:
                future.set_result(value)
            except InvalidStateError:
                pass  # 等待迁移的启动任务可能已经被取消。

    def _work(self):
        connection = None
        try:
            while not self._abandon.is_set():
                if self._stop.is_set() and self.queue.empty():
                    break
                try:
                    kind, row, future = self.queue.get(timeout=0.05)
                except Empty:
                    continue
                success = False
                try:
                    if connection is None:
                        connection = self.connection_factory()
                    elif callable(getattr(connection, "ping", None)):
                        connection.ping(reconnect=True)
                    if kind == "schema":
                        ensure_chat_logs_schema(connection)
                    else:
                        insert_chat_log(connection, row)
                    success = True
                except Exception:
                    logger.warning("对话表补列失败，请运行 ingest.migrate_db" if kind == "schema"
                                   else "对话记录写入失败，已跳过；下一条将重连")
                    # 提交结果不确定时不重放当前行；丢弃失败行，下一条自动重连。
                    self._close(connection)
                    connection = None
                finally:
                    self._resolve(future, success)
                    self.queue.task_done()
        finally:
            self._close(connection)

    async def write(self, row):
        return self._enqueue("row", row)

    async def ensure_schema(self):
        if not self.settings.chat_log_enabled:
            return
        future = Future()
        if self._enqueue("schema", future=future):
            await asyncio.wrap_future(future)

    async def aclose(self, *, timeout=5):
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is None:
            return
        # 不把 join 排入可能已被模型任务占满的线程池，确保退出等待本身也最多 5 秒。
        loop = asyncio.get_running_loop()
        end = loop.time() + min(max(timeout, 0), 5)
        while thread.is_alive() and loop.time() < end:
            await asyncio.sleep(min(0.02, max(0, end - loop.time())))
        if thread.is_alive():
            logger.warning("对话记录队列关闭超时，放弃未完成记录")
            self._abandon.set()
            while True:
                try:
                    _, _, future = self.queue.get_nowait()
                except Empty:
                    break
                self._resolve(future, False)
                self.queue.task_done()


async def persist(writer, row):
    # 可注入写入器也遵循失败不影响响应的约定。
    try:
        await writer.write(row)
    except Exception:
        logger.warning("对话记录写入失败，已跳过")
