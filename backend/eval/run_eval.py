"""真实端到端评测，无硬门槛；evaluate 可离线注入假服务。"""
import argparse
import asyncio
import hashlib
import json
import platform
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

from pydantic import SecretStr

from app.contracts import AppError
from app.pipeline import run_chat
from app.schemas import ChatRequest
from app.services import Services
from ingest.loaders import EXTENSIONS, resolve_data_path
from ingest.eval_expectations import expected_documents
from eval.metrics import percentile

ANSWER_ROUTES = {"faq", "cache", "rag"}
REPORT_ROUTES = ("faq", "cache", "rag", "general", "fallback", "error")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = PROJECT_ROOT / "backend/app/llm/prompts"
SNAPSHOT_FIELDS = (
    "router_model", "generate_model", "rerank_provider", "rerank_enabled",
    "rerank_candidates", "rerank_top_k", "rerank_text_mode", "refuse_threshold",
    "remote_refuse_threshold", "remote_rag_context_min_score", "rag_context_min_score",
    "faq_threshold", "speculative_retrieval", "warmup_on_start",
)


def short_hash(data):
    return hashlib.sha256(data).hexdigest()[:12]


def prompt_hash(name):
    try:
        return short_hash((PROMPT_DIR / name).read_bytes())
    except OSError:
        return None


def git_commit():
    """Git 缺失、非仓库或超时均不阻断报告，也不导出 stderr。"""
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, check=True, timeout=2)
        value = result.stdout.strip()
        return value if re.fullmatch(r"[0-9a-fA-F]{4,40}", value) else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def host_only(value):
    if not isinstance(value, str):
        return None
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def build_meta(settings, *, row_count, eval_file=None, eval_bytes=None, label=""):
    # 白名单读取，绝不 dump Settings，也不解包任何 SecretStr。
    snapshot = {}
    for name in SNAPSHOT_FIELDS:
        value = getattr(settings, name)
        snapshot[name] = None if isinstance(value, SecretStr) else value
    snapshot["llm_base_url_host"] = host_only(settings.llm_base_url)
    snapshot["bge_m3_path"] = Path(settings.bge_m3_path).name
    if settings.rerank_provider == "remote":
        model = settings.rerank_remote_model
        snapshot["rerank_remote_model"] = None if isinstance(model, SecretStr) else model
        snapshot["rerank_remote_base_url_host"] = host_only(settings.remote_rerank_base_url)
    return {"git_commit": git_commit(), "generated_at": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(), "label": label,
            "eval_file": {"path": str(eval_file) if eval_file is not None else None,
                          "line_count": len(eval_bytes.splitlines()) if eval_bytes is not None else None,
                          "row_count": row_count,
                          "sha256": short_hash(eval_bytes) if eval_bytes is not None else None},
            "model_config": snapshot,
            "prompt_hashes": {name: prompt_hash(name) for name in ("generate.md", "router.md", "general.md")}}


def source_name_index(doc_ids):
    """用入库的相对路径摘要反查文件名；只枚举文件，不加载文档或模型。"""
    folder = resolve_data_path("data/docs")
    names = {}
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in EXTENSIONS:
            source = path.relative_to(folder).as_posix()
            names[hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]] = path.name
    names.update({doc_id: Path(filename).name for filename, doc_id in doc_ids.items()})
    return names


