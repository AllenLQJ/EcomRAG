from __future__ import annotations

import argparse
import json
import sys
from time import perf_counter
from typing import TYPE_CHECKING

from ingest.loaders import load_directory, resolve_data_path
from ingest.splitter import split_document
from ingest.kb_common import cli_settings

if TYPE_CHECKING:
    from app.config import Settings


def analyze_documents(documents, *, split_options=None):
    """离线切块与统计，供入库和内容合并共用。"""
    chunks = [chunk for document in documents
              for chunk in split_document(document, **(split_options or {}))]
    return chunks, {
        "documents": len(documents),
        "parent_chunks": len({chunk.parent_id for chunk in chunks}),
        "child_chunks": len(chunks),
    }


def ingest_directory(directory, *, dry_run: bool = True, split_options: dict | None = None,
                     batch_size: int = 16, embedder=None, store=None,
                     sync_delete=False, prune_missing=False, yes=False,
                     staged=False, manager=None, version=None, rebuild=False) -> dict:
    """读取、切块并可选入库；dry-run 全程仅使用标准库。"""
    started = perf_counter()
    if batch_size <= 0:
        raise ValueError("编码批大小必须大于零")
    documents = load_directory(directory)
    if not documents:
        raise ValueError("目录中没有支持的文档")
    chunks, stats = analyze_documents(documents, split_options=split_options)
    if prune_missing and not sync_delete:
        raise ValueError("--prune-missing 必须与 --sync-delete 同用")
    if staged and sync_delete:
        raise ValueError("staged 完整替换与 sync-delete 不能同用")
    if staged and not dry_run:
        if not yes or manager is None or embedder is None:
            raise ValueError("staged 构建需 --yes、发布管理器和编码器")
        return manager.build(chunks, len(documents), embedder, batch_size=batch_size,
                             version=version, rebuild=rebuild)
    if sync_delete and not dry_run and not yes:
        raise ValueError("sync-delete 删除需 --yes")
    plan = None
    if sync_delete and store is not None:
        from ingest.versioning import deletion_plan
        plan = deletion_plan(store, chunks, documents, prune_missing=prune_missing)
        print(json.dumps({"delete_plan": plan, "dry_run": dry_run}, ensure_ascii=False), flush=True)
    if not dry_run:
        if embedder is None or store is None:
            raise ValueError("实际入库必须提供编码器和存储客户端")
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            vectors = embedder.encode([chunk.child_text for chunk in batch])
            store.upsert(batch, vectors)
        if sync_delete:
            store.delete_ids([row["chunk_id"] for row in plan])
            store.flush_load()
    return {
        **stats,
        "elapsed_s": round(perf_counter() - started, 4),
        "dry_run": dry_run,
        **({"delete_plan": plan} if sync_delete else {}),
    }


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识文档入库；默认不下载模型")
    parser.add_argument("--dir", required=True, help="文档目录，相对路径支持仓库根目录")
    parser.add_argument("--dry-run", action="store_true", help="只读取与切块，不连接服务")
    parser.add_argument("--yes", action="store_true", help="确认构建/写入/删除；默认 dry-run")
    parser.add_argument("--staged", action="store_true", help="构建新版本，绝不自动切换")
    parser.add_argument("--version", help="重试同一个 staging 的完整版本名")
    parser.add_argument("--rebuild", action="store_true", help="丢弃未发布 staging 并重建，需 --version --yes")
    parser.add_argument("--sync-delete", action="store_true")
    parser.add_argument("--prune-missing", action="store_true")
    parser.add_argument("--plan", action="store_true", help="与 --sync-delete 同用，只读 Milvus 并打印精确删除清单")
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)
    try:
        if settings is None:
            settings = cli_settings()  # 禁止读取 .env*；只接收进程环境与默认值。
        dry_run = args.dry_run or not args.yes
        if args.plan:
            dry_run = True
            if not args.sync_delete or args.dry_run:
                raise ValueError("--plan 需 --sync-delete，不能与完全离线的 --dry-run 同用")
        if args.prune_missing and not args.sync_delete:
            raise ValueError("--prune-missing 需 --sync-delete")
        if (args.version or args.rebuild) and not args.staged:
            raise ValueError("--version/--rebuild 仅适用于 staged")
        if args.rebuild and not args.version:
            raise ValueError("--rebuild 需明确指定 --version")
        if args.staged and args.sync_delete:
            raise ValueError("staged 与 sync-delete 不能同用")
        options = {
            "batch_size": settings.embed_batch_size,
            "split_options": {
                "parent_max_chars": settings.parent_max_chars,
                "child_chunk_size": settings.child_chunk_size,
                "child_chunk_overlap": settings.child_chunk_overlap,
            },
        }
        if not dry_run or args.plan:
            from app.retrieval.embedder import BgeM3Embedder
            from app.retrieval.store import MilvusStore

            options["store"] = MilvusStore(settings.milvus_uri, timeout_s=settings.milvus_timeout_s)
            if not dry_run:
                options["embedder"] = BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size)
        if args.staged:
            options.update(staged=True, yes=args.yes, version=args.version, rebuild=args.rebuild)
            if not dry_run:
                from ingest.versioning import VersionManager
                manager = VersionManager(options["store"], state_dir=args.state_dir)
                options["manager"] = manager
        if args.sync_delete:
            options.update(sync_delete=True, prune_missing=args.prune_missing, yes=args.yes)
            # 明确 --dry-run 时仍不连接服务；真实删除清单在 --yes 下写入前输出。
        from contextlib import nullcontext
        # 就地更新也共享发布锁，并固定本次目标实体，防止批次中途 alias 改指其他版本。
        manager = options.get("manager")
        if not dry_run and not args.staged:
            from ingest.versioning import VersionManager
            manager = VersionManager(options["store"], state_dir=args.state_dir)
        with manager.locked() if manager else nullcontext():
            if manager and not args.staged:
                manager.recover()
                current = manager.current()
                if current:
                    options["store"] = options["store"].for_collection(current)
                    # 就地修改会使原发布清单失效；拒绝以后用陈旧清单切换回来。
                    record = manager.state["versions"].get(current)
                    if record:
                        record["status"] = "modified-in-place"
                        manager.save()
            stats = ingest_directory(resolve_data_path(args.dir), dry_run=dry_run, **options)
        if dry_run and args.staged:
            stats.update(staged=True, version=args.version, switched=False)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"入库失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
