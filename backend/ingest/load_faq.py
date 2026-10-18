from __future__ import annotations

import argparse
import csv
import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingest.loaders import CATEGORIES, resolve_data_path

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True)
class FaqRow:
    question: str
    answer: str
    category: str


def read_faq(path) -> list[FaqRow]:
    """校验 CSV，重复问题保留最后一条，不访问数据库。"""
    rows = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["question", "answer", "category"]:
            raise ValueError("FAQ CSV 表头必须为 question,answer,category")
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"FAQ 第 {line} 行列数不正确")
            values = {key: value.strip() for key, value in row.items()}
            if not values["question"] or not values["answer"] or len(values["question"]) > 500:
                raise ValueError(f"FAQ 第 {line} 行问题或答案为空，或问题超过 500 字")
            if values["category"] not in CATEGORIES:
                raise ValueError(f"FAQ 第 {line} 行分类不合法")
            rows[values["question"]] = FaqRow(**values)
    return list(rows.values())


def upsert_faq(connection, rows: list[FaqRow]) -> int:
    """命名锁串行化导入，兼容已有无 question 唯一索引的 FAQ 表。"""
    lock_name = "ecomrag_faq_import"
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
            result = cursor.fetchone()
            acquired = bool(result and result[0] == 1)
            if not acquired:
                raise RuntimeError("无法获取 FAQ 导入锁，请稍后重试")
            for row in rows:
                cursor.execute("SELECT id FROM faq_pairs WHERE question=%s LIMIT 1 FOR UPDATE",
                               (row.question,))
                if cursor.fetchone():
                    cursor.execute("UPDATE faq_pairs SET answer=%s, category=%s WHERE question=%s",
                                   (row.answer, row.category, row.question))
                else:
                    cursor.execute("INSERT INTO faq_pairs (question, answer, category) VALUES (%s, %s, %s)",
                                   (row.question, row.answer, row.category))
        connection.commit()
        return len(rows)
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description="按问题去重导入 FAQ CSV")
    parser.add_argument("path", help="FAQ CSV 路径")
    args = parser.parse_args(argv)
    rows = read_faq(resolve_data_path(args.path))
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    try:
        pymysql = importlib.import_module("pymysql")
    except ImportError as exc:
        raise RuntimeError("FAQ 入库需要 pymysql，请通过 uv 安装声明的依赖") from exc
    connection = pymysql.connect(
        host=settings.mysql_host, port=settings.mysql_port,
        user=settings.mysql_user, password=settings.mysql_password.get_secret_value(),
        database=settings.mysql_database, charset="utf8mb4", autocommit=False, connect_timeout=10,
    )
    try:
        print(f"FAQ 导入完成：{upsert_faq(connection, rows)} 条（按 question 更新或插入）")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
