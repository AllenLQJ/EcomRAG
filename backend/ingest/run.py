from __future__ import annotations

import argparse
import json
import sys
from time import perf_counter
from typing import TYPE_CHECKING

from ingest.loaders import load_directory, resolve_data_path
from ingest.splitter import split_document

if TYPE_CHECKING:
    from app.config import Settings


def ingest_directory(directory, *, dry_run: bool = True, split_options: dict | None = None,
                     batch_size: int = 16, embedder=None, store=None) -> dict:
    """读取、切块并可选入库；dry-run 全程仅使用标准库。"""
    started = perf_counter()
    if batch_size <= 0:
        raise ValueError("编码批大小必须大于零")
    documents = load_directory(directory)
    if not documents:
        raise ValueError("目录中没有支持的文档")
    chunks = [chunk for document in documents
              for chunk in split_document(document, **(split_options or {}))]
    if not dry_run:
        if embedder is None or store is None:
            raise ValueError("实际入库必须提供编码器和存储客户端")
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            vectors = embedder.encode([chunk.child_text for chunk in batch])
            store.upsert(batch, vectors)
    return {
        "documents": len(documents),
        "parent_chunks": len({chunk.parent_id for chunk in chunks}),
        "child_chunks": len(chunks),
        "elapsed_s": round(perf_counter() - started, 4),
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识文档入库；默认不下载模型")
    parser.add_argument("--dir", required=True, help="文档目录，相对路径支持仓库根目录")
    parser.add_argument("--dry-run", action="store_true", help="只读取与切块，不连接服务")
    args = parser.parse_args(argv)
    try:
        if settings is None:
            # CLI 加载仓库配置；测试显式注入时不调用配置加载器。
            from app.config import get_settings

            settings = get_settings()
        options = {
            "batch_size": settings.embed_batch_size,
            "split_options": {
                "parent_max_chars": settings.parent_max_chars,
                "child_chunk_size": settings.child_chunk_size,
                "child_chunk_overlap": settings.child_chunk_overlap,
            },
        }
        if not args.dry_run:
            from app.retrieval.embedder import BgeM3Embedder
            from app.retrieval.store import MilvusStore

            options.update({
                "embedder": BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size),
                "store": MilvusStore(settings.milvus_uri, timeout_s=settings.milvus_timeout_s),
            })
        stats = ingest_directory(resolve_data_path(args.dir), dry_run=args.dry_run, **options)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"入库失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
