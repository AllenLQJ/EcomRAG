"""Judge 调用回归：仅标准库，可用 unittest 单独运行，不加载服务或评估依赖。"""
import asyncio
import inspect
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from eval import ragas_eval
from eval.judge_diagnostics import (
    EmptyJudgeResponseError, TruncatedJudgeResponseError, diagnose, safe_parameters,
)
from eval.ragas_backend import ResponseDiagnostics, make_scorer
from eval.reports import write_report


def config(**changes):
    return ragas_eval.ScoreConfig(**{
        "judge_model": "glm-5.3-flash", "judge_base_url": "https://judge.invalid/v1",
        "judge_key_env": "TEST_JUDGE_KEY", "api_key": "sk-test-only",
        "metrics": ("faithfulness",), "retry_backoff_s": 0, **changes,
    })


def collection(count=1):
    return {"phase": "collect", "schema_version": 1, "cases": [
        {"id": i, "question": str(i), "answer": "answer", "reference": "reference", "route": "rag",
         "contexts": [{"text": "private-context"}]} for i in range(count)]}


class HTTPFailure(Exception):
    def __init__(self, status, code=None):
        super().__init__("private-response sk-test-only private-prompt")
        self.status_code = status
        self.body = {"error": {"code": code, "message": "private-response"}}
        self.headers = {"Authorization": "Bearer sk-test-only"}


class OpenAIConnectionError(Exception):
    pass


def connection_failure():
    underlying = RuntimeError("Event loop is closed")
    transport = type("ConnectError", (Exception,), {})("All connection attempts failed")
    transport.__cause__ = underlying
    sdk = OpenAIConnectionError("Connection error.")
    sdk.__cause__ = transport
    return sdk


class LoopBoundHTTPClient:
    """假连接池在第一次创建的循环里才能使用/关闭，用于重现旧生命周期故障。"""
    def __init__(self, **kwargs):
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self):
        if self.closed or self.loop is not asyncio.get_running_loop():
            raise connection_failure()

    async def request(self):
        self.check()
        await asyncio.sleep(0)
        return "json"

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *args):
        self.check()
        self.closed = True

    async def __aenter__(self):
        self.check()
        return self

    async def __aexit__(self, *args):
        self.check()
        await asyncio.sleep(0)
        self.closed = True


def response(text="json", finish_reason="stop"):
    return SimpleNamespace(generations=[[SimpleNamespace(
        text=text, generation_info={"finish_reason": finish_reason},
        message=SimpleNamespace(response_metadata={}, tool_calls=[]))]])


@contextmanager
def fake_dependencies(evaluate):
    """伪造公开指标 API 和有循环约束的 HTTP clients，不导入 SDK。"""
    modules = {name: ModuleType(name) for name in (
        "ragas", "ragas.dataset_schema", "ragas.llms", "ragas.embeddings", "ragas.metrics", "ragas.run_config",
        "langchain_openai", "langchain_core", "langchain_core.callbacks", "httpx")}

    def model(**kwargs):
        kwargs["http_async_client"].check()
        kwargs["http_client"].check()
        return SimpleNamespace(**kwargs)

    clients = Mock(side_effect=model)
    clients.http_clients = []
    clients.embedding_clients = Mock(side_effect=model)

    def http_client(**kwargs):
        client = LoopBoundHTTPClient(**kwargs)
        clients.http_clients.append(client)
        return client

    class Metric:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def init(self, run_config):
            self.run_config = run_config

        async def single_turn_ascore(self, sample, *, callbacks, timeout):
            assert asyncio.get_running_loop().is_running()
            assert timeout == self.run_config.timeout
            for wrapped in (self.llm, getattr(self, "embeddings", None)):
                if getattr(wrapped, "http_async_client", None) is not None:
                    await wrapped.http_async_client.request()
            result = evaluate([vars(sample)], metrics=[self], callbacks=callbacks, run_config=self.run_config)
            if inspect.isawaitable(result):
                result = await result
            return next(iter(result.scores[0].values()))

    modules["ragas.dataset_schema"].SingleTurnSample = SimpleNamespace
    modules["ragas"].evaluate = Mock(side_effect=AssertionError("不得使用隐式管理事件循环的 evaluate"))
    modules["ragas.llms"].LangchainLLMWrapper = lambda client: client
    modules["ragas.embeddings"].LangchainEmbeddingsWrapper = lambda client: client
    for name in ("Faithfulness", "ResponseRelevancy", "LLMContextPrecisionWithReference", "LLMContextRecall"):
        setattr(modules["ragas.metrics"], name, Metric)
    modules["ragas.run_config"].RunConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    modules["langchain_openai"].ChatOpenAI = clients
    modules["langchain_openai"].OpenAIEmbeddings = clients.embedding_clients
    modules["httpx"].Client = http_client
    modules["httpx"].AsyncClient = http_client
    modules["langchain_core.callbacks"].BaseCallbackHandler = type("BaseCallbackHandler", (), {})
    with patch.dict("sys.modules", modules), patch.dict(os.environ, {"RAGAS_DO_NOT_TRACK": "true"}):
        yield clients


