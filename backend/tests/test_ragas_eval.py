"""离线 collect/score：禁真实服务、模型、LLM 与 RAGAS 依赖。"""
import asyncio
import builtins
import hashlib
import json
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import Settings
from app.contracts import RouteDecision
from chat_fakes import FakeServices
from eval import ragas_eval, run_eval
from eval.ragas_backend import EvaluationConfigurationError, EvaluationDependencyError, make_scorer
from eval.reports import safe_path, secret_safe_logs, write_report


@pytest.fixture(autouse=True)
def no_real_judge_env_file(monkeypatch, tmp_path):
    # CLI 测试只用进程环境或下文显式创建的 env-sample，绝不读取真实 .env*。
    monkeypatch.setattr(ragas_eval, "JUDGE_ENV_FILE", tmp_path / "missing-judge-config")


def rag_case(index=0, **changes):
    case = {"id": index, "question": f"问题{index}", "answer": "回答", "reference": "参考",
            "route": "rag", "scenario": "product", "risk": "normal",
            "contexts": [{"index": 1, "text": "只允许存在于采集文件的全文"}]}
    return {**case, **changes}


def collection(cases):
    return {"phase": "collect", "schema_version": 1, "meta": {
        "git_commit": "abcdef1", "eval_file": {"sha256": "123456789abc"},
        "model_config": {"generate_model": "fake-generator"}}, "cases": cases}


def test_collect_preserves_multiple_expected_documents(monkeypatch):
    expected = ["payment_invoices.md", "payment_invoice_correction_case.md"]

    async def chat(request, **kwargs):
        kwargs["collector"]({"route": "rag", "answer": "说明[1]", "history": [], "sources": [],
                             "contexts": [{"index": 1, "text": "案例中的开票事实"}]})

    monkeypatch.setattr("app.pipeline.run_chat", chat)
    report = asyncio.run(ragas_eval.collect(
        [{"question": "发票", "expected_route": "answer", "expected_doc": expected,
          "reference": "信息完整后一般3个工作日内开出"}],
        settings=Settings(_env_file=None), services=FakeServices()))
    assert report["cases"][0]["expected_doc"] == expected
    assert report["cases"][0]["status"] != "error"
    assert report["cases"][0]["contexts"][0]["text"] == "案例中的开票事实"


@pytest.mark.parametrize("expected", [[], "", " ", 0, False, {}, ["a.md", None], [["a.md"]]])
def test_collect_rejects_invalid_expected_document_before_chat(monkeypatch, expected):
    chat = AsyncMock()
    monkeypatch.setattr("app.pipeline.run_chat", chat)
    with pytest.raises(ValueError, match="expected_doc"):
        asyncio.run(ragas_eval.collect(
            [{"question": "标注错误", "expected_route": "answer", "expected_doc": expected}],
            settings=Settings(_env_file=None), services=FakeServices()))
    chat.assert_not_called()


def config(**changes):
    return ragas_eval.ScoreConfig(**{**dict(judge_model="glm-5.3-flash", judge_base_url="https://judge.test/v1",
                                           judge_key_env="TEST_JUDGE_KEY", api_key="sk-fake-test-key",
                                           max_retries=0, max_workers=1,
                                           metrics=("faithfulness",)), **changes})


def test_score_errors_invalid_nan_and_group_denominators():
    cases = [rag_case(i, risk="boundary" if i > 0 else "normal") for i in range(7)]
    cases += [rag_case(7, route="faq", contexts=[]), rag_case(8, route="error", status="error")]
    scorer = Mock(side_effect=[
        {"value": 0.8, "usage": {"input_tokens": 11, "output_tokens": 2, "total_tokens": 13}},
        TimeoutError("sk-fake-test-key"), ValueError("sk-fake-test-key"), float("nan"),
        {"status": "error"}, {"status": "invalid"}, 0,
    ])
    result = ragas_eval.score(collection(cases), config(), scorer=scorer)
    stats = result["summary"]["metrics"]["faithfulness"]
    assert stats == {"total": 9, "scorable": 7, "attempted": 7, "valid": 2, "error": 4,
                     "invalid": 2, "not_applicable": 1, "skipped_limit": 0, "mean": 0.4, "median": 0.4}
    assert result["summary"]["groups"]["risk"]["normal"]["faithfulness"]["total"] == 3
    assert result["summary"]["groups"]["scenario"]["product"]["faithfulness"] == stats
    assert result["summary"]["not_applicable_by_route"] == {"faq": 1}
    assert result["summary"]["lowest_samples"]["faithfulness"][0]["id"] == 6
    assert result["summary"]["judge_usage"]["total_tokens"] == 13
    assert result["summary"]["judge_usage"]["token_usage_available_calls"] == 1
    assert result["summary"]["judge_usage"]["token_usage_missing_calls"] == 6
    assert result["summary"]["judge_usage"]["elapsed_ms"] >= 0
    text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert "sk-fake-test-key" not in text and "只允许存在于采集文件的全文" not in text
    assert "retrieved_contexts" not in text
    assert result["cases"][1]["scores"]["faithfulness"]["reason"] == "judge_timeout"


