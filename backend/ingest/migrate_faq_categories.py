"""旧 FAQ category ENUM 的幂等兼容迁移；新建表已使用 VARCHAR。"""
from __future__ import annotations

import argparse
import sys


def ensure_faq_categories_schema(connection):
    """只将 ENUM 改成 VARCHAR；再次执行不发 ALTER，保留已有分类值。"""
    with connection.cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM faq_pairs LIKE 'category'")
        column = cursor.fetchone()
        if not column:
            raise ValueError("faq_pairs.category 不存在；请先执行初始化建表")
        kind = column[1].decode() if isinstance(column[1], bytes) else column[1]
        changed = kind.lower().startswith("enum(")
        if changed:
            cursor.execute("ALTER TABLE faq_pairs MODIFY COLUMN category VARCHAR(100) NOT NULL DEFAULT ''")
    connection.commit()
    return changed


def main(argv=None, *, connection_factory=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        if connection_factory is None:
            from ingest.kb_common import cli_settings
            from ingest.load_faq import connect_mysql
            connection_factory = lambda: connect_mysql(cli_settings())
        connection = connection_factory()
        try:
            changed = ensure_faq_categories_schema(connection)
        finally:
            connection.close()
        print("FAQ 分类迁移完成" if changed else "FAQ 分类已兼容，无需 ALTER")
        return 0
    except Exception as exc:
        print(f"FAQ 分类迁移失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
