"""真实环境重排剖析；纯汇总函数可注入假 reranker，不依赖模型库。"""
import argparse
import math
from itertools import product
from statistics import median

from app.retrieval.reranker import rerank_text


def profile(question, candidates, reranker):
    """相同的 RRF 候选池上比较 27 组配置，每组两次、只保留第二次。"""
    rows, baseline = [], None
    for limit, max_chars, batch in product((20, 10, 6), (0, 600, 300), (8, 16, 32)):
        docs = candidates[:limit]
        texts = [doc.content[:max_chars] if max_chars else doc.content for doc in docs]
        lengths = [len(question) + len(text) for text in texts]
        measured = None
        for _ in range(2):
            measured = reranker.profile(question, texts, batch_size=batch)
        scores = measured["scores"]
        if len(scores) != len(docs):
            raise ValueError("剖析分数数量与候选数量不一致")
        top1 = docs[max(range(len(docs)), key=lambda index: scores[index])].doc_id if docs else None
        if not rows:
            baseline = top1
        rows.append({"candidate_limit": limit, "candidates": len(docs), "max_chars": max_chars,
                     "batch_size": batch, "length_min": min(lengths, default=0),
                     "length_median": median(lengths) if lengths else 0, "length_max": max(lengths, default=0),
                     "device": measured["device"], "dtype": measured["dtype"],
                     "tokenize_ms": measured["tokenize_ms"], "forward_ms": measured["forward_ms"],
                     "total_ms": measured["total_ms"], "top1_doc_id": top1,
                     "top1_matches_baseline": top1 == baseline})
    return {"baseline_top1_doc_id": baseline, "rows": rows}


def print_report(report):
    print("每组执行 2 次，丢弃首次预热；基准为候选20、字符不截断、batch8（token 上限仍取配置）。")
    print("长度为截断后的问题+父块字符数；前向同步设备计时，total 包含 tokenize、前向及其他开销。")
    print("device dtype limit actual chars batch length(min/median/max) tokenize_ms forward_ms total_ms Top1一致")
    for row in report["rows"]:
        print(f'{row["device"]} {row["dtype"]} {row["candidate_limit"]} {row["candidates"]} '
              f'{row["max_chars"]} {row["batch_size"]} '
              f'{row["length_min"]}/{row["length_median"]:g}/{row["length_max"]} '
              f'{row["tokenize_ms"]:.3f} {row["forward_ms"]:.3f} {row["total_ms"]:.3f} '
              f'{"是" if row["top1_matches_baseline"] else "否"}')


def compare_precision(question, candidates, fp32, fp16_factory, *, text_mode="child", max_chars=600, batch_size=16):
    """只为同一份文本测量两种精度；CPU 或 fp16 不可用时保留 fp32 结果。"""
    report = {"text_mode": text_mode, "candidates": len(candidates), "skipped": True}
    if not candidates:
        return {**report, "reason": "没有候选，跳过精度对比"}
    texts = [rerank_text(doc, text_mode, max_chars) for doc in candidates]

    def measure(reranker):
        for _ in range(2):
            result = reranker.profile(question, texts, batch_size=batch_size)
        if len(result["scores"]) != len(texts) or not all(math.isfinite(score) for score in result["scores"]):
            raise ValueError("精度对比分数数量或数值不合法")
        return result

    full = measure(fp32)
    report.update(device=full["device"], fp32_forward_ms=full["forward_ms"])
    if full["device"] != "mps":
        return {**report, "reason": "当前设备不是 mps，fp16 实验已跳过；CPU 始终使用 fp32"}
    try:
        half = measure(fp16_factory())
        if half["device"] != "mps" or half["dtype"] != "float16":
            return {**report, "reason": "未实际启用 MPS float16，已跳过精度对比"}
    except Exception as exc:
        # 不回显供应商/模型异常文本，避免原文进入应用日志或终端诊断。
        return {**report, "reason": f"MPS fp16 加载或推理失败（{type(exc).__name__}），已跳过"}
    differences = [abs(a - b) for a, b in zip(full["scores"], half["scores"])]
    top1 = lambda scores: candidates[max(range(len(candidates)), key=lambda i: scores[i])].doc_id
    return {**report, "skipped": False, "fp16_forward_ms": half["forward_ms"],
            "fp32_top1_doc_id": top1(full["scores"]), "fp16_top1_doc_id": top1(half["scores"]),
            "top1_matches": top1(full["scores"]) == top1(half["scores"]),
            "score_mean_abs_diff": sum(differences) / len(differences), "score_max_abs_diff": max(differences)}


def print_precision(report):
    print(f'精度对比 text_mode={report["text_mode"]} candidates={report["candidates"]}（各跑2次，丢弃首次）')
    if "fp32_forward_ms" in report:
        print(f'fp32 forward_ms={report["fp32_forward_ms"]:.3f}')
    if report["skipped"]:
        print(report["reason"])
    else:
        print(f'fp16 forward_ms={report["fp16_forward_ms"]:.3f} Top1一致={"是" if report["top1_matches"] else "否"} '
              f'score_mean_abs_diff={report["score_mean_abs_diff"]:.6f} '
              f'score_max_abs_diff={report["score_max_abs_diff"]:.6f}')


def main(argv=None, *, settings=None, retriever=None, reranker=None, fp16_factory=None):
    parser = argparse.ArgumentParser(description="比较重排候选数、父块字符截断和批大小的耗时及 Top1")
    parser.add_argument("question")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    owned = retriever is None
    if owned:
        from app.retrieval.embedder import BgeM3Embedder
        from app.retrieval.retriever import Retriever
        from ingest.loaders import resolve_data_path
        retriever = Retriever(None, BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size),
                              uri=settings.milvus_uri, timeout_s=settings.milvus_timeout_s, rerank_enabled=False)
    try:
        candidates = retriever.retrieve_candidates([args.question])
        if not candidates:
            print("没有检索候选，无法比较重排性能与 Top1。")
            return 1
        if reranker is None:
            from app.retrieval.reranker import BgeReranker
            from ingest.loaders import resolve_data_path
            reranker = BgeReranker(resolve_data_path(settings.reranker_path),
                                   max_length=settings.rerank_max_length, batch_size=settings.rerank_batch_size, fp16=False)
        if fp16_factory is None:
            def fp16_factory():
                from app.retrieval.reranker import BgeReranker
                from ingest.loaders import resolve_data_path
                return BgeReranker(resolve_data_path(settings.reranker_path), max_length=settings.rerank_max_length,
                                   batch_size=settings.rerank_batch_size, fp16=True)
        print(f"RRF 候选池={len(candidates)}，RERANK_MAX_LENGTH={settings.rerank_max_length}")
        print("下方 27 组网格使用 parent / fp32，便于与此前剖析结果比较。")
        print_report(profile(args.question, candidates, reranker))
        print_precision(compare_precision(args.question, candidates[:settings.rerank_candidates], reranker, fp16_factory,
                                          text_mode=settings.rerank_text_mode, max_chars=settings.rerank_max_chars,
                                          batch_size=settings.rerank_batch_size))
    finally:
        if owned and retriever.client is not None:
            retriever.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
