"""真实端到端评测，无硬门槛；evaluate 可离线注入假服务。"""
import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter

from app.contracts import AppError
from app.pipeline import run_chat
from app.schemas import ChatRequest
from app.services import Services
from ingest.loaders import resolve_data_path
from eval.metrics import percentile


def expected_doc_id(filename):
    # 与入库 loader 相同的稳定路径摘要；Source.doc_id 不等于源文件名。
    from ingest.loaders import load_document
    return load_document(resolve_data_path("data/docs") / filename).doc_id


async def evaluate(rows, *, settings, services, doc_ids=None):
    results, refused, errors, route_errors = [], [], [], []
    timings = defaultdict(list)
    totals = []
    doc_ids = {} if doc_ids is None else doc_ids
    for i, row in enumerate(rows):
        trace = []
        error_result = None
        started = perf_counter()
        try:
            result = await run_chat(ChatRequest(session_id=f"eval-{i}", message=row["question"], debug=True),
                                    settings=settings, services=services, trace_sink=trace)
        except AppError as exc:
            errors.append({"question": row["question"], "code": exc.code})
            route_errors.append({"question": row["question"], "expected_route": row["expected_route"],
                                 "route": "error", "error_code": exc.code})
            error_result = {"question": row["question"], "expected_route": row["expected_route"],
                            "route": "error", "route_correct": False, "expected_doc": row.get("expected_doc"),
                            "source_hit": False if row["expected_route"] == "rag" else None,
                            "error_code": exc.code, "is_error": True,
                            "answer": "", "sources": []}
            results.append(error_result)
            continue
        finally:
            total_ms = (perf_counter() - started) * 1000
            totals.append(total_ms)
            stage_ms = {}
            for item in trace:
                stage_ms[item.stage] = stage_ms.get(item.stage, 0) + item.ms
            for stage, ms in stage_ms.items():
                timings[stage].append(ms)
            # AppError 行在 except 内加入，continue 前也补齐实际已执行阶段和耗时。
            if error_result is not None:
                error_result.update(total_ms=total_ms, stage_ms=stage_ms)
        expected = row["expected_route"]
        correct = result.route == expected or expected == "cache" and result.route == "faq"
        if not correct:
            route_errors.append({"question": row["question"], "expected_route": expected, "route": result.route})
        match = None
        if expected == "rag":
            filename = row["expected_doc"]
            if filename not in doc_ids:
                doc_ids[filename] = expected_doc_id(filename)
            match = any(source.doc_id in {filename, doc_ids[filename]}
                        or Path(source.doc_id).name == filename for source in result.sources)
        results.append({"question": row["question"], "expected_route": expected, "route": result.route,
                        "route_correct": correct, "expected_doc": row.get("expected_doc"),
                        "source_hit": match, "error_code": None, "is_error": False,
                        "answer": result.answer,
                        "sources": [{"doc_id": source.doc_id, "title": source.title, "score": source.score}
                                    for source in result.sources],
                        "total_ms": total_ms, "stage_ms": stage_ms})
        if result.route == "fallback":
            refused.append(row["question"])
    rag = [row for row in results if row["source_hit"] is not None]
    rag_total = sum(row["expected_route"] == "rag" for row in rows)
    rag_hits = sum(row["source_hit"] for row in rag)
    return {"total": len(rows), "completed": len(results) - len(errors), "errors": errors,
            "error_distribution": dict(Counter(str(item["code"]) for item in errors)),
            "route_accuracy": sum(row["route_correct"] for row in results) / len(rows) if rows else 0.0,
            "rag_total": rag_total, "rag_source_hits": rag_hits,
            "rag_source_hit_rate": rag_hits / rag_total if rag_total else 0.0,
            "route_errors": route_errors,
            "stage_mean_ms": {stage: sum(values) / len(values) for stage, values in timings.items()},
            "stage_p50_ms": {stage: percentile(values, 50) for stage, values in timings.items()},
            "stage_p90_ms": {stage: percentile(values, 90) for stage, values in timings.items()},
            "total_p50_ms": percentile(totals, 50), "total_p90_ms": percentile(totals, 90),
            "refused_questions": refused, "results": results,
            "trace_enabled": True, "api_trace_allowed": settings.allow_trace,
            "note": "expected_route=cache 接受 cache 或首次运行的 faq；耗时使用内部 trace，独立于 ALLOW_TRACE，"
                    "阶段均值与 p50/p90 仅统计实际执行阶段（包含出错前已完成的阶段），"
                    "总耗时包含失败请求；分位数使用排序后线性插值，并行阶段耗时不能相加。"
                    "FAQ_THRESHOLD 默认 0.9：实测 FAQ 误命中最高 0.885，阶段 4 用测试集校准。"}


def print_report(report):
    print(f'总题数：{report["total"]}；完成：{report["completed"]}；错误：{len(report["errors"])}')
    distribution = "，".join(f"{code} ×{count}" for code, count in sorted(report["error_distribution"].items()))
    print("错误分布：" + (distribution or "无"))
    print(f'路由准确率：{report["route_accuracy"]:.2%}')
    print(f'RAG sources 命中率：{report["rag_source_hit_rate"]:.2%} '
          f'({report["rag_source_hits"]}/{report["rag_total"]})')
    print("各阶段平均耗时(ms)：" + (", ".join(f"{stage}={ms:.3f}" for stage, ms in
                                                report["stage_mean_ms"].items()) or "无 trace 数据"))
    for label, key in (("p50", "stage_p50_ms"), ("p90", "stage_p90_ms")):
        print(f"各阶段 {label}(ms)：" + (", ".join(f"{stage}={ms:.3f}" for stage, ms in report[key].items()) or "无"))
    if report["total_p50_ms"] is not None:
        print(f'总耗时(ms)：p50={report["total_p50_ms"]:.3f}，p90={report["total_p90_ms"]:.3f}')
    else:
        print("总耗时(ms)：无样本")
    print("路由错误的题目：")
    for row in report["route_errors"]:
        print(f'- {row["question"]} | 期望={row["expected_route"]} | 实际={row["route"]}')
    if not report["route_errors"]:
        print("无")


def main(argv=None, *, settings=None):
    parser = argparse.ArgumentParser(description="端到端路由、引用与耗时评测（无硬门槛）")
    parser.add_argument("--eval-file", default="data/eval.jsonl")
    parser.add_argument("--output", type=Path, help="将完整 JSON 写入指定文件")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    rows = [json.loads(line) for line in resolve_data_path(args.eval_file).read_text(encoding="utf-8").splitlines()
            if line.strip()]

    async def run():
        services = Services(settings)
        try:
            if settings.warmup_on_start:
                await services.warmup()
            report = await evaluate(rows, settings=settings, services=services)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print_report(report)
        finally:
            await services.aclose()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
