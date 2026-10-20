"""python -m eval.ragas_eval collect|score；与 HTTP 生命周期完全分离。"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from eval.ragas_adapter import not_applicable_category, to_sample
from eval.ragas_backend import EvaluationConfigurationError, EvaluationDependencyError, make_scorer
from eval.reports import DENOMINATOR_RULE, redact, safe_path, secret_safe_logs, summarize, write_report

METRICS = ("faithfulness", "response_relevancy", "context_precision", "context_recall")
# Judge 配置允许写在 backend/.env 中，但只读取下面这些白名单名字；
# LLM_API_KEY 等线上密钥和其他任何变量一律不读取、不返回。
JUDGE_ENV_NAMES = ("JUDGE_API_KEY", "JUDGE_MODEL", "JUDGE_BASE_URL",
                   "JUDGE_EMBEDDING_MODEL", "JUDGE_EMBEDDING_BASE_URL", "JUDGE_EXTRA_BODY")
# glm-* 模型默认关闭思考模式：思考内容会占用输出配额，导致 Judge 的 JSON 回答被截断、解析失败，并增加耗时与费用。
GLM_DEFAULT_EXTRA_BODY = {"thinking": {"type": "disabled"}}
JUDGE_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_judge_env(path=None):
    """只解析 .env 里 JUDGE_* 白名单变量；文件不存在时返回空字典。"""
    try:
        text = Path(JUDGE_ENV_FILE if path is None else path).read_text(encoding="utf-8")
    except OSError:
        return {}
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name not in JUDGE_ENV_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


@dataclass(frozen=True)
class ScoreConfig:
    judge_model: str
    judge_base_url: str
    judge_key_env: str
    max_samples: int = 50
    metrics: tuple[str, ...] = ("faithfulness", "response_relevancy")
    timeout_s: float = 60
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    judge_extra_body: dict | None = None
    api_key: str = field(default="", repr=False, compare=False)

    def __post_init__(self):
        if self.max_samples < 1 or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("max_samples 和 timeout_s 必须为正数")
        if not self.metrics or len(set(self.metrics)) != len(self.metrics) or set(self.metrics) - set(METRICS):
            raise ValueError("metrics 必须是支持的非重复指标名")


def resolve_extra_body(raw, model):
    """JUDGE_EXTRA_BODY 为 JSON 对象；未设置时 glm-* 模型默认关闭思考，其他模型不附加参数。"""
    raw = (raw or "").strip()
    if raw:
        try:
            value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except ValueError:
            raise ValueError("JUDGE_EXTRA_BODY 必须是合法的 JSON 对象；写 {} 表示不附加任何参数") from None
        if not isinstance(value, dict):
            raise ValueError("JUDGE_EXTRA_BODY 必须是 JSON 对象，不能是数组、字符串或 null")
        return value
    return dict(GLM_DEFAULT_EXTRA_BODY) if str(model).lower().startswith("glm-") else None


def settings_keys(settings):
    return tuple(value.get_secret_value() for name in
                 ("llm_api_key", "legacy_llm_api_key", "rerank_remote_api_key")
                 if (value := getattr(settings, name, None)) is not None
                 and hasattr(value, "get_secret_value"))


async def collect(rows, *, settings, services, eval_file=None, eval_bytes=None):
    with secret_safe_logs(settings_keys(settings)):
        return await _collect(rows, settings=settings, services=services, eval_file=eval_file, eval_bytes=eval_bytes)


async def _collect(rows, *, settings, services, eval_file=None, eval_bytes=None):
    # 复用 run_eval 的 Settings 白名单、commit、数据指纹、prompt 指纹。
    from eval.run_eval import build_meta, expected_routes
    from app.pipeline import run_chat
    from app.schemas import ChatRequest

    cases = []
    meta = build_meta(settings, row_count=len(rows), eval_file=eval_file, eval_bytes=eval_bytes,
                      label="ragas-collect")
    for i, row in enumerate(rows):
        expected_routes(row["expected_route"])
        case = {key: row[key] for key in ("id", "question", "reference", "answerable", "scenario", "risk",
                                         "expected_route", "expected_doc", "source_version", "reviewed") if key in row}
        started, captured, trace = perf_counter(), [], []
        try:
            await run_chat(ChatRequest(session_id=f"ragas-{i}", message=row["question"],
                                       history=row.get("history", [])),
                           settings=settings, services=services, trace_sink=trace, collector=captured.append)
            case.update(captured[0])
            mapped = to_sample(case)
            case.update(status=mapped["status"], reason=mapped["reason"])
        except Exception:
            # 与 run_eval 同口径，不导出潜在含密钥的异常消息、repr 或栈。
            case.update(status="error", reason="collection_error", route="error", answer="",
                        intent="unknown", refuse_reason="unknown",
                        history=row.get("history", []), sources=[], contexts=[])
        case["total_ms"] = (perf_counter() - started) * 1000
        case["stage_ms"] = {}
        for item in trace:
            case["stage_ms"][item.stage] = case["stage_ms"].get(item.stage, 0) + item.ms
        cases.append(case)
    return redact({"phase": "collect", "schema_version": 1, "meta": meta, "cases": cases,
                   "failures": [case for case in cases if case["status"] in {"error", "invalid"}],
                   "route_stats": dict(Counter(case["route"] for case in cases)),
                   "not_applicable_by_category": dict(Counter(not_applicable_category(case) for case in cases
                                                             if case["status"] == "not_applicable")),
                   "status_counts": dict(Counter(case["status"] for case in cases))}, settings_keys(settings))


def _entry(status, reason, *, eligible=False, attempted=False, value=None, elapsed_ms=0, usage=None):
    return {"status": status, "reason": reason, "eligible": eligible, "attempted": attempted,
            "value": value, "elapsed_ms": elapsed_ms, "usage": usage or {}}


def _judge_score(scorer, sample, metric):
    started = perf_counter()
    try:
        raw = scorer(sample, metric)
        raw = raw if isinstance(raw, dict) else {"value": raw}
        usage = {key: value for key, value in (raw.get("usage") or {}).items()
                 if key in {"input_tokens", "output_tokens", "total_tokens"}
                 and isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0}
        value = raw.get("value")
        if raw.get("status") in {"error", "invalid"}:
            result = _entry(raw["status"], "judge_" + raw["status"], usage=usage)
        elif (isinstance(value, bool) or not isinstance(value, (int, float))
              or not math.isfinite(value) or not 0 <= value <= 1):
            result = _entry("invalid", "non_finite_or_out_of_range_score", usage=usage)
        else:
            reason = raw.get("reason")
            result = _entry("valid", reason if isinstance(reason, str) else "metric_result",
                            value=float(value), usage=usage)
    except TimeoutError:
        result = _entry("error", "judge_timeout")
    except (ValueError, TypeError, KeyError):
        result = _entry("error", "judge_parse_error")
    except Exception:
        result = _entry("error", "judge_error")
    result.update(eligible=True, attempted=True, elapsed_ms=(perf_counter() - started) * 1000)
    return result


def score(collection, config, *, scorer=None, judge=None, embeddings=None):
    """scorer(sample, metric) 为无依赖替身入口；judge/embeddings 为 RAGAS 包装对象入口。"""
    with secret_safe_logs((config.api_key,)):
        return _score(collection, config, scorer=scorer, judge=judge, embeddings=embeddings)


def _score(collection, config, *, scorer=None, judge=None, embeddings=None):
    # 默认后端在任何评分前初始化；缺依赖必须清楚失败，不能伪装为每行 Judge 错误。
    if scorer is None:
        scorer = make_scorer(config, judge=judge, embeddings=embeddings)
    cases, selected = [], 0
    for i, collected in enumerate(collection["cases"]):
        mapped = to_sample(collected)
        sample = mapped["sample"]
        case = {key: collected.get(key) for key in ("id", "question", "reference", "answer", "route", "answerable")}
        case.update(scenario=str(collected.get("scenario", "unknown")), risk=str(collected.get("risk", "unknown")),
                    intent=collected.get("intent", "unknown"), refuse_reason=collected.get("refuse_reason", "unknown"),
                    index=i, scores={})
        within_limit = selected < config.max_samples
        if mapped["status"] == "applicable":
            selected += 1
        for metric in config.metrics:
            if mapped["status"] != "applicable":
                entry = _entry(mapped["status"], mapped["reason"])
            elif any(not isinstance(sample[key], str) or not sample[key].strip()
                     for key in ("user_input", "response")):
                entry = _entry("invalid", "missing_question_or_answer")
            elif metric.startswith("context_") and (
                    not isinstance(sample["reference"], str) or not sample["reference"].strip()):
                entry = _entry("invalid", "missing_reference")
            elif not within_limit:
                entry = _entry("skipped_limit", "max_samples", eligible=True)
            else:
                entry = _judge_score(scorer, sample, metric)
            case["scores"][metric] = entry
        statuses = {entry["status"] for entry in case["scores"].values()}
        case["status"] = next(status for status in
                              ("error", "invalid", "not_applicable", "skipped_limit", "valid") if status in statuses)
        cases.append(case)
    # 不复制采集正文或任意 meta 字段，只复制 run_eval 的安全元数据白名单。
    original = collection.get("meta", {})
    meta = {key: original.get(key) for key in ("git_commit", "eval_file", "model_config", "prompt_hashes")}
    meta.update(generated_at=datetime.now(timezone.utc).isoformat(), judge_model=config.judge_model,
                metrics=list(config.metrics), max_samples=config.max_samples,
                embedding_model=config.embedding_model, denominator_rule=DENOMINATOR_RULE)
    report = {"phase": "score", "schema_version": 1, "meta": meta, "cases": cases,
              "failures": [case for case in cases if case["status"] in {"error", "invalid"}],
              "route_stats": dict(Counter(case["route"] for case in cases)),
              "summary": summarize(cases, config.metrics)}
    return redact(report, (config.api_key,))


def main(argv=None, *, settings=None, services_factory=None, scorer=None, judge=None, embeddings=None):
    parser = argparse.ArgumentParser(description="离线两阶段 RAGAS 质量评估")
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect")
    collect_parser.add_argument("--eval-file", default="data/eval_ragas_gold.jsonl")
    collect_parser.add_argument("--out", required=True)
    score_parser = commands.add_parser("score")
    score_parser.add_argument("--in", dest="input_file", required=True)
    score_parser.add_argument("--out", required=True)
    # 三项都可省略：依次回落到环境变量、backend/.env 中的 JUDGE_MODEL / JUDGE_BASE_URL / JUDGE_API_KEY。
    for option in ("judge-model", "judge-base-url", "judge-key-env"):
        score_parser.add_argument("--" + option)
    score_parser.add_argument("--max-samples", type=int, default=50)
    score_parser.add_argument("--metrics", default="faithfulness,response_relevancy")
    score_parser.add_argument("--timeout-s", type=float, default=60)
    score_parser.add_argument("--embedding-model")
    score_parser.add_argument("--embedding-base-url")
    args = parser.parse_args(argv)
    secrets = ()
    try:
        # 在路径/配置校验前取得指定 key，确保这些错误的原因也可脱敏。
        if args.command == "score":
            file_values = load_judge_env()

            def pick(name, cli_value=None):
                return (cli_value or os.environ.get(name) or file_values.get(name) or "").strip()

            key_env = args.judge_key_env or "JUDGE_API_KEY"
            # 自定义变量名只读环境变量；只有白名单名字才允许回落到 .env 文件。
            key = os.environ.get(key_env, "") or (file_values.get(key_env, "") if key_env in JUDGE_ENV_NAMES else "")
            judge_model, judge_base_url = pick("JUDGE_MODEL", args.judge_model), pick("JUDGE_BASE_URL", args.judge_base_url)
            embedding_model = pick("JUDGE_EMBEDDING_MODEL", args.embedding_model) or None
            embedding_base_url = pick("JUDGE_EMBEDDING_BASE_URL", args.embedding_base_url) or None
            extra_body = resolve_extra_body(pick("JUDGE_EXTRA_BODY"), judge_model)
            secrets = (key,)
        elif settings is not None:
            secrets = settings_keys(settings)
        out = safe_path(args.out)
        if out.suffix != ".json":
            raise ValueError("--out 必须使用 .json 后缀")
        if args.command == "collect":
            from app.config import Settings
            from app.services import Services
            from ingest.loaders import resolve_data_path
            settings = Settings(_env_file=None) if settings is None else settings
            secrets = settings_keys(settings)
            path = safe_path(resolve_data_path(safe_path(args.eval_file)))
            data = path.read_bytes()
            rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]

            async def run():
                with secret_safe_logs(settings_keys(settings)):
                    services = (services_factory or Services)(settings)
                    try:
                        if settings.warmup_on_start:
                            await services.warmup()
                        return await collect(rows, settings=settings, services=services, eval_file=path, eval_bytes=data)
                    finally:
                        await services.aclose()
            write_report(asyncio.run(run()), out, secrets=settings_keys(settings))
        else:
            if not key.strip():
                raise ValueError("Judge key 环境变量为空：请设置该环境变量，或在 backend/.env 中写入 JUDGE_API_KEY")
            if not judge_model or not judge_base_url:
                raise ValueError("缺少 Judge 模型或地址：请传 --judge-model / --judge-base-url，"
                                 "或设置 JUDGE_MODEL / JUDGE_BASE_URL（环境变量或 backend/.env）")
            config = ScoreConfig(judge_model, judge_base_url, key_env,
                                 max_samples=args.max_samples, metrics=tuple(args.metrics.split(",")),
                                 timeout_s=args.timeout_s, embedding_model=embedding_model,
                                 embedding_base_url=embedding_base_url, judge_extra_body=extra_body,
                                 api_key=key)
            collection = json.loads(safe_path(args.input_file).read_text(encoding="utf-8"))
            if collection.get("phase") != "collect" or collection.get("schema_version") != 1:
                raise ValueError("score 需要 schema_version=1 的 collect 文件")
            report = score(collection, config, scorer=scorer, judge=judge, embeddings=embeddings)
            write_report(report, out, secrets=(key,))
    except EvaluationDependencyError as exc:
        parser.exit(2, redact(str(exc), secrets) + "\n")
    except (ValueError, EvaluationConfigurationError) as exc:
        parser.exit(2, redact(str(exc), secrets) + "\n")
    except Exception:
        # 无异常原文/命令回显，防止 SDK、配置验证或 URL 在错误输出中携带密钥。
        parser.exit(2, "离线评估失败：请检查输入文件、CLI 参数和评估依赖；未输出异常原文。\n")
    print("离线评估报告已写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