def test_sensitive_and_fallback_counts_with_legacy_unknown():
    report = ragas_eval.score(collection([
        rag_case(0, route="fallback", intent="sensitive", refuse_reason="sensitive"),
        rag_case(1, route="fallback", intent="knowledge", refuse_reason="no_docs"),
        rag_case(2, route="fallback"),
    ]), config(), scorer=Mock(side_effect=AssertionError("不适用样本不得评分")))
    assert report["summary"]["not_applicable_by_route"] == {"fallback": 3}
    assert report["summary"]["not_applicable_by_category"] == {"sensitive": 1, "fallback": 1, "unknown": 1}
    assert report["cases"][0]["scores"]["faithfulness"]["reason"] == "route_sensitive"
    assert report["cases"][2]["intent"] == "unknown"
    assert report["cases"][2]["refuse_reason"] == "unknown"
    assert report["summary"]["metrics"]["faithfulness"]["not_applicable"] == 3


def test_collect_counts_sensitive_separately_from_no_docs(monkeypatch):
    monkeypatch.setattr(run_eval, "git_commit", lambda: None)
    fake = FakeServices()
    fake.route.side_effect = [RouteDecision(intent="sensitive", rewritten_query="问题"),
                              RouteDecision(intent="knowledge", rewritten_query="问题")]
    fake.retrieve.return_value = []
    report = asyncio.run(ragas_eval.collect(
        [{"question": "问题", "expected_route": "fallback"}] * 2,
        settings=Settings(_env_file=None, speculative_retrieval=False), services=fake))
    assert report["route_stats"] == {"fallback": 2}
    assert report["not_applicable_by_category"] == {"sensitive": 1, "fallback": 1}
    assert [case["refuse_reason"] for case in report["cases"]] == ["sensitive", "no_docs"]


def test_limit_preserves_every_row_and_missing_reference_invalid_per_metric():
    scorer = Mock(return_value=0.75)
    report = ragas_eval.score(collection([rag_case(0, route="general"), rag_case(1, reference=None), rag_case(2)]),
                              config(max_samples=1, metrics=("faithfulness", "context_recall")), scorer=scorer)
    assert len(report["cases"]) == 3
    assert report["cases"][0]["status"] == "not_applicable"
    assert report["cases"][1]["scores"]["faithfulness"]["value"] == 0.75
    assert report["cases"][1]["scores"]["context_recall"]["reason"] == "missing_reference"
    assert report["cases"][2]["status"] == "skipped_limit"
    scorer.assert_called_once()


def test_all_metrics_and_injected_judge_embedding_factory(monkeypatch):
    judge, embeddings = object(), object()
    scorer = Mock(return_value=0.9)
    factory = Mock(return_value=scorer)
    monkeypatch.setattr(ragas_eval, "make_scorer", factory)
    settings = config(metrics=ragas_eval.METRICS)
    report = ragas_eval.score(collection([rag_case()]), settings, judge=judge, embeddings=embeddings)
    factory.assert_called_once_with(settings, judge=judge, embeddings=embeddings)
    assert scorer.call_count == 4
    assert set(report["cases"][0]["scores"]) == set(ragas_eval.METRICS)
    for call in scorer.call_args_list:
        assert call.args[0] == {"user_input": "问题0", "response": "回答", "reference": "参考",
                                "retrieved_contexts": ["只允许存在于采集文件的全文"]}


def test_over_budget_missing_reference_still_invalid_for_reference_metric():
    report = ragas_eval.score(collection([rag_case(0), rag_case(1, reference=None)]),
                              config(max_samples=1, metrics=("faithfulness", "context_precision")),
                              scorer=lambda *args: 0.5)
    assert report["cases"][1]["scores"]["faithfulness"]["status"] == "skipped_limit"
    assert report["cases"][1]["scores"]["context_precision"]["status"] == "invalid"
    assert report["summary"]["metrics"]["context_precision"]["scorable"] == 1