def case_details(row, result, names):
    allowed = expected_routes(row["expected_route"])
    route = result["route"]
    error_types = []
    if result["is_error"]:
        error_types.append("异常")
    else:
        if not result["route_correct"]:
            error_types.append("路由错")
        if route == "rag" and result["source_hit"] is False:
            error_types.append("来源未命中")
        if route == "fallback" and "fallback" not in allowed:
            error_types.append("误拒")
        elif route != "fallback" and allowed == {"fallback"}:
            error_types.append("误答")
    history = row.get("history", [])
    case = {"question": row["question"], "history_count": len(history) if isinstance(history, list) else 0,
            "expected": {"route": row["expected_route"], "doc": row.get("expected_doc"),
                         "should_refuse": True if allowed == {"fallback"} else
                                          None if "fallback" in allowed else False},
            "actual": {"route": route,
                       "sources": list(dict.fromkeys(names.get(source["doc_id"], Path(source["doc_id"]).name)
                                                     for source in result["sources"])),
                       "is_refusal": None if result["is_error"] else route == "fallback"},
            # cases 仅对实际 RAG 路由判引用；results 保持旧口径，供旧 RAG 汇总使用。
            "source_hit": result["source_hit"] if route == "rag" else None,
            "matched_by": route if route in ANSWER_ROUTES else None,
            "passed": not error_types, "error_types": error_types, "error": result.get("error"),
            "total_ms": result["total_ms"], "stage_ms": result["stage_ms"],
            "cache_hit": route == "cache", "faq_hit": route == "faq"}
    if "id" in row:
        case["id"] = row["id"]
    return case


def route_statistics(cases):
    groups = {}
    for route in REPORT_ROUTES:
        group = [case for case in cases if case["actual"]["route"] == route]
        times = [case["total_ms"] for case in group]
        groups[route] = {"total": len(group), "passed": sum(case["passed"] for case in group),
                         "total_p50_ms": percentile(times, 50), "total_p90_ms": percentile(times, 90)}
    return groups


def expected_routes(value):
    """旧的 cache 首次可命中 faq；answer 和列表只扩展可接受的路由集合。"""
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError("评测期望路由列表不能为空")
    routes = set()
    for route in values:
        if not isinstance(route, str) or route not in {*ANSWER_ROUTES, "general", "fallback", "answer"}:
            raise ValueError("无效的评测期望路由")
        routes.update(ANSWER_ROUTES if route == "answer" else {"cache", "faq"} if route == "cache" else {route})
    return routes


def expected_doc_id(filename):
    # 与入库 loader 相同的稳定路径摘要；Source.doc_id 不等于源文件名。
    from ingest.loaders import load_document
    return load_document(resolve_data_path("data/docs") / filename).doc_id


