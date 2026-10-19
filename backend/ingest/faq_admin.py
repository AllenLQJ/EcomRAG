"""FAQ 运维共享实现：备份/修改共用导入锁，提交后发布 Redis 版本。"""
import csv
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.faq.cache import FaqCache
from app.faq.version import publish
from ingest.load_faq import _upsert_rows
from ingest.loaders import CATEGORIES, PROJECT_ROOT

BACKUP_FIELDS = ["id", "question", "answer", "category", "enabled", "updated_at"]
BACKUP_DIR = PROJECT_ROOT / "backups" / "faq"


@contextmanager
def import_lock(connection):
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", ("ecomrag_faq_import", 10))
            result = cursor.fetchone()
            acquired = bool(result and result[0] == 1)
            if not acquired:
                raise RuntimeError("无法获取 FAQ 导入锁")
        yield
    except Exception:
        connection.rollback()
        raise
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", ("ecomrag_faq_import",))


def read_state(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, question, answer, category, enabled, updated_at FROM faq_pairs ORDER BY id", ())
        return [dict(row) if isinstance(row, Mapping) else dict(zip(BACKUP_FIELDS, row))
                for row in cursor.fetchall()]


def backup_state(state, *, directory=None):
    directory = Path(directory) if directory is not None else BACKUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"faq-{stamp}.csv"
    # 排他创建，备份完整状态（包含停用行、ID 和更新时间），不包含连接配置。
    with path.open("x", encoding="utf-8", newline="") as handle:
        path.chmod(0o600)
        writer = csv.DictWriter(handle, fieldnames=BACKUP_FIELDS)
        writer.writeheader()
        writer.writerows(state)
    print(f"FAQ 当前状态已备份：{path}")
    return path


def read_backup(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != BACKUP_FIELDS:
            raise ValueError("FAQ 备份表头不合法")
        rows, ids = [], set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("FAQ 备份列数不正确")
            row["id"] = int(row["id"])
            row["enabled"] = int(row["enabled"])
            if (row["id"] <= 0 or row["id"] in ids or row["enabled"] not in (0, 1)
                    or not row["question"].strip() or len(row["question"]) > 500
                    or not row["answer"].strip() or row["category"] not in CATEGORIES):
                raise ValueError("FAQ 备份内容不合法")
            datetime.fromisoformat(row["updated_at"])
            ids.add(row["id"])
            rows.append(row)
        return rows


def import_rows(connection, rows, *, dry_run=False, prune=False, yes=False, ignored=0, backup_dir=None):
    # dry-run 只 SELECT，不获取 MySQL 命名锁、不提交、不回滚，不接触 Redis 或文件。
    with (import_lock(connection) if not dry_run else _no_lock()):
        state = read_state(connection)
        current = {}
        for row in state:
            current.setdefault(row["question"], []).append(row)
        questions = {row.question for row in rows}
        added = sum(row.question not in current for row in rows)
        modified = sum(row.question in current and any(
            (old["answer"], old["category"]) != (row.answer, row.category)
            for old in current[row.question]) for row in rows)
        removed = sorted(set(current) - questions) if prune else []
        print(f"FAQ 对比：新增 {added}，修改 {modified}，未变化 {len(rows)-added-modified}，将被忽略 {ignored}")
        if prune:
            print(f"FAQ 将删除的问题（{len(removed)} 个）：")
            for question in removed:
                # repr 防止问题中的换行/控制符伪造 CLI 输出。
                print(f"  {question!r}")
            if not yes and not dry_run:
                raise ValueError("--prune 必须提供 --yes")
        if dry_run:
            return False
        backup_state(state, directory=backup_dir)
        if not added and not modified and not removed:
            connection.commit()
            print("FAQ 无变化，未发布（不递增版本、不清理缓存）")
            return False
        with connection.cursor() as cursor:
            _upsert_rows(cursor, rows)
            for question in removed:
                cursor.execute("DELETE FROM faq_pairs WHERE BINARY question=%s", (question,))
        connection.commit()
        print(f"FAQ 导入完成：{len(rows)} 条，删除 {len(removed)} 个问题")
        return True


@contextmanager
def _no_lock():
    yield


def restore_rows(connection, rows, *, yes=False, backup_dir=None):
    print(f"FAQ 将完整恢复为备份的 {len(rows)} 行（含 enabled/ID/更新时间）")
    if not yes:
        raise ValueError("恢复必须提供 --yes")
    with import_lock(connection):
        backup_state(read_state(connection), directory=backup_dir)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM faq_pairs", ())
            for row in rows:
                cursor.execute(
                    "INSERT INTO faq_pairs (id, question, answer, category, enabled, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", tuple(row[key] for key in BACKUP_FIELDS))
        connection.commit()


def notify(settings, *, client=None):
    cache = FaqCache(client, url=settings.redis_url)
    try:
        redis = cache._client(warn_missing=False)
        if redis is None:
            raise ConnectionError
        version = publish(redis)
        print(f"FAQ 发布通知完成：version={version}；各 worker 在下次 FAQ 请求时检查并后台重建")
        return 0
    except Exception:
        print("FAQ 数据已提交，但 Redis 发布/缓存清理失败；修复 Redis 后运行 python -m ingest.faq_reload", file=sys.stderr)
        return 2
    finally:
        if client is None and cache.client is not None:
            try:
                cache.client.close()
            except Exception:
                pass