@pytest.mark.parametrize("value", [float("inf"), -0.1, 1.1, None, True, "0.5"])
def test_invalid_values_never_become_zero(value):
    result = ragas_eval.score(collection([rag_case()]), config(), scorer=lambda *args: value)
    stats = result["summary"]["metrics"]["faithfulness"]
    assert stats["invalid"] == 1 and stats["valid"] == 0
    assert stats["mean"] is None and stats["median"] is None


def test_score_cli_reads_saved_file_only_and_redacts_key_everywhere(tmp_path, monkeypatch, capsys):
    key = "sk-deliberately-in-echoed-fields"
    monkeypatch.setenv("TEST_JUDGE_KEY", key)
    path, out = tmp_path / "collect.json", tmp_path / "score.json"
    path.write_text(json.dumps(collection([rag_case(question=f"问题 {key}", answer=f"答案 {key}")])) , encoding="utf-8")
    monkeypatch.setattr(ragas_eval, "collect", Mock(side_effect=AssertionError("score 不得采集")))
    report_scorer = Mock(return_value={"value": 0.5, "reason": f"评分原因 {key}"})
    assert ragas_eval.main(["score", "--in", str(path), "--out", str(out), "--judge-model", "glm-5.3-flash",
                            "--judge-base-url", f"https://user:{key}@judge.test/v1", "--judge-key-env", "TEST_JUDGE_KEY",
                            "--metrics", "faithfulness"], scorer=report_scorer) == 0
    report_scorer.assert_called_once()
    for text in (out.read_text(), out.with_suffix(".md").read_text(), capsys.readouterr().out):
        assert key not in text and "只允许存在于采集文件的全文" not in text
    exported = json.loads(out.read_text())
    assert exported["meta"]["judge_model"] == "glm-5.3-flash"
    assert exported["cases"][0]["question"].startswith("问题")
    assert exported["cases"][0]["answer"].startswith("答案")


def test_evaluation_log_and_exception_redacted_without_changing_factory(caplog):
    key = config().api_key
    original = logging.getLogRecordFactory()

    def scorer(*args):
        try:
            raise ValueError(key)
        except ValueError:
            assert logging.getLogRecordFactory() is original
            logging.getLogger("ragas.fake").exception("SDK received %s", key)
        return 0.5

    ragas_eval.score(collection([rag_case()]), config(), scorer=scorer)
    assert key not in caplog.text and "[REDACTED]" in caplog.text
    assert logging.getLogRecordFactory() is original


def test_log_filter_scope_new_children_and_cleanup(caplog):
    key = config().api_key
    related = [logging.getLogger(name) for name in ("eval.test_redaction", "ragas.test_redaction")]
    namespaces = [logging.getLogger(name) for name in ("eval", "ragas")]
    unrelated = logging.getLogger("app.unrelated_redaction")
    watched = [*related, *namespaces, unrelated, logging.getLogger()]
    before = {logger: (list(logger.filters), list(logger.handlers)) for logger in watched}
    original = logging.getLogRecordFactory()
    with secret_safe_logs((key,)):
        assert logging.getLogRecordFactory() is original
        for logger in related:
            assert len(logger.filters) == len(before[logger][0]) + 1
            logger.error("相关日志 %s", key)
        # 祖先 logger 的 Filter 不继承；此子 logger 的记录也必须被脱敏。
        dynamic = logging.getLogger(f"eval.created_in_scope_{id(caplog)}")
        dynamic.error("新子日志 %s", key)
        unrelated.error("无关日志 %s", key)
        assert unrelated.filters == before[unrelated][0]
    for logger in watched:
        assert (logger.filters, logger.handlers) == before[logger]
    assert not dynamic.filters and not dynamic.handlers
    for record in caplog.records:
        if record.name.startswith(("eval.", "ragas.")):
            assert key not in record.getMessage() and "[REDACTED]" in record.getMessage()
        elif record.name == unrelated.name:
            assert key in record.getMessage()
    assert logging.getLogRecordFactory() is original


def test_log_filters_removed_on_exception():
    logger = logging.getLogger("eval.exception_cleanup")
    before = list(logger.filters)
    with pytest.raises(RuntimeError):
        with secret_safe_logs((config().api_key,)):
            raise RuntimeError("test")
    assert logger.filters == before


