"""python -m ingest.migrate_products：幂等创建独立 products 表。"""
import argparse
import sys

from ingest.load_products import connect_mysql, ensure_products_schema


def main(argv=None, *, settings=None, connection_factory=None):
    argparse.ArgumentParser(description="幂等创建 products 表，不加载 .env").parse_args(argv)
    try:
        connection = (connection_factory or (lambda: connect_mysql(settings)))()
        try:
            ensure_products_schema(connection)
        finally:
            connection.close()
    except Exception:
        print("商品表迁移失败，请检查 pymysql 依赖、MySQL 连接与 CREATE 权限", file=sys.stderr)
        return 1
    print("products 建表完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
