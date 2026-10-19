"""手动真实环境评测：python -m eval.check_retrieval。"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from pathlib import Path
from time import perf_counter

from ingest.loaders import resolve_data_path
from app.retrieval.rewrite import merge_reranked, needs_rewrite_rerank


async def invoke(function, *args, **kwargs):
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def evaluate(rows, retriever, *, refuse_threshold: float | None) -> dict:
    return asyncio.run(evaluate_async(rows, retriever, refuse_threshold=refuse_threshold))


async def evaluate_async(rows, retriever, *, refuse_threshold: float | None) -> dict:
    """仅评估 RAG 和 fallback；可注入假检索器，不加载配置或模型。"""
    rag, fallback = [], []
    for row in rows:
        route = row["expected_route"]
        if route not in {"rag", "fallback"}:
            continue
        question = row["question"]
        if callable(getattr(retriever, "retrieve_candidates", None)) and callable(getattr(retriever, "rerank_candidates", None)):
            candidates = await invoke(retriever.retrieve_candidates, [question])
            started = perf_counter()
            docs = (await invoke(retriever.rerank_candidates, question, candidates, top_k=5))[:5]
            # 可在 --eval-file 的行里附 rewritten_query；共用在线判断，少量增字也补充重排。
            # 仅标点/空格/大小写变化视为相同，其他排除条件仍由共用函数处理；不调用 LLM。
            rewritten = row.get("rewritten_query", question)
            if (refuse_threshold is not None
                    and not any(getattr(doc, "score_provider", None) == "fallback_rrf" for doc in docs)
                    and needs_rewrite_rerank(question, rewritten, docs, enabled=True, threshold=refuse_threshold)):
                revised = await invoke(retriever.rerank_candidates, rewritten, candidates, top_k=5)
                if any(getattr(doc, "score_provider", None) == "fallback_rrf" for doc in revised):
                    docs = revised
                else:
                    providers = {getattr(doc, "score_provider", None) for doc in (*docs, *revised)} - {None}
                    docs = revised if len(providers) > 1 else merge_reranked(docs, revised, 5)
            rerank_ms = (perf_counter() - started) * 1000
        else:
            # 兼容已有仅实现 retrieve 的替身；不把检索总耗时冒充为重排耗时。
            docs = (await invoke(retriever.retrieve, [question], question, top_k=5))[:5]
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
                "refused": ((top_score is None or top_score < refuse_threshold)
                            if refuse_threshold is not None else None),
                "rerank_ms": rerank_ms,
            })
    total = len(rag)
    rag_min = min((item["score"] for item in rag if item["score"] is not None), default=None)
    fallback_max = max((item["top_score"] for item in fallback if item["top_score"] is not None), default=None)
    return {
        "rag": rag, "fallback": fallback, "rag_total": total,
        "rag_min_score": rag_min, "fallback_max_score": fallback_max,
        "suggested_threshold": (rag_min + fallback_max) / 2 if rag_min is not None and fallback_max is not None else None,
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
        if threshold is None:
            print(f'[FALLBACK] {item["question"]} top_score={score} '
                  f'云端分数不用于拒答（阈值未设） rerank_ms={ms}')
            continue
        relation = "<" if item["refused"] else ">="
        comparison = f'{score} {relation} {threshold:g}' if item["top_score"] is not None else "无检索结果"
        print(f'[FALLBACK] {item["question"]} top_score={score} '
              f'REFUSE_THRESHOLD={threshold:g} ({comparison}) '
              f'是否会被拒答={"是" if item["refused"] else "否"} rerank_ms={ms}')
    note = "；云端分数无区分度时，该建议仅供参考，不能作为有效拒答依据" if threshold is None else ""
    print(f'RAG 最低分={report["rag_min_score"]} / 拒答最高分={report["fallback_max_score"]} / '
          f'建议阈值={report["suggested_threshold"]}（两者中点，仅供参考）{note}')
    if report["rag_min_score"] is not None and report["fallback_max_score"] is not None and report["rag_min_score"] <= report["fallback_max_score"]:
        print("分数分布重叠，单一阈值无法完全区分；请检查未命中题与数据集。")



def main(argv=None, *, settings=None) -> int:
    parser = argparse.ArgumentParser(description="检索评测：RAG Hit@5/Hit@1 与 fallback 拒答阈值")
    parser.add_argument("--rerank-provider", choices=("local", "remote"), help="默认使用 RERANK_PROVIDER")
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

    provider = args.rerank_provider or settings.rerank_provider
    settings = settings.model_copy(update={"rerank_provider": provider})
    embedder = BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size)

    def local_model():
        return BgeReranker(resolve_data_path(settings.reranker_path), max_length=settings.rerank_max_length,
                           batch_size=settings.rerank_batch_size, fp16=settings.rerank_fp16)

    limit = args.candidates if args.candidates is not None else settings.rerank_candidates
    modes = (args.rerank_text_mode,) if args.rerank_text_mode else ("child", "parent")

    def build(reranker, mode):
        return Retriever(None, embedder, reranker, uri=settings.milvus_uri, timeout_s=settings.milvus_timeout_s,
                         rerank_enabled=True, rerank_candidates=limit, rerank_max_chars=settings.rerank_max_chars,
                         rerank_text_mode=mode)

    def close(retriever):
        if retriever.client is not None:
            retriever.client.close()

    if provider == "local":
        reranker = local_model()
        reranker.rerank("店铺服务", ["店铺服务"])
        for mode in modes:
            retriever = build(reranker, mode)
            try:
                print(f"rerank=on provider=local text_mode={mode} candidates={limit}（归一化分；已预热）")
                print_report(evaluate(rows, retriever, refuse_threshold=settings.refuse_threshold))
            finally:
                close(retriever)
    else:
        async def run_remote():
            from app.retrieval.remote_reranker import RemoteReranker
            reranker = RemoteReranker(settings)
            try:
                for mode in modes:
                    retriever = build(reranker, mode)
                    # 校准必须拿到真实远程分数，避免把回退分用于云端阈值。
                    retriever.rerank_fallback = "error"
                    retriever.rerank_candidates = retriever.arerank_candidates
                    try:
                        print(f"rerank=on provider=remote text_mode={mode} candidates={limit}（分数原样使用）")
                        print_report(await evaluate_async(rows, retriever, refuse_threshold=settings.rerank_refuse_threshold))
                    finally:
                        close(retriever)
            finally:
                await reranker.aclose()
        asyncio.run(run_remote())
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
