"""python -m ingest.migrate_db：为已有 chat_logs 表幂等补列。"""
import argparse
import sys

from app.chat_log import connect_mysql, ensure_chat_logs_schema


def main(argv=None, *, settings=None, connection_factory=None):
    argparse.ArgumentParser(description="为 chat_logs 幂等补充 sources_json、error_code 和 history_count").parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    try:
        connection = (connection_factory or (lambda: connect_mysql(settings)))()
        try:
            ensure_chat_logs_schema(connection)
        finally:
            connection.close()
        print("chat_logs 补列完成")
        return 0
    except Exception:
        print("数据库迁移失败，请检查 MySQL 服务、chat_logs 表与 ALTER 权限", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