async def evaluate(rows, *, settings, services, doc_ids=None, eval_file=None, eval_bytes=None, label=""):
    results, refused, errors, route_errors = [], [], [], []
    timings = defaultdict(list)
    totals = []
    false_refusals, false_answers = [], []
    accepted = [expected_routes(row["expected_route"]) for row in rows]
    references = [expected_documents(row.get("expected_doc")) for row in rows]
    meta = build_meta(settings, row_count=len(rows), eval_file=eval_file, eval_bytes=eval_bytes, label=label)
    doc_ids = {} if doc_ids is None else doc_ids
    for i, row in enumerate(rows):
        allowed = accepted[i]
        has_reference = "rag" in allowed and bool(references[i])
        trace = []
        error_result = None
        started = perf_counter()
        try:
            result = await run_chat(ChatRequest(session_id=f"eval-{i}", message=row["question"], debug=True,
                                                history=row.get("history", [])),
                                    settings=settings, services=services, trace_sink=trace)
        except Exception as exc:
            # 未知异常也保留错题；不导出 message、repr、traceback 或校验输入。
            code = exc.code if isinstance(exc, AppError) and type(exc.code) is int else None
            category = "AppError" if isinstance(exc, AppError) else "Exception"
            errors.append({"question": row["question"], "code": code})
            route_errors.append({"question": row["question"], "expected_route": row["expected_route"],
                                 "route": "error", "error_code": code})
            error_result = {"question": row["question"], "expected_route": row["expected_route"],
                            "route": "error", "route_correct": False, "expected_doc": row.get("expected_doc"),
                            "source_hit": False if has_reference else None,
                            "error_code": code, "is_error": True,
                            "error": {"code": code, "category": category},
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
            # 异常行在 except 内加入，continue 前也补齐实际已执行阶段和耗时。
            if error_result is not None:
                error_result.update(total_ms=total_ms, stage_ms=stage_ms)
        expected = row["expected_route"]
        correct = result.route in allowed
        if not correct:
            route_errors.append({"question": row["question"], "expected_route": expected, "route": result.route})
        match = None
        if has_reference:
            filenames = references[i]
            for filename in filenames:
                if filename not in doc_ids:
                    doc_ids[filename] = expected_doc_id(filename)
            # 仅使用在线链路过滤后的实际响应来源，不计入未引用候选。
            match = result.route == "rag" and any(source.doc_id in {filename, doc_ids[filename]}
                        or Path(source.doc_id).name == filename
                        for source in result.sources for filename in filenames)
        results.append({"question": row["question"], "expected_route": expected, "route": result.route,
                        "route_correct": correct, "expected_doc": row.get("expected_doc"),
                        "source_hit": match, "error_code": None, "is_error": False,
                        "answer": result.answer,
                        "sources": [{"index": source.index, "doc_id": source.doc_id,
                                     "title": source.title, "score": source.score}
                                    for source in result.sources],
                        "total_ms": total_ms, "stage_ms": stage_ms})
        if result.route == "fallback":
            refused.append(row["question"])
            if allowed & ANSWER_ROUTES and "fallback" not in allowed:
                false_refusals.append(row["question"])
        elif allowed == {"fallback"}:
            false_answers.append(row["question"])
    rag = [row for row in results if row["source_hit"] is not None]
    rag_total = len(rag)
    rag_hits = sum(row["source_hit"] for row in rag)
    names = source_name_index(doc_ids)
    cases = [case_details(row, result, names) for row, result in zip(rows, results)]
    return {"meta": meta, "cases": cases, "failures": [case for case in cases if not case["passed"]],
            "route_stats": route_statistics(cases),
            "total": len(rows), "completed": len(results) - len(errors), "errors": errors,
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
            "false_refusals": false_refusals, "false_answers": false_answers,
            "trace_enabled": True, "api_trace_allowed": settings.allow_trace,
            "note": "expected_route=cache 接受 cache 或首次运行的 faq；耗时使用内部 trace，独立于 ALLOW_TRACE，"
                    "expected_route=answer 接受 faq/cache/rag，列表接受列出的任一路由；"
                    "缺少 expected_doc 的行跳过引用命中统计，带文档参考的失败行仍计入分母。"
                    "阶段均值与 p50/p90 仅统计实际执行阶段（包含出错前已完成的阶段），"
                    "总耗时包含失败请求；分位数使用排序后线性插值，并行阶段耗时不能相加。"
                    "RAG 引用命中仅基于响应中过滤后的 sources；无有效引用时包含最高分的一条兜底来源。"
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
    for title, key in (("误拒答（应答却 fallback）", "false_refusals"), ("误答（应拒却回答）", "false_answers")):
        print(f'{title}：{len(report[key])} 题')
        for question in report[key]:
            print(f'- {question}')
        if not report[key]:
            print("无")


def main(argv=None, *, settings=None):
    parser = argparse.ArgumentParser(description="端到端路由、引用与耗时评测（无硬门槛）")
    parser.add_argument("--eval-file", default="data/eval.jsonl")
    parser.add_argument("--output", type=Path, help="将完整 JSON 写入指定文件")
    parser.add_argument("--label", default="", help="写入报告 meta.label 的基线名称")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    eval_file = resolve_data_path(args.eval_file)
    eval_bytes = eval_file.read_bytes()
    rows = [json.loads(line) for line in eval_bytes.decode("utf-8").splitlines()
            if line.strip()]
    for row in rows:
        expected_routes(row["expected_route"])

    async def run():
        services = Services(settings)
        try:
            if settings.warmup_on_start:
                await services.warmup()
            report = await evaluate(rows, settings=settings, services=services, eval_file=eval_file,
                                    eval_bytes=eval_bytes, label=args.label)
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
