"""只使用 MockTransport，禁止启动真实服务或调用模型。"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from eval import load_test as lt


HEALTH = {"mysql": "up", "redis": "up", "milvus": "up", "llm": "configured"}
ANSWER = "PRIVATE_ANSWER_完整答案不得出现在文件中"
SECRET = "sk-private-test-credential"


def answer(**changes):
    return dict(answer=ANSWER, route="rag", **changes)


def event(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks, self.delay = chunks, delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def options(tmp_path, *extra):
    return lt.parse_args(["--questions", str(tmp_path / "missing.jsonl"), *extra])


def run(args, handler):
    return asyncio.run(lt.run_load_test(args, transport=httpx.MockTransport(handler)))


def test_percentiles_and_empty_samples():
    assert lt.percentile([], 50) is None
    assert lt.percentile([7], 99) == 7
    assert lt.percentile([40, 10, 30, 20], 50) == 25
    assert lt.percentile([0, 100], 90) == 90
    assert lt.percentile([0, 100], 95) == 95
    assert lt.percentile([0, 100], 99) == 99
    with pytest.raises(ValueError):
        lt.percentile([1], 101)
    report = lt.summarize([], 0, debug=True)
    assert report["completed"] == report["throughput_success_per_second"] == 0
    assert report["latency_ms"] == dict(count=0, min=None, p50=None, p90=None, p95=None, p99=None, max=None)
    assert report["stages"]["router"]["mean_ms"] is None


def test_sse_lines_comments_multiline_unknown_and_error():
    rows = list(lt.parse_sse_lines([
        ": ping", "", "event: meta", 'data: {"request_id":"r"}', "",
        "event: status", 'data: {"stage":"router"}', "",
        "event: ignored", "data: not-json", "",
        "event: delta", 'data: {"text":', 'data: "你好"}', "",
        "event:error", 'data:{"code":50302,"message":"private"}', "",
    ]))
    assert [name for name, _ in rows] == ["meta", "status", "delta", "error"]
    assert rows[-2][1] == {"text": "你好"}
    assert rows[-1][1]["code"] == 50302
    assert list(lt.parse_sse_lines(["event: done", 'data: {"answer":"partial"}'])) == []
    with pytest.raises(ValueError):
        list(lt.parse_sse_lines(["event: delta", "data: []", ""]))


def test_summary_mixed_success_failure_limit_timeout_and_trace(capsys):
    first = lt.RequestResult(0, "json", "q", False, success=True, http_status=200,
                             route="rag", latency_ms=10, stages_ms={"router": 2, "rerank": 6},
                             rerank_providers={"remote": 1})
    rows = [first, replace(first, index=1, mode="stream", latency_ms=30, route="faq",
                           first_delta_ms=4, complete_answer_ms=28, stages_ms={"router": 8}),
            lt.RequestResult(2, "json", "q", False, http_status=503, code=50302, error_kind="http_error"),
            lt.RequestResult(3, "stream", "q", False, http_status=200, code=42901, error_kind="business_error"),
            lt.RequestResult(4, "json", "q", False, http_status=429, code=42902, error_kind="http_error"),
            lt.RequestResult(5, "json", "q", False, error_kind="timeout")]
    report = lt.summarize(rows, 2, debug=True)
    assert (report["completed"], report["succeeded"], report["failed"]) == (6, 2, 4)
    assert report["service_limited"] == report["backend_failures"] == 2
    assert report["throughput_success_per_second"] == 1
    assert report["latency_ms"]["p50"] == 20
    assert report["stream_first_delta_ms"]["p90"] == 4
    assert report["stream_complete_answer_ms"]["max"] == 28
    assert report["http_statuses"] == {"200": 3, "503": 1, "429": 1}
    assert report["business_codes"] == {"50302": 1, "42901": 1, "42902": 1}
    assert report["error_kinds"]["timeout"] == 1
    assert report["stages"]["router"] == {"count": 2, "mean_ms": 5, "p90_ms": pytest.approx(7.4)}
    assert report["routes"] == {"faq": 1, "cache": 0, "rag": 1, "general": 0, "fallback": 0}
    lt.print_report(report)
    assert "被本服务限流" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [
    ["--concurrency", "0"], ["--concurrency", "201"], ["--requests", "0"], ["--requests", "5001"],
    ["--history-ratio", "-0.1"], ["--history-ratio", "1.1"], ["--timeout", "0"],
    ["--timeout", "nan"], ["--ramp", "inf"], ["--ramp", "-1"],
    ["--concurrency", "1.5"], ["--base-url", "ftp://localhost"],
    ["--base-url", "localhost:8010"], ["--base-url", "https://user:private@localhost"],
    ["--base-url", "http://localhost?api_key=private"], ["--base-url", "http://localhost/#private"],
    ["--mode", "invalid"], ["--questions", ".env"], ["--output", "folder/.env.local"],
])
def test_parameter_validation(flags):
    with pytest.raises(SystemExit) as exc:
        lt.parse_args(flags)
    assert exc.value.code == 2


def test_defaults_and_limits():
    args = lt.parse_args([])
    assert (args.base_url, args.concurrency, args.requests, args.timeout, args.ramp) == (
        lt.DEFAULT_BASE_URL, 10, 10, 90, 0)
    assert args.questions == lt.DEFAULT_QUESTIONS
    assert (lt.parse_args(["--concurrency", "200", "--requests", "5000"]).requests) == 5000
    assert lt.parse_args(["--concurrency", "7"]).requests == 7


@pytest.mark.parametrize("host", ["example.com", "192.168.1.2", "127.0.0.2", "[::1]"])
def test_nonlocal_protection_even_at_small_concurrency(host):
    flags = ["--base-url", f"https://{host}", "--concurrency", "1"]
    with pytest.raises(SystemExit):
        lt.parse_args(flags)
    assert lt.parse_args([*flags, "--i-know-this-is-not-localhost"]).concurrency == 1


def test_localhost_and_acknowledged_high_concurrency():
    assert lt.parse_args(["--base-url", "http://localhost:8010", "--concurrency", "200"]).concurrency == 200
    assert lt.parse_args(["--base-url", "https://example.com", "--concurrency", "21",
                          "--i-know-this-is-not-localhost"]).concurrency == 21


@pytest.mark.parametrize("ratio,count", [(0, 0), (0.25, 3), (0.5, 5), (1, 10)])
def test_history_ratio_and_unique_sessions(tmp_path, ratio, count):
    args = options(tmp_path, "--requests", "10", "--mode", "both", "--history-ratio", str(ratio), "--seed", "7")
    specs = lt.build_requests(args, ["a", "b"])
    repeated = lt.build_requests(args, ["a", "b"])
    assert sum(spec.has_history for spec in specs) == count
    assert [spec.has_history for spec in specs] == [spec.has_history for spec in repeated]
    assert len({spec.session_id for spec in specs + repeated}) == 20
    assert all(spec.session_id.startswith("loadtest-") for spec in specs)
    assert [spec.question for spec in specs[:3]] == ["a", "b", "a"]
    assert sum(spec.mode == "json" for spec in specs) == 5
    assert sum(spec.mode == "stream" for spec in specs) == 5


def test_questions_deduplicate_and_fallback(tmp_path):
    path = tmp_path / "questions.jsonl"
    assert len(lt.load_questions(path)) == 10
    path.write_text('\n{"question":" a "}\n{"question":"a"}\n{"question":"b"}\n{"question":null}\n', encoding="utf-8")
    assert lt.load_questions(path) == ["a", "b"]
    path.write_text("\n", encoding="utf-8")
    assert len(lt.load_questions(path)) == 10


def test_json_end_to_end_bounded_workers_and_debug(tmp_path):
    args = options(tmp_path, "--concurrency", "3", "--requests", "11", "--history-ratio", "0.5", "--debug")
    active = peak = 0
    requests = []

    async def handler(request):
        nonlocal active, peak
        assert request.url.host == "127.0.0.1"
        if request.url.path == "/health":
            assert not requests
            return httpx.Response(200, json=HEALTH)
        assert request.url.path == "/api/v1/chat"
        payload = json.loads(request.content)
        requests.append(payload)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json=answer(trace=[
            {"stage": "router", "ms": 2}, {"stage": "router", "ms": 3},
            {"stage": "rerank", "ms": 5, "provider": "remote", "detail": SECRET},
            {"stage": "generate", "ms": 7}, {"stage": "retrieve", "ms": 4},
        ]))

    report = run(args, handler)
    assert peak == 3
    assert len(requests) == report["succeeded"] == 11
    assert len({row["session_id"] for row in requests}) == 11
    assert sum(len(row["history"]) == 2 for row in requests) == 6
    assert all(row["debug"] for row in requests)
    assert report["stages"]["router"]["mean_ms"] == 5
    assert report["rerank_providers"] == {"remote": 11}
    assert all(row["answer_length"] == len(ANSWER) for row in report["results"])


def test_stream_end_to_end_utf8_crlf_cross_blocks(tmp_path):
    args = options(tmp_path, "--concurrency", "1", "--mode", "stream", "--debug")
    frames = (": ping\n\n" + event("meta", {"request_id": "r"})
              + event("status", {"stage": "router"}) + event("delta", {"text": "你好"})
              + event("done", answer(trace=[{"stage": "faq", "ms": 3}]))).replace("\n", "\r\n").encode()
    stream = Chunks([bytes([byte]) for byte in frames])

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        assert request.url.path == "/api/v1/chat/stream"
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream)

    report = run(args, handler)
    result = report["results"][0]
    assert report["succeeded"] == 1 and result["route"] == "rag"
    assert 0 <= result["first_delta_ms"] <= result["complete_answer_ms"] <= result["latency_ms"]
    assert result["answer_length"] == len(ANSWER)
    assert report["stages"]["faq"]["mean_ms"] == 3
    assert stream.closed


@pytest.mark.parametrize("code,status", [(50302, 503), (50401, 504), (42901, 429), (42902, 429), (50201, 502)])
@pytest.mark.parametrize("mode", ["json", "stream"])
def test_http_and_sse_errors(tmp_path, code, status, mode):
    args = options(tmp_path, "--concurrency", "1", "--mode", mode)

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        payload = dict(code=code, message=SECRET)
        if mode == "json":
            return httpx.Response(status, json=payload)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=event("delta", {"text": "partial"}) + event("error", payload))

    report = run(args, handler)
    assert report["failed"] == 1 and report["business_codes"] == {str(code): 1}
    assert report["service_limited"] == int(code in lt.LIMIT_CODES)
    assert report["backend_failures"] == int(code not in lt.LIMIT_CODES)
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("failure", ["read_timeout", "wall_timeout", "connection", "missing_done", "invalid_json", "not_sse"])
def test_timeout_transport_and_protocol_errors(tmp_path, failure):
    args = options(tmp_path, "--concurrency", "1", "--timeout", "0.01", "--mode", "stream")

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        if failure == "read_timeout":
            raise httpx.ReadTimeout(SECRET)
        if failure == "connection":
            raise httpx.ConnectError(SECRET)
        if failure == "not_sse":
            return httpx.Response(200, text=SECRET)
        content = b"event: done\ndata: invalid\n\n" if failure == "invalid_json" else b": ping\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Chunks([content] * 5, delay=0.004 if failure == "wall_timeout" else 0))

    report = run(args, handler)
    kind = "timeout" if "timeout" in failure else "transport_error" if failure == "connection" else "protocol_error"
    assert report["failed"] == 1 and report["error_kinds"] == {kind: 1}
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("mode", ["json", "stream"])
def test_main_output_drops_answers_secrets_and_free_text(tmp_path, capsys, mode):
    output = tmp_path / "reports" / "load.json"
    questions = tmp_path / "questions.jsonl"
    questions.write_text(json.dumps({"question": f"api_key={SECRET} 请问保修多久？"}), encoding="utf-8")

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={**HEALTH, "api_key": SECRET})
        payload = answer(trace=[{"stage": "router", "ms": 1, "detail": SECRET},
                                {"stage": "rerank", "ms": 2, "provider": SECRET}])
        if mode == "json":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=event("delta", {"text": ANSWER}) + event("done", payload))

    assert lt.main(["--questions", str(questions), "--concurrency", "1", "--output", str(output),
                    "--debug", "--mode", mode], transport=httpx.MockTransport(handler)) == 0
    serialized, console = output.read_text(encoding="utf-8"), capsys.readouterr().out
    assert ANSWER not in serialized + console and SECRET not in serialized + console
    row = json.loads(serialized)["results"][0]
    assert len(row["question_preview"]) <= 20 and row["answer_length"] == len(ANSWER)
    assert "[redacted]" in row["question_preview"]
    assert "trace" not in row and "answer" not in row and "session_id" not in row
    assert lt.RATE_LIMIT_NOTICE in console


@pytest.mark.parametrize("problem", ["down", "bad_status", "redirect", "timeout", "malformed", "secret"])
def test_health_failure_exits_without_chat_or_external_redirect(tmp_path, capsys, problem):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert request.url.path == "/health"
        if problem == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if problem == "bad_status":
            return httpx.Response(503, text=SECRET)
        if problem == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example/"})
        if problem == "malformed":
            return httpx.Response(200, text=SECRET)
        return httpx.Response(200, json={**HEALTH, "mysql": SECRET if problem == "secret" else "down"})

    assert lt.main(["--questions", str(tmp_path / "missing.jsonl")], transport=httpx.MockTransport(handler)) == 1
    assert seen == [lt.DEFAULT_BASE_URL + "/health"]
    console = capsys.readouterr().out
    assert "健康检查失败" in console and SECRET not in console


def test_both_ramp_nonlocal_warning_and_no_redirects(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://should-not-be-used.invalid:80")
    args = options(tmp_path, "--base-url", "https://target.example/prefix", "--i-know-this-is-not-localhost",
                   "--mode", "both", "--concurrency", "21", "--requests", "5", "--ramp", "0.03")
    paths = []

    def handler(request):
        assert request.url.host == "target.example"
        paths.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json=HEALTH)
        return httpx.Response(307, headers={"location": "https://other.example/"})

    report = run(args, handler)
    assert report["failed"] == 5
    assert report["wall_seconds"] >= 0.025
    assert paths.count("/prefix/api/v1/chat") == 3
    assert paths.count("/prefix/api/v1/chat/stream") == 2
    assert "非本机主机" in capsys.readouterr().out


def test_environment_paths_are_rejected_before_io(tmp_path):
    with pytest.raises(ValueError):
        lt.load_questions(Path(".env.private"))
    with pytest.raises(ValueError):
        lt.write_report(tmp_path / ".env.output", {})
    link = tmp_path / "report-link"
    link.symlink_to(tmp_path / ".env.secret")
    with pytest.raises(ValueError):
        lt.safe_path(link)