class JudgeTests(unittest.TestCase):
    def test_shared_async_pool_reproduces_closed_loop_connection_failure(self):
        shared = []

        async def use():
            if not shared:
                shared.append(LoopBoundHTTPClient())
            return await shared[0].request()

        self.assertEqual(asyncio.run(use()), "json")
        self.assertTrue(shared[0].loop.is_closed())
        with self.assertRaises(OpenAIConnectionError) as caught:
            asyncio.run(use())
        diagnostic = diagnose(caught.exception)
        self.assertEqual(diagnostic["category"], "connection")
        self.assertEqual(diagnostic["exception_chain"][-1], {
            "exception_type": "RuntimeError", "message": "Event loop is closed"})

    def test_default_backend_isolates_all_48_attempts_and_closes_clients_on_owner_loop(self):
        def evaluate(dataset, *, callbacks, **kwargs):
            callbacks[0].on_llm_end(response())
            return SimpleNamespace(scores=[{"metric": 0.5}])

        metrics = ("faithfulness", "context_precision", "context_recall")
        with fake_dependencies(evaluate) as clients:
            # make_scorer 只捕获配置和类，不能在运行循环外创建/缓存客户端。
            scorer = make_scorer(config(metrics=metrics))
            self.assertEqual(clients.call_count, 0)
            self.assertEqual(clients.http_clients, [])
            report = ragas_eval.score(collection(16), config(metrics=metrics))
            self.assertEqual(clients.call_count, 48)
            self.assertEqual(len(clients.http_clients), 96)
            self.assertEqual(len({id(client.loop) for client in clients.http_clients}), 48)
            self.assertTrue(all(client.closed and client.loop.is_closed() for client in clients.http_clients))
        self.assertEqual(report["meta"]["judge_client_lifecycle"], "per_attempt_event_loop")
        self.assertEqual(report["meta"]["max_workers"], 4)
        self.assertEqual(report["summary"]["failure_categories"], {})
        self.assertEqual([report["summary"]["metrics"][name]["valid"] for name in metrics], [16, 16, 16])

    def test_connection_failure_retries_with_fresh_clients_then_succeeds(self):
        attempted = []

        def evaluate(dataset, *, metrics, callbacks, **kwargs):
            attempted.append(metrics[0].llm.http_async_client)
            if len(attempted) == 1:
                raise connection_failure()
            callbacks[0].on_llm_end(response())
            return SimpleNamespace(scores=[{"faithfulness": 0.75}])

        with fake_dependencies(evaluate) as clients:
            report = ragas_eval.score(collection(), config())
            self.assertEqual(clients.call_count, 2)
            self.assertTrue(all(client.closed and client.loop.is_closed() for client in clients.http_clients))
        self.assertIsNot(attempted[0], attempted[1])
        self.assertIsNot(attempted[0].loop, attempted[1].loop)
        entry = report["cases"][0]["scores"]["faithfulness"]
        self.assertEqual((entry["status"], entry["retries"]), ("valid", 1))
        failure = entry["attempt_failures"][0]
        self.assertEqual(failure["category"], "connection")
        self.assertTrue(failure["is_connection_error"])
        self.assertEqual([item["exception_type"] for item in failure["exception_chain"]],
                         ["OpenAIConnectionError", "ConnectError", "RuntimeError"])
        self.assertEqual(report["summary"]["attempt_failure_categories"], {"connection": 1})

    def test_persistent_connection_failure_exhausts_retries_and_preserves_diagnostics(self):
        def evaluate(*args, **kwargs):
            raise connection_failure()

        with fake_dependencies(evaluate) as clients:
            report = ragas_eval.score(collection(), config())
            self.assertEqual(clients.call_count, 3)
            self.assertTrue(all(client.closed and client.loop.is_closed() for client in clients.http_clients))
        entry = report["cases"][0]["scores"]["faithfulness"]
        self.assertEqual((entry["status"], entry["reason"], entry["retries"]), ("error", "judge_connection", 2))
        self.assertEqual(len(entry["attempt_failures"]), 3)
        self.assertEqual(entry["diagnostics"]["exception_chain"][-1]["message"], "Event loop is closed")
        self.assertEqual(report["summary"]["failure_categories"], {"connection": 1})
        self.assertEqual(report["summary"]["failure_categories_by_metric"], {"faithfulness": {"connection": 1}})
        self.assertEqual(report["summary"]["attempt_failure_categories"], {"connection": 3})

    def test_embeddings_use_and_close_the_same_attempts_owned_clients(self):
        def evaluate(dataset, *, metrics, callbacks, **kwargs):
            metric = metrics[0]
            self.assertIs(metric.llm.http_async_client, metric.embeddings.http_async_client)
            self.assertIs(metric.llm.http_client, metric.embeddings.http_client)
            callbacks[0].on_llm_end(response())
            return SimpleNamespace(scores=[{"response_relevancy": 0.5}])

        with fake_dependencies(evaluate) as clients:
            report = ragas_eval.score(collection(2), config(
                metrics=("response_relevancy",), embedding_model="fake-embedding",
                embedding_base_url="https://embedding.invalid/v1"))
            self.assertEqual(clients.embedding_clients.call_count, 2)
            self.assertTrue(all(client.closed for client in clients.http_clients))
        self.assertEqual(report["summary"]["metrics"]["response_relevancy"]["valid"], 2)

    def test_injected_clients_remain_caller_owned(self):
        judge, embeddings = SimpleNamespace(), SimpleNamespace()

        def evaluate(dataset, *, metrics, **kwargs):
            self.assertIs(metrics[0].llm, judge)
            self.assertIs(metrics[0].embeddings, embeddings)
            return SimpleNamespace(scores=[{"response_relevancy": 0.5}])

        with fake_dependencies(evaluate) as clients:
            report = ragas_eval.score(collection(), config(metrics=("response_relevancy",)),
                                      judge=judge, embeddings=embeddings)
            self.assertEqual(clients.call_count, 0)
            self.assertEqual(clients.http_clients, [])
        self.assertEqual(report["meta"]["judge_client_lifecycle"], "external_clients")
        self.assertEqual(vars(judge), {})
        self.assertEqual(vars(embeddings), {})

    def test_connection_type_variants_and_timeout_precedence(self):
        for name in ("OpenAIConnectionError", "APIConnectionError", "ConnectError", "ReadError", "WriteError",
                     "RemoteProtocolError", "ClosedResourceError", "BrokenResourceError", "EndOfStream"):
            with self.subTest(name=name):
                exc = type(name, (Exception,), {})("private-body")
                self.assertEqual(diagnose(exc)["category"], "connection")
                report = ragas_eval.score(collection(), config(), scorer=Mock(side_effect=[exc, 0.5]))
                self.assertEqual(report["cases"][0]["scores"]["faithfulness"]["retries"], 1)
        exc = type("ReadTimeout", (type("ReadError", (Exception,), {}),), {})("private-body")
        self.assertEqual(diagnose(exc)["category"], "timeout")
        self.assertTrue(diagnose(exc)["is_connection_error"])

    def test_exception_chain_messages_are_bounded_and_never_copy_payloads_or_urls(self):
        payload = (' https://user:sk-test-only@judge.invalid/v1?token=private-query '
                   'Authorization: Bearer sk-test-only request_body={"prompt":"private-body"}')
        exc = OpenAIConnectionError("Connection error." + payload)
        exc.__cause__ = RuntimeError("Event loop is closed" + payload)
        report = ragas_eval.score(collection(), config(max_retries=0), scorer=Mock(side_effect=exc))
        entry = report["cases"][0]["scores"]["faithfulness"]
        self.assertEqual(entry["diagnostics"]["exception_chain"][-1]["message"], "Event loop is closed")
        self.assertTrue(all(len(item["message"] or "") <= 80
                            for item in entry["diagnostics"]["exception_chain"]))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            out = Path(folder) / "connection.json"
            write_report(report, out, secrets=("sk-test-only",))
            exported = json.dumps(report) + out.read_text() + out.with_suffix(".md").read_text()
        for secret in ("sk-test-only", "private-query", "judge.invalid", "private-body", "request_body", "Authorization"):
            self.assertNotIn(secret, exported)
        opaque = type("ReadError", (Exception,), {})(payload)
        self.assertEqual(diagnose(opaque)["exception_chain"][0]["message"], "[REDACTED]")
        parser = ValueError(payload)
        self.assertIsNone(diagnose(parser)["exception_chain"][0]["message"])
        current = exc
        for _ in range(12):
            current.__cause__ = RuntimeError(payload)
            current = current.__cause__
        self.assertEqual(len(diagnose(exc)["exception_chain"]), 8)
        current.__cause__ = exc
        self.assertLessEqual(len(diagnose(exc)["exception_chain"]), 8)

    def test_defaults_and_explicit_parameters(self):
        cfg = config()
        self.assertEqual((cfg.max_retries, cfg.max_workers, cfg.max_tokens), (2, 4, 4096))
        self.assertIsNone(ragas_eval.resolve_extra_body(None, "glm-5.3-flash"))
        for body in ({}, {"thinking": {"type": "enabled"}}, {"reasoning_effort": "low"}):
            self.assertEqual(ragas_eval.resolve_extra_body(json.dumps(body), "glm-5.3-flash"), body)

    def test_1210_fallback_uses_low_and_reports_actual_parameters(self):
        def evaluate(dataset, *, metrics, callbacks, **kwargs):
            if metrics[0].llm.extra_body is None:
                raise HTTPFailure(400, "1210")
            self.assertEqual(metrics[0].llm.extra_body, {"reasoning_effort": "low"})
            callbacks[0].on_llm_end(response())
            return SimpleNamespace(scores=[{"faithfulness": 0.75}])

        with fake_dependencies(evaluate) as clients:
            report = ragas_eval.score(collection(), config())
        entry = report["cases"][0]["scores"]["faithfulness"]
        self.assertEqual(entry["retries"], 1)
        self.assertEqual(entry["status"], "valid")
        self.assertEqual(entry["attempt_failures"][0]["provider_error_code"], "1210")
        self.assertEqual(report["meta"]["actual_judge_parameters"], [{"reasoning_effort": "low"}])
        self.assertEqual(report["summary"]["failure_categories"], {})
        self.assertEqual(report["summary"]["attempt_failure_categories"], {"http_error": 1})
        self.assertEqual(clients.call_count, 2)

    def test_1210_fallback_is_once_and_never_overrides_explicit_body(self):
        for body, calls in ((None, 2), ({}, 1), ({"thinking": {"type": "disabled"}}, 1)):
            with self.subTest(body=body), fake_dependencies(Mock(side_effect=HTTPFailure(400, 1210))) as clients:
                report = ragas_eval.score(collection(), config(judge_extra_body=body))
                entry = report["cases"][0]["scores"]["faithfulness"]
                self.assertEqual(clients.call_count, calls)
                self.assertEqual(clients.call_args_list[0].kwargs["extra_body"], body)
                self.assertEqual(entry["retries"], calls - 1)
                self.assertEqual(entry["diagnostics"]["provider_error_code"], "1210")

    def test_failure_classification_and_counts(self):
        errors = [
            (ValueError("private-response"), "parse_error"),
            (EmptyJudgeResponseError(), "empty_response"), (TimeoutError(), "timeout"),
            (HTTPFailure(429, "rate_limit_exceeded"), "rate_limit"),
            (HTTPFailure(503), "server_error"), (HTTPFailure(401), "http_error"),
            (TruncatedJudgeResponseError(), "truncated"), (RuntimeError(), "unknown"),
        ]
        by_question = {str(i): exc for i, (exc, _) in enumerate(errors)}

        def scorer(sample, metric):
            raise by_question[sample["user_input"]]

        report = ragas_eval.score(collection(len(errors)), config(max_retries=0), scorer=scorer)
        counts = {category: 1 for _, category in errors}
        self.assertEqual(report["summary"]["failure_categories"], counts)
        self.assertEqual(report["summary"]["failure_categories_by_metric"], {"faithfulness": counts})
        self.assertEqual(report["summary"]["metrics"]["faithfulness"]["error"], len(errors))
        for i, (exc, category) in enumerate(errors):
            info = report["cases"][i]["scores"]["faithfulness"]["diagnostics"]
            self.assertEqual(info["category"], category)
            self.assertEqual(info["exception_type"], type(exc).__name__)
            for flag, target in (("is_parse_error", "parse_error"), ("is_timeout", "timeout"),
                                 ("is_empty_response", "empty_response"), ("is_truncated", "truncated")):
                self.assertEqual(info[flag], category == target)

    def test_retry_budget_backoff_and_recovered_failure_history(self):
        for error in (ValueError(), EmptyJudgeResponseError(), TimeoutError(), HTTPFailure(500), HTTPFailure(429)):
            with self.subTest(error=type(error).__name__), patch.object(ragas_eval, "sleep") as sleep:
                scorer = Mock(side_effect=[error, error, 0.5])
                report = ragas_eval.score(collection(), config(retry_backoff_s=0.25), scorer=scorer)
                entry = report["cases"][0]["scores"]["faithfulness"]
                self.assertEqual(entry["retries"], 2)
                self.assertEqual(len(entry["attempt_failures"]), 2)
                self.assertEqual(entry["status"], "valid")
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])
                self.assertEqual(report["summary"]["judge_usage"]["retries"], 2)
        scorer = Mock(side_effect=TimeoutError())
        report = ragas_eval.score(collection(), config(), scorer=scorer)
        self.assertEqual(scorer.call_count, 3)
        self.assertEqual(report["cases"][0]["scores"]["faithfulness"]["retries"], 2)
        self.assertEqual(report["summary"]["attempt_failure_categories"], {"timeout": 3})

    def test_permanent_errors_and_truncation_do_not_retry(self):
        for error in (HTTPFailure(400), HTTPFailure(401), TruncatedJudgeResponseError(), RuntimeError()):
            with self.subTest(error=type(error).__name__):
                scorer = Mock(side_effect=error)
                ragas_eval.score(collection(), config(), scorer=scorer)
                self.assertEqual(scorer.call_count, 1)

    def test_observer_detects_empty_and_length_without_exporting_text(self):
        for text, finish, category in (("private-response", "length", "truncated"),
                                       ("", "stop", "empty_response")):
            def evaluate(dataset, *, callbacks, **kwargs):
                callbacks[0].on_llm_end(response(text, finish))
                raise ValueError("private-response sk-test-only")

            with self.subTest(category=category), fake_dependencies(evaluate):
                report = ragas_eval.score(collection(), config(max_retries=0))
            info = report["cases"][0]["scores"]["faithfulness"]["diagnostics"]
            self.assertEqual(info["category"], category)
            self.assertEqual(info["exception_type"], "ValueError")
            self.assertNotIn("private-response", json.dumps(report))
        observer = ResponseDiagnostics()
        observer.on_llm_end(SimpleNamespace(generations=[]))
        self.assertEqual(observer.failure()["category"], "empty_response")
        observer = ResponseDiagnostics()
        observer.on_llm_end(response("", "length"))
        self.assertEqual(observer.failure()["category"], "truncated")
        self.assertTrue(observer.failure()["is_empty_response"])

    def test_backend_passes_limits_and_separate_callback_state(self):
        observers = []

        def evaluate(dataset, *, callbacks, run_config, **kwargs):
            self.assertEqual((run_config.max_retries, run_config.max_workers, run_config.timeout), (0, 1, 17))
            observers.append(callbacks[0])
            callbacks[0].on_llm_end(response())
            return SimpleNamespace(scores=[{"faithfulness": 0.5}])

        with fake_dependencies(evaluate) as clients:
            scorer = make_scorer(config(max_tokens=8192, timeout_s=17))
            scorer({}, "faithfulness")
            scorer({}, "faithfulness")
            self.assertEqual(clients.call_args.kwargs["max_tokens"], 8192)
            self.assertEqual(clients.call_args.kwargs["max_retries"], 0)
            self.assertIsNot(observers[0], observers[1])

    def test_backend_empty_response_retries_even_when_evaluate_returns_a_score(self):
        calls = 0

        def evaluate(dataset, *, callbacks, **kwargs):
            nonlocal calls
            calls += 1
            callbacks[0].on_llm_end(response("" if calls == 1 else "json"))
            return SimpleNamespace(scores=[{"faithfulness": 0.5}])

        with fake_dependencies(evaluate):
            report = ragas_eval.score(collection(), config())
        entry = report["cases"][0]["scores"]["faithfulness"]
        self.assertEqual((entry["status"], entry["retries"], calls), ("valid", 1, 2))
        self.assertEqual(entry["attempt_failures"][0]["category"], "empty_response")

    def test_finish_reason_in_message_metadata_and_non_length_incomplete_output(self):
        observer = ResponseDiagnostics()
        result = response()
        result.generations[0][0].generation_info = {}
        result.generations[0][0].message.response_metadata = {"finish_reason": "length"}
        observer.on_llm_end(result)
        self.assertEqual(observer.failure()["finish_reason"], "length")
        # 未完成可能是 content_filter 等原因，不能把所有未完成异常推断为长度截断。
        exc = type("LLMDidNotFinishException", (Exception,), {})()
        self.assertFalse(diagnose(exc)["is_truncated"])

    def test_budget_and_denominators_remain_compatible(self):
        coll = collection(5)
        coll["cases"][0].update(route="faq", contexts=[])
        coll["cases"][2]["reference"] = None
        scorer = Mock(return_value=0.5)
        report = ragas_eval.score(coll, config(max_samples=2, metrics=("faithfulness", "context_recall")),
                                  scorer=scorer)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(scorer.call_count, 3)
        self.assertEqual(report["summary"]["metrics"]["faithfulness"], {
            "total": 5, "scorable": 4, "attempted": 2, "valid": 2, "error": 0, "invalid": 0,
            "not_applicable": 1, "skipped_limit": 2, "mean": 0.5, "median": 0.5})
        self.assertEqual(report["cases"][2]["scores"]["context_recall"]["reason"], "missing_reference")
        self.assertEqual(report["summary"]["failure_categories"], {"missing_reference": 1})

    def test_diagnostics_never_export_body_prompt_headers_or_keys(self):
        class Response:
            status_code = 503

            @property
            def text(self):
                raise AssertionError("不得读取 HTTP 正文")

            def json(self):
                raise AssertionError("不得读取 HTTP 正文")

        exc = HTTPFailure(503, "private-response")
        exc.response = Response()
        cfg = config(max_retries=0, judge_extra_body={
            "thinking": {"type": "enabled", "secret": "private-response"},
            "api_key": "sk-test-only", "prompt": "private-prompt", "headers": exc.headers})
        report = ragas_eval.score(collection(), cfg, scorer=Mock(side_effect=exc))
        self.assertIsNone(report["cases"][0]["scores"]["faithfulness"]["diagnostics"]["provider_error_code"])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            out = Path(folder) / "report.json"
            write_report(report, out, secrets=(cfg.api_key,))
            text = json.dumps(report) + out.read_text() + out.with_suffix(".md").read_text()
        for forbidden in ("private-response", "private-prompt", "sk-test-only", "Authorization", "private-context"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(safe_parameters(cfg.judge_extra_body), {"thinking": {"type": "enabled"}})

    def test_nested_sdk_errors_and_parser_timeout_names(self):
        for name, category in (("APITimeoutError", "timeout"), ("RagasOutputParserException", "parse_error"),
                               ("OutputParserException", "parse_error"), ("ValidationError", "parse_error"),
                               ("APIResponseValidationError", "parse_error"),
                               ("LengthFinishReasonError", "truncated")):
            self.assertEqual(diagnose(type(name, (Exception,), {})("private-response"))["category"], category)
        outer = RuntimeError("private-response")
        outer.__cause__ = HTTPFailure(429, 1234)
        self.assertEqual(diagnose(outer)["http_status"], 429)
        self.assertEqual(diagnose(outer)["provider_error_code"], "1234")

    def test_max_workers_bounds_parallel_tasks_and_keeps_input_order(self):
        barrier, lock = threading.Barrier(2), threading.Lock()
        active, peak = 0, 0

        def scorer(sample, metric):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=3)
            with lock:
                active -= 1
            return 0.5

        report = ragas_eval.score(collection(6), config(max_workers=2, max_retries=0), scorer=scorer)
        self.assertEqual(peak, 2)
        self.assertEqual([case["id"] for case in report["cases"]], list(range(6)))
        self.assertEqual(report["summary"]["metrics"]["faithfulness"]["valid"], 6)

    def test_cli_environment_and_explicit_zero_override(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source, out = Path(folder) / "collect.json", Path(folder) / "score.json"
            source.write_text(json.dumps(collection()))
            env = {"TEST_JUDGE_KEY": "sk-test-only", "JUDGE_MODEL": "glm-5.3-flash",
                   "JUDGE_BASE_URL": "https://judge.invalid/v1", "JUDGE_MAX_RETRIES": "5",
                   "JUDGE_RETRY_BACKOFF_S": "0.2", "JUDGE_MAX_WORKERS": "7", "JUDGE_MAX_TOKENS": "8192",
                   "JUDGE_EXTRA_BODY": '{"thinking":{"type":"enabled"}}'}
            argv = ["score", "--in", str(source), "--out", str(out), "--judge-key-env", "TEST_JUDGE_KEY",
                    "--metrics", "faithfulness"]
            with patch.dict(os.environ, env, clear=True), patch.object(ragas_eval, "load_judge_env", return_value={}):
                ragas_eval.main(argv, scorer=lambda *a: 0.5)
                meta = json.loads(out.read_text())["meta"]
                self.assertEqual((meta["max_retries"], meta["retry_backoff_s"], meta["max_workers"], meta["max_tokens"]),
                                 (5, 0.2, 7, 8192))
                self.assertEqual(meta["judge_parameters"], {"thinking": {"type": "enabled"}})
                ragas_eval.main([*argv, "--max-retries", "0", "--retry-backoff-s", "0", "--max-workers", "1",
                                 "--max-tokens", "16384"], scorer=lambda *a: 0.5)
                meta = json.loads(out.read_text())["meta"]
                self.assertEqual((meta["max_retries"], meta["retry_backoff_s"], meta["max_workers"], meta["max_tokens"]),
                                 (0, 0, 1, 16384))

    def test_configuration_rejects_invalid_limits(self):
        for options in ({"max_retries": -1}, {"retry_backoff_s": -1}, {"retry_backoff_s": float("nan")},
                        {"max_workers": 0}, {"max_tokens": 0}, {"max_workers": 1.5}, {"max_retries": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                config(**options)

    def test_cli_no_env_file_never_calls_config_file_loader(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            source, out = Path(folder) / "collect.json", Path(folder) / "score.json"
            source.write_text(json.dumps(collection()))
            env = {"TEST_JUDGE_KEY": "sk-test-only", "JUDGE_MODEL": "glm-5.3-flash",
                   "JUDGE_BASE_URL": "https://judge.invalid/v1"}
            with patch.dict(os.environ, env, clear=True), patch.object(
                    ragas_eval, "load_judge_env", side_effect=AssertionError("不允许读取配置文件")) as loader:
                result = ragas_eval.main([
                    "score", "--in", str(source), "--out", str(out), "--judge-key-env", "TEST_JUDGE_KEY",
                    "--metrics", "faithfulness", "--no-env-file"], scorer=lambda *a: 0.5)
                self.assertEqual(result, 0)
                loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
