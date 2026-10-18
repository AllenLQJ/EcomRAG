"""原文只写 MySQL；所有日志写入和迁移失败均使用固定 warning。"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from dataclasses import dataclass, field

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
    def __init__(self, settings, *, connection_factory=None):
        self.settings = settings
        self.connection_factory = connection_factory or (lambda: connect_mysql(settings))

    def _with_connection(self, operation):
        connection = self.connection_factory()
        try:
            operation(connection)
        finally:
            connection.close()

    async def write(self, row):
        if not self.settings.chat_log_enabled:
            return
        try:
            await asyncio.to_thread(self._with_connection, lambda conn: insert_chat_log(conn, row))
        except Exception:
            logger.warning("对话记录写入失败，已跳过")

    async def ensure_schema(self):
        if not self.settings.chat_log_enabled:
            return
        try:
            await asyncio.to_thread(self._with_connection, ensure_chat_logs_schema)
        except Exception:
            logger.warning("对话表补列失败，请运行 ingest.migrate_db")


async def persist(writer, row):
    # 可注入写入器也遵循失败不影响响应的约定。
    try:
        await writer.write(row)
    except Exception:
        logger.warning("对话记录写入失败，已跳过")
