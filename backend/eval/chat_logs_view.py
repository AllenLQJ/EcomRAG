"""本机授权查看对话原文；输出到终端，不写应用日志。"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from app.chat_log import connect_mysql


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def since_delta(value):
    match = re.fullmatch(r"([1-9][0-9]*)(h|d)", value)
    if not match:
        raise argparse.ArgumentTypeError("格式须为 2h 或 1d 等正数时段")
    try:
        return timedelta(**{"hours" if match[2] == "h" else "days": int(match[1])})
    except OverflowError:
        raise argparse.ArgumentTypeError("时间范围过大") from None


def read_logs(connection, *, route=None, session=None, since=None, limit=20, full=None, now=None):
    columns = "id, created_at, session_id, route, latency_ms, question, answer"
    if full is not None:
        sql = "SELECT * FROM chat_logs WHERE id=%s"
        params = (full,)
    else:
        clauses, values = [], []
        for column, value in (("route", route), ("session_id", session)):
            if value is not None:
                clauses.append(f"{column}=%s")
                values.append(value)
        if since is not None:
            # MySQL TIMESTAMP 的转换由会话时区决定，用 UTC 会话避免本机时区差异。
            current = now or datetime.now(timezone.utc)
            clauses.append("created_at >= %s")
            values.append((current - since).astimezone(timezone.utc).replace(tzinfo=None))
        sql = f"SELECT {columns} FROM chat_logs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
        params = (*values, limit)
    with connection.cursor() as cursor:
        cursor.execute("SET time_zone = '+00:00'")
        cursor.execute(sql, params)
        names = [item[0] for item in cursor.description]
        return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]


def print_rows(rows, *, full=False):
    if full:
        print(json.dumps(rows[0] if rows else None, ensure_ascii=False, indent=2, default=str))
        return
    print("时间(UTC)\tsession\troute\t耗时(ms)\t问题前40字\t答案前40字")
    def preview(value):
        # 原文中的控制字符不能破坏表格结构。
        return str(value or "")[:40].replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    for row in rows:
        print("\t".join(preview(row.get(key)) if key in {"session_id", "question", "answer"}
                        else str(row.get(key, "")) for key in
                        ("created_at", "session_id", "route", "latency_ms", "question", "answer")))


def main(argv=None, *, settings=None, connection_factory=None):
    parser = argparse.ArgumentParser(description="查看 MySQL 对话原文（本机管理工具）")
    parser.add_argument("--route", choices=["cache", "faq", "rag", "general", "fallback", "error"])
    parser.add_argument("--session")
    parser.add_argument("--since", type=since_delta)
    parser.add_argument("--limit", type=positive_int, default=20)
    parser.add_argument("--full", type=positive_int, metavar="ID")
    args = parser.parse_args(argv)
    if args.full is not None and any(value is not None for value in (args.route, args.session, args.since)):
        parser.error("--full 不与筛选参数同时使用")
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    try:
        connection = (connection_factory or (lambda: connect_mysql(settings)))()
        try:
            rows = read_logs(connection, route=args.route, session=args.session, since=args.since,
                             limit=args.limit, full=args.full)
        finally:
            connection.close()
        print_rows(rows, full=args.full is not None)
        return 0
    except Exception:
        print("对话记录读取失败，请检查 MySQL 服务、表结构和权限", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
