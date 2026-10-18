from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FaqRecord:
    id: int
    question: str
    answer: str
    category: str | None = None


class FaqRepo:
    """注入的连接由调用方管理；工厂或配置创建的连接每次读取后关闭。"""

    def __init__(self, connection=None, *, connection_factory=None, settings=None):
        self.connection = connection
        self.connection_factory = connection_factory
        self.settings = settings

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory()
        if self.settings is None:
            raise ValueError("FAQ 仓库需要注入连接、连接工厂或配置")
        driver = importlib.import_module("pymysql")
        settings = self.settings
        return driver.connect(
            host=settings.mysql_host, port=settings.mysql_port,
            user=settings.mysql_user, password=settings.mysql_password.get_secret_value(),
            database=settings.mysql_database, charset="utf8mb4",
            autocommit=True, connect_timeout=10, read_timeout=10,
        )

    def load_enabled(self) -> list[FaqRecord]:
        connection = self.connection if self.connection is not None else self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, question, answer, category FROM faq_pairs WHERE enabled=%s ORDER BY id",
                    (1,),
                )
                return [FaqRecord(**row) if isinstance(row, Mapping) else FaqRecord(*row)
                        for row in cursor.fetchall()]
        finally:
            if self.connection is None:
                connection.close()