@pytest.mark.parametrize("options,key,reason", [
    ([], "", "Judge key 环境变量为空"),
    (["--max-samples", "0"], "sk-fake-error-key", "max_samples 和 timeout_s 必须为正数"),
    (["--timeout-s", "nan"], "sk-fake-error-key", "max_samples 和 timeout_s 必须为正数"),
    (["--max-workers", "0"], "sk-fake-error-key", "max_workers 和 max_tokens 必须为正整数"),
    (["--max-tokens", "0"], "sk-fake-error-key", "max_workers 和 max_tokens 必须为正整数"),
    (["--max-retries", "-1"], "sk-fake-error-key", "max_retries 必须非负"),
    (["--retry-backoff-s", "nan"], "sk-fake-error-key", "retry_backoff_s 必须是"),
    (["--metrics", "unknown"], "sk-fake-error-key", "metrics 必须是支持的非重复指标名"),
    (["--out", ".env.report"], "sk-fake-error-key", "评估禁止读写 .env* 文件"),
    (["--in", ".env.input"], "sk-fake-error-key", "评估禁止读写 .env* 文件"),
])
def test_cli_configuration_errors_show_specific_reason(tmp_path, monkeypatch, capsys, options, key, reason):
    monkeypatch.setenv("TEST_JUDGE_KEY", key)
    args = ["score", "--in", str(tmp_path / "collect.json"), "--out", str(tmp_path / "score.json"),
            "--judge-model", "fake", "--judge-base-url", "https://judge.test/v1", "--judge-key-env", "TEST_JUDGE_KEY"]
    with pytest.raises(SystemExit) as caught:
        ragas_eval.main([*args, *options])
    text = capsys.readouterr().err
    assert caught.value.code == 2 and reason in text and "离线评估失败" not in text
    assert not key or key not in text


@pytest.mark.parametrize("error_type", [ValueError, EvaluationConfigurationError, EvaluationDependencyError, RuntimeError])
def test_cli_error_reasons_redacted_and_unknown_errors_generic(tmp_path, monkeypatch, capsys, error_type):
    key = "sk-fake-error-reason-key"
    monkeypatch.setenv("TEST_JUDGE_KEY", key)
    path, out = tmp_path / "collect.json", tmp_path / "score.json"
    path.write_text(json.dumps(collection([rag_case()])), encoding="utf-8")
    monkeypatch.setattr(ragas_eval, "score", Mock(side_effect=error_type(f"明确原因 {key}")))
    with pytest.raises(SystemExit) as caught:
        ragas_eval.main(["score", "--in", str(path), "--out", str(out), "--judge-model", "fake",
                         "--judge-base-url", "https://judge.test/v1", "--judge-key-env", "TEST_JUDGE_KEY"])
    text = capsys.readouterr().err
    assert caught.value.code == 2 and key not in text
    if error_type is RuntimeError:
        assert "离线评估失败" in text and "明确原因" not in text
    else:
        assert "明确原因 [REDACTED]" in text and "离线评估失败" not in text


def test_cli_path_error_is_redacted_before_config_creation(tmp_path, monkeypatch, capsys):
    key = "sk-fake-path-error-key"
    monkeypatch.setenv("TEST_JUDGE_KEY", key)
    monkeypatch.setattr(ragas_eval, "safe_path", Mock(side_effect=ValueError(f"路径拒绝 {key}")))
    with pytest.raises(SystemExit):
        ragas_eval.main(["score", "--in", "unused.json", "--out", "unused.json", "--judge-model", "fake",
                         "--judge-base-url", "https://judge.test/v1", "--judge-key-env", "TEST_JUDGE_KEY"])
    text = capsys.readouterr().err
    assert "路径拒绝 [REDACTED]" in text and key not in text


