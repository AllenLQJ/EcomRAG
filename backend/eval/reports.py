"""RAGAS 报告：逐指标分母、失败可见、汇总不含上下文全文。"""
import json
import logging
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from statistics import mean, median

from eval.ragas_adapter import not_applicable_category


DENOMINATOR_RULE = (
    "保留全部采集行；仅实际 rag 且有上下文的前 max_samples 行进入评分预算。"
    "FAQ/cache/general/sensitive/fallback 为 not_applicable；超限为 skipped_limit。"
    "逐指标校验输入；缺失 reference 的 context 指标为 invalid。"
    "error/invalid/NaN 均单列且不记零；均值/中位数只以 valid 为分母，空集为 null。"
)


def redact(value, secrets=()):
    if isinstance(value, str):
        for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    return value


def _evaluation_logger(name):
    return name in {"eval", "ragas"} or name.startswith(("eval.", "ragas."))


class _SecretFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = tuple(secrets)

    def filter(self, record):
        if not _evaluation_logger(record.name):
            return True
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        # 异常文本/栈可能由 formatter 稍后拼接，评估不记录它们。
        record.exc_info = record.exc_text = record.stack_info = None
        return True


class _NamespaceFilterHandler(logging.Handler):
    """让新建子 logger 的传播记录也经过 Filter（祖先 logger 的 Filter 不继承）。"""
    def emit(self, record):
        # 记录继续按原日志链传播；没有实际处理器时保留 logging.lastResort 行为。
        logger = logging.getLogger(record.name)
        first_guard = None
        while logger is not None:
            if any(not isinstance(handler, _NamespaceFilterHandler) for handler in logger.handlers):
                return
            if first_guard is None and logger.handlers:
                first_guard = logger.handlers[0]
            logger = logger.parent if logger.propagate else None
        if self is first_guard and logging.lastResort is not None and record.levelno >= logging.lastResort.level:
            logging.lastResort.handle(record)


@contextmanager
def secret_safe_logs(secrets):
    """只临时过滤 eval/ragas 命名空间，不修改全局日志工厂或无关 logger。"""
    namespaces = [logging.getLogger(name) for name in ("eval", "ragas")]
    loggers = [logger for name, logger in list(logging.Logger.manager.loggerDict.items())
               if isinstance(logger, logging.Logger) and _evaluation_logger(name)]
    redactor = _SecretFilter(secrets)
    # 已有子 logger 的私有 handler 也要先脱敏，不能依赖向上冒泡。
    targets = [*loggers, *{handler for logger in loggers for handler in logger.handlers}]
    guards = []
    try:
        for target in targets:
            target.addFilter(redactor)
        for logger in namespaces:
            handler = _NamespaceFilterHandler()
            handler.addFilter(redactor)
            logger.addHandler(handler)
            guards.append((logger, handler))
        yield
    finally:
        for logger, handler in guards:
            logger.removeHandler(handler)
            handler.removeFilter(redactor)
            handler.close()
        for target in targets:
            target.removeFilter(redactor)


def safe_path(value):
    path = Path(value)
    if any(part.startswith(".env") for part in path.resolve().parts):
        raise ValueError("评估禁止读写 .env* 文件")
    return path


def metric_statistics(cases, metrics):
    result = {}
    for metric in metrics:
        entries = [case["scores"][metric] for case in cases]
        counts = Counter(entry["status"] for entry in entries)
        values = [entry["value"] for entry in entries if entry["status"] == "valid"]
        result[metric] = {
            "total": len(entries), "scorable": sum(entry["eligible"] for entry in entries),
            "attempted": sum(entry["attempted"] for entry in entries),
            **{status: counts[status] for status in
               ("valid", "error", "invalid", "not_applicable", "skipped_limit")},
            "mean": mean(values) if values else None, "median": median(values) if values else None,
        }
    return result


def summarize(cases, metrics):
    groups = {}
    for field in ("scenario", "risk"):
        groups[field] = {
            label: metric_statistics([case for case in cases if case[field] == label], metrics)
            for label in sorted({case[field] for case in cases})}
    usage_entries = [score for case in cases for score in case["scores"].values() if score["attempted"]]
    tokens = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        values = [entry["usage"][field] for entry in usage_entries if field in entry["usage"]]
        tokens[field] = sum(values) if values else None
    return {
        "total": len(cases), "status_counts": dict(Counter(case["status"] for case in cases)),
        "not_applicable_by_route": dict(Counter(case["route"] for case in cases
                                               if case["status"] == "not_applicable")),
        "not_applicable_by_category": dict(Counter(not_applicable_category(case) for case in cases
                                                  if case["status"] == "not_applicable")),
        "metrics": metric_statistics(cases, metrics), "groups": groups,
        "failure_categories": dict(Counter(
            entry.get("diagnostics", {}).get("category", entry["reason"])
            for case in cases for entry in case["scores"].values() if entry["status"] in {"error", "invalid"})),
        "failure_categories_by_metric": {
            metric: dict(Counter(case["scores"][metric].get("diagnostics", {}).get(
                "category", case["scores"][metric]["reason"])
                for case in cases if case["scores"][metric]["status"] in {"error", "invalid"}))
            for metric in metrics},
        "attempt_failure_categories": dict(Counter(
            failure["category"] for entry in usage_entries for failure in entry.get("attempt_failures", []))),
        "judge_usage": {**tokens, "metric_calls": len(usage_entries),
                        "retries": sum(entry.get("retries", 0) for entry in usage_entries),
                        "token_usage_available_calls": sum(bool(entry["usage"]) for entry in usage_entries),
                        "token_usage_missing_calls": sum(not entry["usage"] for entry in usage_entries),
                        "elapsed_ms": sum(entry["elapsed_ms"] for entry in usage_entries),
                        "note": "逐指标调用墙钟耗时含重试和退避；token 仅含最终尝试可得用量，不含失败尝试，可能含嵌入调用，不推算。"},
        "lowest_samples": {
            metric: sorted((case for case in cases if case["scores"][metric]["status"] == "valid"),
                           key=lambda case: case["scores"][metric]["value"])[:10]
            for metric in metrics},
    }


def markdown_report(report):
    # JSON 代码块保留完整逐行详情与分组，同时不受问题中的 Markdown 语法影响。
    summary = report["summary"]
    lines = ["# 离线 RAGAS 质量评估", "", DENOMINATOR_RULE, "",
             f"总样本：{summary['total']}；分类：{json.dumps(summary['status_counts'], ensure_ascii=False)}", "",
             "| 指标 | 可评分 | 已尝试 | 有效 | 错误 | 无效 | 不适用 | 超限 | 均值 | 中位数 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for metric, stats in summary["metrics"].items():
        cells = [metric] + [stats[key] for key in (
            "scorable", "attempted", "valid", "error", "invalid", "not_applicable", "skipped_limit", "mean", "median")]
        lines.append("| " + " | ".join("—" if cell is None else str(cell) for cell in cells) + " |")
    # 转义反引号以免样本正文提前结束报告代码块。
    detail = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).replace("`", "\\u0060")
    lines.extend(["", "失败类别：" + json.dumps(summary.get("failure_categories", {}), ensure_ascii=False),
                  "", "## 元数据、分组、低分样本与逐行详情", "", "```json", detail, "```", ""])
    return "\n".join(lines)


def write_report(report, out, *, secrets=()):
    out = safe_path(out)
    if out.suffix != ".json":
        raise ValueError("--out 必须使用 .json 后缀（score 同时生成同名 .md）")
    report = redact(report, secrets)
    content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    if report["phase"] == "score":
        out.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
