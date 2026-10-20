"""CLI 公共入口；禁用 dotenv，默认只预览，显式 --yes 才产生写操作。"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext

from ingest.loaders import resolve_data_path
from ingest.eval_expectations import expected_documents, matches_expected_document


def cli_settings():
    from app.config import Settings
    return Settings(_env_file=None)


def smoke_check(version, settings, *, eval_file="data/eval_ragas_gold.jsonl", limit=20,
                retriever=None, client=None):
    if limit <= 0:
        raise ValueError("check-limit 必须大于零")
    rows = []
    with resolve_data_path(eval_file).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            expected = expected_documents(row.get("expected_doc"))
            if row.get("answerable") is True and expected:
                if not isinstance(row.get("question"), str) or not row["question"].strip():
                    raise ValueError("冒烟查询缺少 question")
                rows.append(row)
    # 文件顺序固定；从整个候选集等间距抽样，避免只测开头同一类问题。
    if len(rows) > limit:
        rows = [rows[i * len(rows) // limit] for i in range(limit)]
    if not rows:
        raise ValueError("没有 answerable=true 且有 expected_doc 的冒烟查询")
    if retriever is None:
        from app.retrieval.embedder import BgeM3Embedder
        from app.retrieval.retriever import Retriever
        retriever = Retriever(client, BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size),
                              uri=settings.milvus_uri, timeout_s=settings.milvus_timeout_s,
                              collection_name=version, rerank_enabled=False)
    # CLI 借用 manager.store 的客户端，检查只指定实体版本，不改变 Store 的集合名。
    # 线上 pymilvus 2.5.18 已实测：相同 URI 等参数的 MilvusClient 共享连接别名；
    # close 会断开该别名对应连接，使其他客户端随后抛 ConnectionNotExistException。
    # 因此不能按 Python 客户端对象是否新建来判断连接所有权，也不能在检查结束时 close。
    # 即使独立调用而未传 client，也保留可能共享的连接，由进程生命周期统一回收。
    results = []
    for row in rows:
        docs = retriever.retrieve([row["question"]], row["question"], top_k=5)[:5]
        expected = expected_documents(row["expected_doc"], required=True)
        any_hit = any(matches_expected_document(doc, expected) for doc in docs)
        primary_hit = any(matches_expected_document(doc, expected[:1]) for doc in docs)
        results.append({"question": row["question"], "expected_doc": row["expected_doc"],
                        "hit5": any_hit, "any_hit": any_hit, "primary_hit": primary_hit})
    return {"hit5": sum(row["hit5"] for row in results) / len(results),
            "primary_hit5": sum(row["primary_hit"] for row in results) / len(results),
            "queries": len(results), "results": results}


def command(kind, argv=None, *, settings=None, store=None):
    parser = argparse.ArgumentParser(description=f"知识库 {kind}；默认只读/预览，写操作需 --yes")
    parser.add_argument("--alias", default="kb_chunks")
    parser.add_argument("--state-dir", help="发布状态目录；同一 URI/alias 所有发布命令必须共享")
    parser.add_argument("--yes", action="store_true")
    if kind == "switch":
        parser.add_argument("--to", required=True)
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--min-hit5", type=float, default=0.9)
        parser.add_argument("--min-primary-hit5", type=float, default=None,
                            help="可选主文档 Hit@5 门禁，须配合 --check")
        parser.add_argument("--eval-file", default="data/eval_ragas_gold.jsonl")
        parser.add_argument("--check-limit", type=int, default=20)
    if kind == "drop":
        parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        settings = settings or cli_settings()
        from app.retrieval.store import MilvusStore
        from ingest.versioning import VersionManager, validate_check_thresholds, validate_check_report
        if kind == "switch":
            validate_check_thresholds(args.min_hit5, args.min_primary_hit5)
            if args.min_primary_hit5 is not None and not args.check:
                raise ValueError("--min-primary-hit5 必须配合 --check")
        store = store or MilvusStore(settings.milvus_uri, timeout_s=settings.milvus_timeout_s)
        manager = VersionManager(store, alias=args.alias, state_dir=args.state_dir)
        with manager.locked() if args.yes else nullcontext():
            if kind == "list":
                result = manager.listing()
            elif kind == "migrate":
                result = manager.migrate(yes=args.yes)
            elif kind == "drop":
                result = manager.drop(args.version, yes=args.yes)
            else:
                check = None
                if args.check:
                    # 显式 --check 允许读检索/本地编码，即使未给 --yes 也不切换。
                    def check(version):
                        report = smoke_check(version, settings, eval_file=args.eval_file, limit=args.check_limit,
                                             client=manager.store._connect())
                        print(json.dumps({"check": report}, ensure_ascii=False), flush=True)
                        return report
                    if not args.yes:
                        manager.verify(args.to)
                        validate_check_report(check(args.to), args.min_hit5, args.min_primary_hit5)
                result = manager.switch(args.to, yes=args.yes, check=check, min_hit5=args.min_hit5,
                                        min_primary_hit5=args.min_primary_hit5)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # 包括 SDK 异常；不重试写入、不把异常解释成 alias 不存在。
        print(f"知识库 {kind} 失败：{exc}", file=sys.stderr)
        return 1