def test_collect_cli_uses_fake_services_and_existing_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "git_commit", lambda: "abcdef1")
    key = "sk-fake-collect-key"
    settings = Settings(_env_file=None, speculative_retrieval=False, warmup_on_start=False, llm_api_key=key)
    fake = FakeServices().knowledge()
    fake.aclose = AsyncMock()
    dataset, out = tmp_path / "gold.jsonl", tmp_path / "collect.json"
    data = json.dumps({"id": "one", "question": "产品问题", "expected_route": "answer", "reference": "参考答案",
                       "scenario": "product", "risk": "normal", "answerable": True}, ensure_ascii=False).encode()
    dataset.write_bytes(data)
    assert ragas_eval.main(["collect", "--eval-file", str(dataset), "--out", str(out)], settings=settings,
                           services_factory=lambda _: fake) == 0
    report = json.loads(out.read_text())
    assert report["meta"]["eval_file"]["sha256"] == hashlib.sha256(data).hexdigest()[:12]
    assert report["meta"]["git_commit"] == "abcdef1"
    assert report["cases"][0]["contexts"][0]["text"] == "[1] 资料1\n父块完整内容"
    assert report["cases"][0]["reference"] == "参考答案"
    assert report["cases"][0]["status"] == "applicable"
    assert report["route_stats"] == {"rag": 1} and report["failures"] == []
    assert key not in out.read_text()
    assert not out.with_suffix(".md").exists()
    fake.warmup.assert_not_awaited()
    fake.aclose.assert_awaited_once()


def test_collection_exception_is_retained_without_exception_secret(monkeypatch):
    monkeypatch.setattr(run_eval, "git_commit", lambda: None)
    fake = FakeServices()
    fake.route.side_effect = RuntimeError("sk-error-secret")
    result = asyncio.run(ragas_eval.collect(
        [{"question": "失败问题", "expected_route": "general"}],
        settings=Settings(_env_file=None, speculative_retrieval=False), services=fake))
    assert result["status_counts"] == {"error": 1}
    assert len(result["failures"]) == 1
    assert "sk-error-secret" not in json.dumps(result)


