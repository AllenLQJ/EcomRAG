"""手动真实环境评测：python -m eval.check_retrieval。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from ingest.loaders import resolve_data_path


def evaluate(rows, retriever, *, refuse_threshold: float) -> dict:
    """仅评估 RAG 和 fallback；可注入假检索器，不加载配置或模型。"""
    rag, fallback = [], []
    for row in rows:
        route = row["expected_route"]
        if route not in {"rag", "fallback"}:
            continue
        question = row["question"]
        if callable(getattr(retriever, "retrieve_candidates", None)) and callable(getattr(retriever, "rerank_candidates", None)):
            candidates = retriever.retrieve_candidates([question])
            started = perf_counter()
            docs = retriever.rerank_candidates(question, candidates, top_k=5)[:5]
            rerank_ms = (perf_counter() - started) * 1000
        else:
            # 兼容已有仅实现 retrieve 的替身；不把检索总耗时冒充为重排耗时。
            docs = retriever.retrieve([question], question, top_k=5)[:5]
            rerank_ms = None
        if route == "rag":
            match = next(((rank, doc) for rank, doc in enumerate(docs, 1)
                          if Path(doc.source).name == row["expected_doc"]), None)
            rag.append({
                "question": question, "expected_doc": row["expected_doc"],
                "hit_at_5": match is not None,
                "rank": match[0] if match else None,
                "score": match[1].score if match else None,
                "rerank_ms": rerank_ms,
            })
        else:
            top_score = max((doc.score for doc in docs), default=None)
            fallback.append({
                "question": question, "top_score": top_score,
                "refused": top_score is None or top_score < refuse_threshold,
                "rerank_ms": rerank_ms,
            })
    total = len(rag)
    return {
        "rag": rag, "fallback": fallback, "rag_total": total,
        "hit_at_5": sum(item["hit_at_5"] for item in rag) / total if total else 0.0,
        "hit_at_1": sum(item["rank"] == 1 for item in rag) / total if total else 0.0,
        "refuse_threshold": refuse_threshold,
        "text_mode": getattr(retriever, "rerank_text_mode", None),
        "candidates": getattr(retriever, "candidate_limit", None),
    }


def print_report(report: dict) -> None:
    for item in report["rag"]:
        score = f'{item["score"]:.6f}' if item["score"] is not None else "-"
        ms = f'{item["rerank_ms"]:.3f}' if item["rerank_ms"] is not None else "-"
        print(f'[RAG] {item["question"]} expected_doc={item["expected_doc"]} '
              f'Top5={"命中" if item["hit_at_5"] else "未命中"} '
              f'rank={item["rank"] if item["rank"] is not None else "-"} rerank_score={score} rerank_ms={ms}')
    total = report["rag_total"]
    hits5 = sum(item["hit_at_5"] for item in report["rag"])
    hits1 = sum(item["rank"] == 1 for item in report["rag"])
    print(f'RAG 汇总：Hit@5={report["hit_at_5"]:.2%} ({hits5}/{total}) '
          f'Hit@1={report["hit_at_1"]:.2%} ({hits1}/{total})')
    threshold = report["refuse_threshold"]
    for item in report["fallback"]:
        score = f'{item["top_score"]:.6f}' if item["top_score"] is not None else "无结果"
        ms = f'{item["rerank_ms"]:.3f}' if item["rerank_ms"] is not None else "-"
        relation = "<" if item["refused"] else ">="
        comparison = f'{score} {relation} {threshold:g}' if item["top_score"] is not None else "无检索结果"
        print(f'[FALLBACK] {item["question"]} top_score={score} '
              f'REFUSE_THRESHOLD={threshold:g} ({comparison}) '
              f'是否会被拒答={"是" if item["refused"] else "否"} rerank_ms={ms}')
    print("REFUSE_THRESHOLD 默认 0.5；历史 parent 模式实测 RAG 最低 0.894、拒答类最高 0.261。"
          "重排输入或精度改变会改变分数分布，请分别用本工具重新校准阈值。")


def main(argv=None, *, settings=None) -> int:
    parser = argparse.ArgumentParser(description="检索评测：RAG Hit@5/Hit@1 与 fallback 拒答阈值")
    parser.add_argument("--eval-file", default="data/eval.jsonl")
    parser.add_argument("--rerank-text-mode", choices=("child", "parent"), help="仅测指定模式；省略时对比两种模式")
    parser.add_argument("--candidates", type=int, help="覆盖 RERANK_CANDIDATES，必须大于 0")
    args = parser.parse_args(argv)
    if args.candidates is not None and args.candidates <= 0:
        parser.error("--candidates 必须大于 0")
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    with resolve_data_path(args.eval_file).open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]

    from app.retrieval.embedder import BgeM3Embedder
    from app.retrieval.reranker import BgeReranker
    from app.retrieval.retriever import Retriever

    embedder = BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size)
    reranker = BgeReranker(resolve_data_path(settings.reranker_path), max_length=settings.rerank_max_length,
                           batch_size=settings.rerank_batch_size, fp16=settings.rerank_fp16)
    # 只预热一次并复用相同精度的模型，避免把加载成本算到第一种模式。
    reranker.rerank("店铺服务", ["店铺服务"])
    limit = args.candidates if args.candidates is not None else settings.rerank_candidates
    for mode in (args.rerank_text_mode,) if args.rerank_text_mode else ("child", "parent"):
        retriever = Retriever(None, embedder, reranker, uri=settings.milvus_uri, timeout_s=settings.milvus_timeout_s,
                              rerank_enabled=True, rerank_candidates=limit, rerank_max_chars=settings.rerank_max_chars,
                              rerank_text_mode=mode)
        try:
            print(f"rerank=on text_mode={mode} candidates={limit}（归一化分；固定开启重排，已预热）")
            print_report(evaluate(rows, retriever, refuse_threshold=settings.refuse_threshold))
        finally:
            if retriever.client is not None:
                retriever.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
