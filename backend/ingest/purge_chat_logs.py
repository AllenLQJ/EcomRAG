"""默认只统计；--yes 才删除过期对话原文。"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from app.chat_log import connect_mysql


def retention_days(value):
    try:
        days = int(value)
        if not 1 <= days <= 365000:
            raise ValueError
        return days
    except ValueError:
        raise argparse.ArgumentTypeError("days 必须为 1–365000 的整数") from None


def purge(connection, *, days, confirm=False, now=None):
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
    with connection.cursor() as cursor:
        cursor.execute("SET time_zone = '+00:00'")
        cursor.execute("SELECT COUNT(*) FROM chat_logs WHERE created_at < %s", (cutoff,))
        count = cursor.fetchone()[0]
        if confirm:
            cursor.execute("DELETE FROM chat_logs WHERE created_at < %s", (cutoff,))
            count = cursor.rowcount
    if confirm:
        connection.commit()
    return count


def main(argv=None, *, settings=None, connection_factory=None):
    parser = argparse.ArgumentParser(description="清理过期对话；默认只统计，--yes 才执行删除")
    parser.add_argument("--days", type=retention_days, required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    try:
        connection = (connection_factory or (lambda: connect_mysql(settings)))()
        try:
            count = purge(connection, days=args.days, confirm=args.yes)
        finally:
            connection.close()
        print(f"{'已删除' if args.yes else '将删除（未执行）'}：{count} 条，保留最近 {args.days} 天")
        return 0
    except Exception:
        print("对话记录清理失败，请检查 MySQL 服务与权限", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
