"""python -m ingest.load_products data/products/products.csv [--dry-run]。"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from ingest.products import FIELDS, read_products

SCHEMA = Path(__file__).with_name("products_schema.sql")
UPSERT = (
    "INSERT INTO products (" + ", ".join(FIELDS) + ") VALUES (" + ", ".join(["%s"] * len(FIELDS)) + ") "
    "ON DUPLICATE KEY UPDATE " + ", ".join(f"{field}=VALUES({field})" for field in FIELDS if field != "sku_id")
)


def connect_mysql(settings=None):
    """沿用现有 pymysql 连接风格；仅进程环境或显式注入配置，绝不加载 .env。"""
    try:
        driver = importlib.import_module("pymysql")
    except ImportError as exc:
        raise RuntimeError("商品入库需要 pymysql，请在当前 Python 环境安装项目已声明的依赖") from exc
    if settings is None:
        host = os.environ.get("MYSQL_HOST", "127.0.0.1")
        port = int(os.environ.get("MYSQL_PORT", "3306"))
        user = os.environ.get("MYSQL_USER", "ecomrag")
        password = os.environ.get("MYSQL_PASSWORD", "")
        database = os.environ.get("MYSQL_DATABASE", "ecomrag")
    else:
        host, port, user, database = settings.mysql_host, settings.mysql_port, settings.mysql_user, settings.mysql_database
        password = settings.mysql_password
        if hasattr(password, "get_secret_value"):
            password = password.get_secret_value()
    return driver.connect(host=host, port=port, user=user, password=password, database=database,
                          charset="utf8mb4", autocommit=False, connect_timeout=10, read_timeout=10, write_timeout=10)


def ensure_products_schema(connection):
    # MySQL DDL 会隐式提交；在导入事务前执行，与后续 DML 回滚边界分开。
    with connection.cursor() as cursor:
        cursor.execute(SCHEMA.read_text(encoding="utf-8"))
    connection.commit()


def upsert_products(connection, rows, *, prune=False, yes=False):
    """主键 upsert；命名锁串行化导入/裁剪，失败回滚整批 DML。"""
    if prune and not yes:
        raise ValueError("删除多余行必须同时指定 --prune --yes")
    if not rows:
        raise ValueError("拒绝导入空商品表")
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", ("ecomrag_products_import", 10))
            result = cursor.fetchone()
            acquired = bool(result and result[0] == 1)
            if not acquired:
                raise RuntimeError("无法获取商品导入锁")
            for row in rows:
                values = tuple(json.dumps(row[field], ensure_ascii=False) if field == "specs" else row[field] for field in FIELDS)
                cursor.execute(UPSERT, values)
            if prune:
                placeholders = ", ".join(["%s"] * len(rows))
                cursor.execute(f"DELETE FROM products WHERE sku_id NOT IN ({placeholders})", tuple(row["sku_id"] for row in rows))
        connection.commit()
        return len(rows)
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", ("ecomrag_products_import",))


def main(argv=None, *, settings=None, connection_factory=None):
    parser = argparse.ArgumentParser(description="校验并按 sku_id 幂等导入商品；不加载 .env")
    parser.add_argument("path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if args.prune and not args.yes:
        parser.error("删除多余行必须同时指定 --prune --yes")
    try:
        rows = read_products(args.path)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"dry-run：校验通过 {len(rows)} 款；计划按 sku_id upsert；" +
              ("计划删除表中其他 SKU" if args.prune else "保留表中其他 SKU") + "；未连接数据库")
        return 0
    try:
        connection = (connection_factory or (lambda: connect_mysql(settings)))()
        try:
            ensure_products_schema(connection)
            count = upsert_products(connection, rows, prune=args.prune, yes=args.yes)
        finally:
            connection.close()
    except Exception:
        # 不回显可能包含连接地址或密码的驱动异常。
        print("商品导入失败，请检查 pymysql 依赖、MySQL 连接、products 表结构与建表/写入权限", file=sys.stderr)
        return 1
    print(f"商品导入完成：{count} 款（按 sku_id 更新或插入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
