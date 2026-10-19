"""python -m ingest.faq_restore --from backups/faq/文件.csv --yes"""
import argparse
import csv
import sys

from ingest.faq_admin import notify, read_backup, restore_rows
from ingest.load_faq import connect_mysql
from ingest.loaders import resolve_data_path


def main(argv=None, *, settings=None, connection=None, client=None, backup_dir=None):
    parser = argparse.ArgumentParser(description="先备份当前状态，再完整恢复 FAQ 备份")
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = read_backup(resolve_data_path(args.source))
    except (ValueError, OSError, csv.Error):
        print("FAQ 恢复未执行：备份文件或内容不合法", file=sys.stderr)
        return 1
    if not args.yes:
        print(f"FAQ 将完整恢复 {len(rows)} 行；请核对备份后添加 --yes 确认", file=sys.stderr)
        return 1
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    owned = connection is None
    try:
        if owned:
            connection = connect_mysql(settings)
        restore_rows(connection, rows, yes=True, backup_dir=backup_dir)
        return notify(settings, client=client)
    except Exception:
        print("FAQ 恢复失败：当前事务已回滚，请检查数据库或备份目录", file=sys.stderr)
        return 1
    finally:
        if owned and connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
