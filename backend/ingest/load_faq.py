from __future__ import annotations

import argparse
import csv
import importlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingest.loaders import CATEGORIES, resolve_data_path
from app.faq.cache import normalize

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True)
class FaqRow:
    question: str
    answer: str
    category: str


def read_faq(path, *, report=None) -> list[FaqRow]:
    """校验 CSV，重复问题保留最后一条，不访问数据库。"""
    rows = {}
    normalized = {}
    ignored = 0
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
            question = values["question"]
            if question in rows:
                ignored += 1
                print(f"FAQ 第 {line} 行重复 question，保留最后一条", file=sys.stderr)
            key = normalize(question)
            if key in normalized and normalized[key][0] != question:
                print(f"FAQ 第 {line} 行与第 {normalized[key][1]} 行规范化后相同，请核对近重复问题", file=sys.stderr)
            normalized[key] = (question, line)
            rows[question] = FaqRow(**values)
    if report is not None:
        report["ignored"] = ignored
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
            _upsert_rows(cursor, rows)
        connection.commit()
        return len(rows)
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))


def _upsert_rows(cursor, rows):
    for row in rows:
        cursor.execute("SELECT id FROM faq_pairs WHERE BINARY question=%s LIMIT 1 FOR UPDATE",
                       (row.question,))
        if cursor.fetchone():
            cursor.execute("UPDATE faq_pairs SET answer=%s, category=%s WHERE BINARY question=%s",
                           (row.answer, row.category, row.question))
        else:
            cursor.execute("INSERT INTO faq_pairs (question, answer, category) VALUES (%s, %s, %s)",
                           (row.question, row.answer, row.category))


def connect_mysql(settings):
    try:
        pymysql = importlib.import_module("pymysql")
    except ImportError:
        raise RuntimeError("FAQ 入库需要 pymysql，请通过 uv 安装声明的依赖") from None
    return pymysql.connect(
        host=settings.mysql_host, port=settings.mysql_port,
        user=settings.mysql_user, password=settings.mysql_password.get_secret_value(),
        database=settings.mysql_database, charset="utf8mb4", autocommit=False, connect_timeout=10,
        read_timeout=10, write_timeout=10,
    )


def main(argv: list[str] | None = None, *, settings: Settings | None = None,
         connection=None, client=None, backup_dir=None) -> int:
    from ingest.faq_admin import import_rows, notify

    parser = argparse.ArgumentParser(description="校验、备份并导入 FAQ CSV")
    parser.add_argument("path", help="FAQ CSV 路径")
    parser.add_argument("--dry-run", action="store_true", help="只校验与对比，不写库、不备份、不通知")
    parser.add_argument("--prune", action="store_true", help="删除 CSV 中不存在的问题")
    parser.add_argument("--yes", action="store_true", help="确认删除已打印的问题")
    args = parser.parse_args(argv)
    try:
        report = {}
        rows = read_faq(resolve_data_path(args.path), report=report)
    except (ValueError, OSError, csv.Error):
        print("FAQ CSV 校验失败：请检查文件、严格表头、分类及非空问题/答案", file=sys.stderr)
        return 1
    if settings is None and (connection is None or not args.dry_run):
        from app.config import get_settings
        settings = get_settings()
    owned = connection is None
    try:
        if owned:
            connection = connect_mysql(settings)
        changed = import_rows(connection, rows, dry_run=args.dry_run, prune=args.prune,
                              yes=args.yes, ignored=report["ignored"], backup_dir=backup_dir)
        if changed:
            return notify(settings, client=client)
        return 0
    except Exception:
        print("FAQ 导入未执行或失败：请检查数据库、备份目录和 --yes 确认", file=sys.stderr)
        return 1
    finally:
        if owned and connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
