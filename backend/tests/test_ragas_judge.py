"""Judge 调用回归：仅标准库，可用 unittest 单独运行，不加载服务或评估依赖。"""
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


def response(text="json", finish_reason="stop"):
    return SimpleNamespace(generations=[[SimpleNamespace(
        text=text, generation_info={"finish_reason": finish_reason},
        message=SimpleNamespace(response_metadata={}, tool_calls=[]))]])


@contextmanager
def fake_dependencies(evaluate):
    """伪造公开依赖接口，验证适配器传参和回调，不导入 SDK。"""
    modules = {name: ModuleType(name) for name in (
        "ragas", "ragas.llms", "ragas.embeddings", "ragas.metrics", "ragas.run_config",
        "langchain_openai", "langchain_core", "langchain_core.callbacks")}
    clients = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    modules["ragas"].EvaluationDataset = SimpleNamespace(from_list=lambda rows: rows)
    modules["ragas"].evaluate = evaluate
    modules["ragas.llms"].LangchainLLMWrapper = lambda client: client
    modules["ragas.embeddings"].LangchainEmbeddingsWrapper = lambda client: client
    for name in ("Faithfulness", "ResponseRelevancy", "LLMContextPrecisionWithReference", "LLMContextRecall"):
        setattr(modules["ragas.metrics"], name, lambda **kwargs: SimpleNamespace(**kwargs))
    modules["ragas.run_config"].RunConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    modules["langchain_openai"].ChatOpenAI = clients
    modules["langchain_openai"].OpenAIEmbeddings = Mock(side_effect=AssertionError("embedding 不应初始化"))
    modules["langchain_core.callbacks"].BaseCallbackHandler = type("BaseCallbackHandler", (), {})
    with patch.dict("sys.modules", modules), patch.dict(os.environ, {"RAGAS_DO_NOT_TRACK": "true"}):
        yield clients


class JudgeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
