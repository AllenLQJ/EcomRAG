"""HTTP 压测入口；不加载应用、模型或环境文件。网络可注入 MockTransport。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import random
import re
from time import perf_counter
from typing import Iterable
import unicodedata
from uuid import uuid4

import httpx

from eval.metrics import percentile

DEFAULT_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_QUESTIONS = Path(__file__).resolve().parents[2] / "data/eval_colloquial.jsonl"
FALLBACK_QUESTIONS = (
    "店铺支持哪些付款方式？", "订单多久发货？", "包邮条件是什么？", "可以修改收货地址吗？",
    "如何申请退货？", "商品保修多久？", "能开电子发票吗？", "积分怎么使用？",
    "台灯有哪些亮度档位？", "耳机续航多久？",
)
ROUTES = ("faq", "cache", "rag", "general", "fallback")
STAGES = ("router", "rerank", "generate", "faq", "retrieve")
PROVIDERS = ("local", "remote", "fallback_rrf", "fallback_local")
LIMIT_CODES = (42901, 42902)
RATE_LIMIT_NOTICE = "若 RATE_LIMIT_PER_MIN / RATE_LIMIT_IP_ENABLED 未放开，高并发会触发 42901/42902"
HISTORY = (
    {"role": "user", "content": "我想了解店铺的商品与售后。"},
    {"role": "assistant", "content": "请问您想了解哪方面？"},
)


def safe_path(value: str | Path) -> Path:
    """拒绝环境文件，包括通过符号链接指向的环境文件。"""
    path = Path(value)
    if any(part.startswith(".env") for part in path.parts):
        raise ValueError("不得读写 .env* 路径")
    if any(part.startswith(".env") for part in path.resolve().parts):
        raise ValueError("不得读写 .env* 路径")
    return path


def _number(kind):
    def convert(value):
        try:
            result = kind(value)
            if not math.isfinite(result):
                raise ValueError
            return result
        except (ValueError, OverflowError):
            raise argparse.ArgumentTypeError("需要有限数值") from None
    return convert


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="电商客服并发压测（只访问指定 base-url）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--concurrency", type=_number(int), default=10)
    parser.add_argument("--requests", type=_number(int))
    parser.add_argument("--mode", choices=("json", "stream", "both"), default="json")
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--history-ratio", type=_number(float), default=0.0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--timeout", type=_number(float), default=90.0)
    parser.add_argument("--ramp", type=_number(float), default=0.0)
    parser.add_argument("--output")
    parser.add_argument("--seed", type=_number(int), default=0)
    parser.add_argument("--i-know-this-is-not-localhost", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 200:
        parser.error("--concurrency 必须在 1–200 之间")
    args.requests = args.concurrency if args.requests is None else args.requests
    if not 1 <= args.requests <= 5000:
        parser.error("--requests 必须在 1–5000 之间")
    if not 0 <= args.history_ratio <= 1:
        parser.error("--history-ratio 必须在 0–1 之间")
    if args.timeout <= 0 or args.ramp < 0:
        parser.error("--timeout 必须大于 0，--ramp 必须非负")
    try:
        url = httpx.URL(args.base_url)
        # 不允许凭据、查询串或 fragment 随 URL 进入 httpx 的诊断日志。
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or url.query or url.fragment):
            raise ValueError
    except (httpx.InvalidURL, ValueError):
        parser.error("--base-url 必须是无凭据、查询串和 fragment 的 http/https 地址")
    if url.host not in {"127.0.0.1", "localhost"} and not args.i_know_this_is_not_localhost:
        parser.error("非本机主机必须显式传 --i-know-this-is-not-localhost；请确认目标授权")
    args.base_url = str(url).rstrip("/")
    try:
        args.questions = safe_path(args.questions)
        if args.output:
            args.output = safe_path(args.output)
    except (ValueError, OSError):
        parser.error("文件路径无效；不得读写 .env* 路径")
    return args


def load_questions(path: Path) -> list[str]:
    path = safe_path(path)
    if not path.exists():
        return list(FALLBACK_QUESTIONS)
    questions = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            question = row.get("question") if isinstance(row, dict) else None
            if isinstance(question, str) and question.strip():
                questions.append(question.strip())
    return list(dict.fromkeys(questions)) or list(FALLBACK_QUESTIONS)


def question_preview(question: str) -> str:
    # 在截取前脱敏，避免把密钥前缀或凭据片段写入报告。
    question = re.sub(r"(?i)\b(?:sk-|bearer\s+)[\w.\-]+", "[redacted]", question)
    question = re.sub(
        r"(?i)(?:api[_-]?key|(?:access|secret|private)[_-]?key|secret|password|token|密钥)"
        r"\s*[\"']?\s*[:=：]\s*[\"']?[^\s,;\"'}]+",
        "[redacted]", question,
    )
    return "".join(char if char.isprintable() else " " for char in question)[:20]


@dataclass(frozen=True)
class RequestSpec:
    index: int
    mode: str
    question: str
    has_history: bool
    session_id: str


def build_requests(args, questions: list[str]) -> list[RequestSpec]:
    if not questions:
        questions = list(FALLBACK_QUESTIONS)
    # 使用精确数量再打乱；比例按最接近整数（.5 向上）取整。
    history_indices = set(random.Random(args.seed).sample(
        range(args.requests), math.floor(args.requests * args.history_ratio + 0.5)))
    run_id = uuid4().hex
    return [RequestSpec(
        i, ("json" if i % 2 == 0 else "stream") if args.mode == "both" else args.mode,
        questions[i % len(questions)], i in history_indices, f"loadtest-{run_id}-{i}",
    ) for i in range(args.requests)]


def parse_sse_lines(lines: Iterable[str]):
    """纯函数：消费已解码的 SSE 行；空行分派事件，注释/未知事件忽略。"""
    event, data = "message", []
    for line in lines:
        if line == "":
            if data and event in {"meta", "status", "delta", "done", "error"}:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ValueError("非法 SSE 数据结构")
                yield event, payload
            event, data = "message", []
        elif not line.startswith(":"):
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                event = value
            elif key == "data":
                data.append(value)


@dataclass
class RequestResult:
    index: int
    mode: str
    question_preview: str
    has_history: bool
    success: bool = False
    http_status: int | None = None
    code: int | None = None
    error_kind: str | None = None
    route: str | None = None
    latency_ms: float = 0.0
    first_delta_ms: float | None = None
    complete_answer_ms: float | None = None
    answer_length: int = 0
    stages_ms: dict = field(default_factory=dict)
    rerank_providers: dict = field(default_factory=dict)


def business_code(payload) -> int | None:
    value = payload.get("code") if isinstance(payload, dict) else None
    return value if type(value) is int and value != 0 else None


def apply_answer(result: RequestResult, payload: dict, debug: bool):
    if not isinstance(payload, dict):
        raise ValueError("响应不是对象")
    if business_code(payload) is not None:
        result.code = business_code(payload)
        result.error_kind = "business_error"
        return
    if not isinstance(payload.get("answer"), str) or payload.get("route") not in ROUTES:
        raise ValueError("响应缺少答案或合法路由")
    result.answer_length = len(payload["answer"])
    result.route = payload["route"]
    if debug:
        trace = payload.get("trace")
        for item in trace if isinstance(trace, list) else []:
            if not isinstance(item, dict):
                continue
            stage, ms = item.get("stage"), item.get("ms")
            if (stage in STAGES and type(ms) in {int, float} and math.isfinite(ms) and ms >= 0):
                result.stages_ms[stage] = result.stages_ms.get(stage, 0) + ms
            if stage == "rerank" and item.get("provider") in PROVIDERS:
                provider = item["provider"]
                result.rerank_providers[provider] = result.rerank_providers.get(provider, 0) + 1
    result.success = True


async def send_request(client: httpx.AsyncClient, args, spec: RequestSpec) -> RequestResult:
    result = RequestResult(spec.index, spec.mode, question_preview(spec.question), spec.has_history)
    payload = dict(session_id=spec.session_id, message=spec.question, debug=args.debug,
                   history=list(HISTORY) if spec.has_history else [])
    started = perf_counter()
    endpoint = "/api/v1/chat/stream" if spec.mode == "stream" else "/api/v1/chat"
    try:
        # httpx 的 read timeout 只是单次读取上限；另加整个请求的墙钟预算。
        async with asyncio.timeout(args.timeout):
            async with client.stream("POST", args.base_url + endpoint, json=payload) as response:
                result.http_status = response.status_code
                if not response.is_success:
                    await response.aread()
                    try:
                        result.code = business_code(response.json())
                    except ValueError:
                        pass
                    result.error_kind = "http_error"
                elif spec.mode == "json":
                    await response.aread()
                    apply_answer(result, response.json(), args.debug)
                else:
                    if response.headers.get("content-type", "").split(";")[0].strip() != "text/event-stream":
                        raise ValueError("响应不是 SSE")
                    frame = []
                    terminal = False
                    # httpx 增量解码 UTF-8、CR/LF，处理字节与行的跨块边界。
                    async for line in response.aiter_lines():
                        frame.append(line)
                        if line:
                            continue
                        for event, data in parse_sse_lines(frame):
                            if event == "delta":
                                text = data.get("text")
                                if not isinstance(text, str):
                                    raise ValueError("非法 delta")
                                if text and result.first_delta_ms is None:
                                    result.first_delta_ms = (perf_counter() - started) * 1000
                                result.answer_length += len(text)
                            elif event == "done":
                                apply_answer(result, data, args.debug)
                                if result.success:
                                    result.complete_answer_ms = (perf_counter() - started) * 1000
                                terminal = True
                            elif event == "error":
                                result.code = business_code(data)
                                result.error_kind = "business_error"
                                terminal = True
                        frame = []
                        if terminal:
                            break
                    if not terminal:
                        raise ValueError("SSE 缺少 done/error")
    except (TimeoutError, httpx.TimeoutException):
        result.success, result.error_kind = False, "timeout"
    except httpx.HTTPError:
        result.success, result.error_kind = False, "transport_error"
    except (ValueError, TypeError, UnicodeError):
        result.success, result.error_kind = False, "protocol_error"
    result.latency_ms = (perf_counter() - started) * 1000
    return result


def latency_stats(values):
    values = list(values)
    return {"count": len(values), "min": min(values) if values else None,
            **{f"p{p}": percentile(values, p) for p in (50, 90, 95, 99)},
            "max": max(values) if values else None}


def summarize(results: list[RequestResult], wall_seconds: float, *, debug=False) -> dict:
    successful = [row for row in results if row.success]
    failed = [row for row in results if not row.success]
    limited = sum(row.code in LIMIT_CODES for row in failed)
    report = dict(
        completed=len(results), succeeded=len(successful), failed=len(failed),
        service_limited=limited, backend_failures=len(failed) - limited,
        wall_seconds=wall_seconds,
        throughput_success_per_second=len(successful) / wall_seconds if wall_seconds > 0 else 0.0,
        http_statuses=dict(Counter(str(row.http_status) for row in results if row.http_status is not None)),
        business_codes=dict(Counter(str(row.code) for row in failed if row.code is not None)),
        error_kinds=dict(Counter(row.error_kind for row in failed)),
        latency_ms=latency_stats(row.latency_ms for row in successful),
        stream_first_delta_ms=latency_stats(row.first_delta_ms for row in successful
                                           if row.mode == "stream" and row.first_delta_ms is not None),
        stream_complete_answer_ms=latency_stats(row.complete_answer_ms for row in successful
                                               if row.mode == "stream" and row.complete_answer_ms is not None),
    )
    if debug:
        stages = {}
        for stage in STAGES:
            samples = [row.stages_ms[stage] for row in results if stage in row.stages_ms]
            stages[stage] = dict(count=len(samples), mean_ms=sum(samples) / len(samples) if samples else None,
                                 p90_ms=percentile(samples, 90))
        providers = Counter()
        for row in results:
            providers.update(row.rerank_providers)
        report.update(stages=stages, routes={route: sum(row.route == route for row in results) for route in ROUTES},
                      rerank_providers=dict(providers))
    report["results"] = [asdict(row) for row in sorted(results, key=lambda row: row.index)]
    return report


class HealthCheckError(Exception):
    """仅使用固定提示，不转发响应与异常原文。"""


async def check_health(client, args):
    try:
        async with asyncio.timeout(args.timeout):
            response = await client.get(args.base_url + "/health")
            if not response.is_success:
                raise HealthCheckError("/health HTTP 检查失败，请确认地址及后端状态")
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError
        states = {name: payload.get(name) if payload.get(name) in allowed else "unknown"
                  for name, allowed in (("mysql", ("up", "down")), ("redis", ("up", "down")),
                                        ("milvus", ("up", "down")), ("llm", ("configured", "unconfigured")))}
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise HealthCheckError("/health 不可用或响应无效，请先启动后端并检查依赖") from None
    print("依赖状态：" + " | ".join(f"{name}={state}" for name, state in states.items()))
    if any(state not in {"up", "configured"} for state in states.values()):
        raise HealthCheckError("/health 依赖未就绪，请修复上述依赖后重试；未发送压测请求")
    return states


async def run_load_test(args, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    questions = load_questions(args.questions)
    specs = build_requests(args, questions)
    # 不信任环境代理、禁止重定向，所有请求均固定拼接至已校验的 base-url。
    async with httpx.AsyncClient(transport=transport, timeout=args.timeout, trust_env=False,
                                follow_redirects=False,
                                limits=httpx.Limits(max_connections=args.concurrency,
                                                    max_keepalive_connections=args.concurrency)) as client:
        states = await check_health(client, args)
        # health 不提供服务端限流配置；本工具从不读取 .env 或导入 Settings。
        print(RATE_LIMIT_NOTICE)
        print(f"开始压测：并发={args.concurrency} 总数={args.requests} mode={args.mode} ramp={args.ramp:g}s")
        if httpx.URL(args.base_url).host not in {"127.0.0.1", "localhost"}:
            print("注意：目标为非本机主机，已显式确认授权；请留意目标承载能力。")
        started = perf_counter()
        pending = iter(specs)
        results = []

        async def worker():
            for spec in pending:
                # 按所有 M 个请求的序号分布到 ramp 内；繁忙时受 worker 容量约束延后。
                offset = args.ramp * spec.index / max(args.requests - 1, 1)
                await asyncio.sleep(max(0, started + offset - perf_counter()))
                results.append(await send_request(client, args, spec))

        await asyncio.gather(*(worker() for _ in range(min(args.concurrency, args.requests))))
        report = summarize(results, perf_counter() - started, debug=args.debug)
        report["health"] = states
        return report


def print_report(report):
    def number(value):
        return "—" if value is None else str(value) if type(value) is int else f"{value:.2f}"

    def cell(title):
        width = sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in title)
        return title + " " * max(0, 28 - width)

    print("\n" + cell("指标") + " | 结果")
    print("-----------------------------+----------------------------------------------------")
    for title, key in (("完成数", "completed"), ("成功数", "succeeded"), ("失败数", "failed"),
                       ("被本服务限流 (42901/42902)", "service_limited"), ("后端故障/其他失败", "backend_failures"),
                       ("总墙钟时间 (s)", "wall_seconds"), ("成功吞吐 (req/s)", "throughput_success_per_second")):
        print(f"{cell(title)} | {number(report[key])}")
    print(cell("延迟 (ms)") + " | 样本     min      p50      p90      p95      p99      max")
    for title, key in (("成功请求", "latency_ms"), ("SSE 首字", "stream_first_delta_ms"),
                       ("SSE 完整回答", "stream_complete_answer_ms")):
        values = report[key]
        print(f"{cell(title)} | {values['count']:>4} " + " ".join(
            f"{number(values[name]):>8}" for name in ("min", "p50", "p90", "p95", "p99", "max")))
    for title, key in (("HTTP 状态分布", "http_statuses"), ("业务错误码分布", "business_codes"),
                       ("失败类型", "error_kinds")):
        print(f"{cell(title)} | {json.dumps(report[key], ensure_ascii=False)}")
    if "stages" in report:
        print(cell("trace 阶段 (ms)") + " | 样本     mean      p90")
        for stage, values in report["stages"].items():
            print(f"{cell(stage)} | {values['count']:>4} {number(values['mean_ms']):>8} {number(values['p90_ms']):>8}")
        print(cell("route 分布") + " | " + json.dumps(report["routes"]))
        print(cell("rerank provider 分布") + " | " + json.dumps(report["rerank_providers"]))
        if not any(row["count"] for row in report["stages"].values()):
            print("无 trace 数据：请确认服务端已允许 ALLOW_TRACE 且请求带 --debug。")


def write_report(path, report):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv=None, *, transport=None):
    args = parse_args(argv)
    try:
        report = asyncio.run(run_load_test(args, transport=transport))
        print_report(report)
        if args.output:
            write_report(args.output, report)
    except HealthCheckError as exc:
        print(f"健康检查失败：{exc}")
        return 1
    except (OSError, ValueError):
        print("输入或报告文件处理失败，请检查 JSONL 格式、路径与权限。")
        return 1
    except KeyboardInterrupt:
        print("压测已中断。")
        return 130
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