def test_missing_ragas_is_clear_and_import_is_delayed(tmp_path, monkeypatch, capsys):
    original_import = builtins.__import__

    def no_ragas(name, *args, **kwargs):
        if name == "ragas" or name.startswith("ragas."):
            raise ModuleNotFoundError("ragas absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ragas)
    # 替身入口不触发 ragas 导入。
    assert ragas_eval.score(collection([rag_case()]), config(), scorer=lambda *a: 0.5)["cases"][0]["status"] == "valid"
    with pytest.raises(EvaluationDependencyError, match="需先安装评估依赖"):
        make_scorer(config())
    path, out = tmp_path / "collect.json", tmp_path / "score.json"
    path.write_text(json.dumps(collection([rag_case()])), encoding="utf-8")
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-fake-no-ragas")
    with pytest.raises(SystemExit) as caught:
        ragas_eval.main(["score", "--in", str(path), "--out", str(out), "--judge-model", "fake",
                         "--judge-base-url", "https://judge.test/v1", "--judge-key-env", "TEST_JUDGE_KEY"])
    assert caught.value.code == 2
    assert "需先安装评估依赖" in capsys.readouterr().err
    assert not out.exists()


def test_empty_report_null_statistics_and_environment_paths_blocked(tmp_path):
    report = ragas_eval.score(collection([]), config(), scorer=lambda *a: pytest.fail("空集不得打分"))
    assert report["summary"]["metrics"]["faithfulness"]["mean"] is None
    write_report(report, tmp_path / "empty.json")
    for name in (".env", ".env.example", ".env.local"):
        with pytest.raises(ValueError, match="禁止读写"):
            safe_path(tmp_path / name)


def test_load_judge_env_reads_only_whitelisted_names(tmp_path):
    env = tmp_path / "env-sample"
    env.write_text("\n".join([
        "# 注释行", "LLM_API_KEY=sk-llm-must-not-leak", "export JUDGE_API_KEY=\"sk-judge-quoted\"",
        "JUDGE_MODEL=glm-5.3-flash  # 行尾注释", "JUDGE_BASE_URL='https://judge.test/v1'",
        "OTHER_SECRET=abc", "JUDGE_UNKNOWN=x", "没有等号的行",
    ]), encoding="utf-8")
    values = ragas_eval.load_judge_env(env)
    assert values == {"JUDGE_API_KEY": "sk-judge-quoted", "JUDGE_MODEL": "glm-5.3-flash",
                      "JUDGE_BASE_URL": "https://judge.test/v1"}
    assert "sk-llm-must-not-leak" not in json.dumps(values)
    assert ragas_eval.load_judge_env(tmp_path / "missing") == {}


def test_score_cli_takes_judge_settings_from_env_file(tmp_path, monkeypatch, capsys):
    env = tmp_path / "env-sample"
    env.write_text("LLM_API_KEY=sk-llm-must-not-leak\nJUDGE_API_KEY=sk-judge-from-file\n"
                   "JUDGE_MODEL=glm-5.3-flash\nJUDGE_BASE_URL=https://judge.test/v1\n", encoding="utf-8")
    monkeypatch.setattr(ragas_eval, "JUDGE_ENV_FILE", env)
    for name in ragas_eval.JUDGE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    path, out = tmp_path / "collect.json", tmp_path / "score.json"
    path.write_text(json.dumps(collection([rag_case()])), encoding="utf-8")
    assert ragas_eval.main(["score", "--in", str(path), "--out", str(out), "--metrics", "faithfulness"],
                           scorer=Mock(return_value=0.9)) == 0
    report = out.read_text(encoding="utf-8")
    assert "sk-judge-from-file" not in report and "sk-llm-must-not-leak" not in report
    assert "sk-judge-from-file" not in capsys.readouterr().out


def test_custom_key_env_never_falls_back_to_non_judge_names_in_env_file(tmp_path, monkeypatch, capsys):
    env = tmp_path / "env-sample"
    env.write_text("LLM_API_KEY=sk-llm-must-not-leak\n", encoding="utf-8")
    monkeypatch.setattr(ragas_eval, "JUDGE_ENV_FILE", env)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    path, out = tmp_path / "collect.json", tmp_path / "score.json"
    path.write_text(json.dumps(collection([rag_case()])), encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        ragas_eval.main(["score", "--in", str(path), "--out", str(out), "--judge-model", "m",
                         "--judge-base-url", "https://judge.test/v1", "--judge-key-env", "LLM_API_KEY"])
    text = capsys.readouterr().err
    assert caught.value.code == 2 and "Judge key 环境变量为空" in text and "sk-llm-must-not-leak" not in text


def _score_config_seen(tmp_path, monkeypatch, model, extra_env=None):
    seen = {}
    original = ragas_eval.score

    def spy(coll, cfg, **kwargs):
        seen["config"] = cfg
        return original(coll, cfg, **kwargs)

    monkeypatch.setattr(ragas_eval, "score", spy)
    monkeypatch.setattr(ragas_eval, "JUDGE_ENV_FILE", tmp_path / "missing-env-file")
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-fake-extra-body-key")
    monkeypatch.delenv("JUDGE_EXTRA_BODY", raising=False)
    for name, value in (extra_env or {}).items():
        monkeypatch.setenv(name, value)
    path, out = tmp_path / "collect.json", tmp_path / "score.json"
    path.write_text(json.dumps(collection([rag_case()])), encoding="utf-8")
    code = ragas_eval.main(["score", "--in", str(path), "--out", str(out), "--judge-model", model,
                            "--judge-base-url", "https://judge.test/v1", "--judge-key-env", "TEST_JUDGE_KEY",
                            "--metrics", "faithfulness"], scorer=Mock(return_value=0.9))
    return code, seen["config"]


def test_glm_judge_defaults_to_no_extra_parameters(tmp_path, monkeypatch):
    code, cfg = _score_config_seen(tmp_path, monkeypatch, "glm-5.3-flash")
    assert code == 0 and cfg.judge_extra_body is None


def test_non_glm_judge_adds_no_extra_body_by_default(tmp_path, monkeypatch):
    code, cfg = _score_config_seen(tmp_path, monkeypatch, "other-judge-model")
    assert code == 0 and cfg.judge_extra_body is None


def test_explicit_extra_body_overrides_glm_default(tmp_path, monkeypatch):
    code, cfg = _score_config_seen(tmp_path, monkeypatch, "glm-5.3-flash",
                                   {"JUDGE_EXTRA_BODY": '{"thinking": {"type": "enabled"}}'})
    assert code == 0 and cfg.judge_extra_body == {"thinking": {"type": "enabled"}}
    code, cfg = _score_config_seen(tmp_path, monkeypatch, "glm-5.3-flash", {"JUDGE_EXTRA_BODY": "{}"})
    assert code == 0 and cfg.judge_extra_body == {}


@pytest.mark.parametrize("raw", ["not json", "[1]", '"text"', "null", '{"x": NaN}'])
def test_invalid_extra_body_is_rejected_with_reason(tmp_path, monkeypatch, capsys, raw):
    with pytest.raises(SystemExit) as caught:
        _score_config_seen(tmp_path, monkeypatch, "glm-5.3-flash", {"JUDGE_EXTRA_BODY": raw})
    assert caught.value.code == 2 and "JUDGE_EXTRA_BODY" in capsys.readouterr().err
