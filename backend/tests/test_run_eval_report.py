"""报告契约测试：只用注入配置、假 pipeline 和本地临时文件。"""
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.contracts import AppError
from eval import run_eval

REAL_GIT_COMMIT = run_eval.git_commit


@pytest.fixture(autouse=True)
def fake_git(monkeypatch):
    monkeypatch.setattr(run_eval, "git_commit", lambda: "abcdef1")


def test_meta_uses_settings_allowlist_and_never_exports_credentials():
    secrets = ["sk-fake-llm-private", "sk-fake-rerank-private", "db-password-private",
               "root-password-private", "redis-password-private", "legacy-key-private"]
    settings = Settings(
        _env_file=None, llm_api_key=secrets[0], rerank_remote_api_key=secrets[1],
        mysql_password=secrets[2], mysql_root_password=secrets[3],
        redis_url=f"redis://user:{secrets[4]}@redis.example/0",
        legacy_llm_api_key=secrets[5],
        llm_base_url="https://url-user:url-password@llm.example:8443/private-path?token=url-token#fragment",
        bge_m3_path="/private/model-directory/bge-m3-test/", rerank_provider="remote",
        rerank_remote_base_url="https://rerank.example/private-api", rerank_remote_model="fake-rerank",
        router_model="fake-router", generate_model="fake-generator", rerank_enabled=False,
        rerank_candidates=17, rerank_top_k=3, rerank_text_mode="parent", refuse_threshold=0.3,
        remote_refuse_threshold=0.4, remote_rag_context_min_score=0.5, rag_context_min_score=0.2,
        faq_threshold=0.95, speculative_retrieval=False,
        llm_extra_body={"private": "extra-body-private"},
    )
    meta = run_eval.build_meta(settings, row_count=0)
    serialized = json.dumps(meta)
    for secret in [*secrets, "url-user", "url-password", "url-token", "fragment", "private-path",
                   "private-api", "extra-body-private", "/private/model-directory"]:
        assert secret not in serialized
    assert not {"llm_api_key", "rerank_remote_api_key", "mysql_password"} & meta["model_config"].keys()
    snapshot = meta["model_config"]
    assert snapshot == {
        "router_model": "fake-router", "generate_model": "fake-generator", "rerank_provider": "remote",
        "rerank_enabled": False, "rerank_candidates": 17, "rerank_top_k": 3, "rerank_text_mode": "parent",
        "refuse_threshold": 0.3, "remote_refuse_threshold": 0.4, "remote_rag_context_min_score": 0.5,
        "rag_context_min_score": 0.2, "faq_threshold": 0.95, "speculative_retrieval": False,
        "warmup_on_start": True, "llm_base_url_host": "llm.example", "bge_m3_path": "bge-m3-test",
        "rerank_remote_model": "fake-rerank", "rerank_remote_base_url_host": "rerank.example",
    }
    assert meta["git_commit"] == "abcdef1" and meta["label"] == ""
    assert datetime.fromisoformat(meta["generated_at"]).utcoffset() == timedelta(0)
    assert meta["python_version"] == run_eval.platform.python_version()
    # 即使注入未经过字段校验的配置，白名单字段中的 SecretStr 也不可序列化导出。
    settings = settings.model_copy(update={"router_model": SecretStr("another-private-value")})
    meta = run_eval.build_meta(settings, row_count=0)
    assert meta["model_config"]["router_model"] is None
    assert "another-private-value" not in json.dumps(meta)


def test_local_meta_omits_remote_model_and_remote_host_and_remote_uses_effective_host():
    settings = Settings(_env_file=None, llm_base_url="https://user:password@llm.example/api")
    local = run_eval.build_meta(settings, row_count=0)["model_config"]
    assert "rerank_remote_model" not in local and "rerank_remote_base_url_host" not in local
    remote = run_eval.build_meta(settings.model_copy(update={"rerank_provider": "remote"}),
                                 row_count=0)["model_config"]
    assert remote["rerank_remote_base_url_host"] == "llm.example"
    assert run_eval.host_only("http://[invalid") is None
    assert run_eval.host_only(SecretStr("secret")) is None


def test_data_and_prompt_hashes_are_stable_and_track_raw_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "PROMPT_DIR", tmp_path)
    generate = b"generate prompt\r\n"
    router = "路由提示词\n".encode()
    general = "闲聊提示词\n".encode()
    (tmp_path / "generate.md").write_bytes(generate)
    (tmp_path / "router.md").write_bytes(router)
    (tmp_path / "general.md").write_bytes(general)
    data = b'{"question":"test","expected_route":"general"}\n\n'
    settings = Settings(_env_file=None)
    kwargs = dict(row_count=1, eval_file="data/test.jsonl", eval_bytes=data)
    first = run_eval.build_meta(settings, **kwargs)
    second = run_eval.build_meta(settings, **kwargs)
    assert first["eval_file"] == second["eval_file"] == {
        "path": "data/test.jsonl", "line_count": 2, "row_count": 1,
        "sha256": hashlib.sha256(data).hexdigest()[:12],
    }
    assert first["prompt_hashes"] == second["prompt_hashes"] == {
        "generate.md": hashlib.sha256(generate).hexdigest()[:12],
        "router.md": hashlib.sha256(router).hexdigest()[:12],
        "general.md": hashlib.sha256(general).hexdigest()[:12],
    }
    (tmp_path / "router.md").write_bytes(router + b"changed")
    third = run_eval.build_meta(settings, **{**kwargs, "eval_bytes": data + b"\n"})
    assert third["eval_file"]["sha256"] != first["eval_file"]["sha256"]
    assert third["prompt_hashes"]["router.md"] != first["prompt_hashes"]["router.md"]
    assert third["prompt_hashes"]["generate.md"] == first["prompt_hashes"]["generate.md"]
    assert third["prompt_hashes"]["general.md"] == first["prompt_hashes"]["general.md"]
    (tmp_path / "general.md").write_bytes(general + b"changed")
    fourth = run_eval.build_meta(settings, **kwargs)
    assert fourth["prompt_hashes"]["general.md"] != first["prompt_hashes"]["general.md"]
    (tmp_path / "general.md").unlink()
    assert run_eval.prompt_hash("general.md") is None
    (tmp_path / "router.md").unlink()
    assert run_eval.prompt_hash("router.md") is None


@pytest.mark.parametrize("outcome,expected", [
    (SimpleNamespace(stdout="abcdef1\n"), "abcdef1"),
    (SimpleNamespace(stdout="unexpected-sensitive-output"), None),
    (FileNotFoundError("private-path"), None),
    (subprocess.CalledProcessError(128, "git", stderr="private-error"), None),
    (subprocess.TimeoutExpired("git", 2), None),
])
def test_git_commit_is_best_effort(outcome, expected, monkeypatch):
    # 绕过本文件的自动替身，直接验证生产函数及其 subprocess 参数。
    command = Mock(**({"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}))
    monkeypatch.setattr(run_eval.subprocess, "run", command)
    assert REAL_GIT_COMMIT() == expected
    command.assert_called_once_with(["git", "rev-parse", "--short", "HEAD"], cwd=run_eval.PROJECT_ROOT,
                                    capture_output=True, text=True, check=True, timeout=2)


def test_cases_route_groups_and_failures_preserve_legacy_fields(monkeypatch):
    definitions = [
        ("faq", "cache", None, "faq"), ("cache", "answer", None, "cache"),
        ("hit", "rag", "doc1.md", "rag"), ("miss", "rag", "doc2.md", "rag"),
        ("general", "general", None, "general"), ("wrong-route", "rag", None, "general"),
        ("false-refusal", "rag", "doc2.md", "fallback"), ("false-answer", "fallback", None, "rag"),
        ("refusal", "fallback", None, "fallback"), ("app-error", "rag", "doc2.md", "error"),
        ("unexpected-error", "general", None, "error"),
    ]
    rows = [dict(id=i, question=q, expected_route=expected, expected_doc=doc)
            for i, (q, expected, doc, _) in enumerate(definitions)]
    rows[0]["history"] = [{"role": "user", "content": "private-history"},
                          {"role": "assistant", "content": "private-answer"}]
    routes = {q: route for q, _, _, route in definitions}
    async def chat(request, *, trace_sink, **kwargs):
        trace_sink.append(SimpleNamespace(stage="cache", ms=2))
        if request.message == "app-error":
            raise AppError(50201, 502, "sk-fake-exception-secret https://user:password@host/path")
        if request.message == "unexpected-error":
            raise RuntimeError("internal-private-data")
        route = routes[request.message]
        sources = ([SimpleNamespace(index=1, doc_id="hashed-doc1", title="资料1", score=0.9)]
                   if route == "rag" else [])
        return SimpleNamespace(route=route, answer="说明", sources=sources)
    monkeypatch.setattr(run_eval, "run_chat", chat)
    # 每题分别耗时 10、20、...、110ms，让分组分位数可精确断言。
    clock = [time for i in range(len(rows)) for time in (i, i + (i + 1) / 100)]
    monkeypatch.setattr(run_eval, "perf_counter", Mock(side_effect=clock))
    report = asyncio.run(run_eval.evaluate(rows, settings=Settings(_env_file=None), services=object(),
                                           doc_ids={"doc1.md": "hashed-doc1", "doc2.md": "hashed-doc2"}))
    cases = report["cases"]
    assert [case["id"] for case in cases] == list(range(11))
    assert cases[0]["history_count"] == 2
    assert cases[0]["cache_hit"] is False and cases[0]["faq_hit"] is True
    assert cases[1]["cache_hit"] is True and cases[1]["faq_hit"] is False
    assert cases[2]["expected"] == {"route": "rag", "doc": "doc1.md", "should_refuse": False}
    assert cases[2]["actual"] == {"route": "rag", "sources": ["doc1.md"], "is_refusal": False}
    assert cases[8]["expected"]["should_refuse"] is True and cases[8]["actual"]["is_refusal"] is True
    assert cases[9]["actual"]["is_refusal"] is None
    assert [case["error_types"] for case in cases] == [
        [], [], [], ["来源未命中"], [], ["路由错"], ["路由错", "误拒"],
        ["路由错", "误答"], [], ["异常"], ["异常"],
    ]
    assert [case["passed"] for case in cases] == [True, True, True, False, True, False, False, False, True, False, False]
    assert cases[9]["error"] == {"code": 50201, "category": "AppError"}
    assert cases[10]["error"] == {"code": None, "category": "Exception"}
    assert all(case["stage_ms"] == {"cache": 2} for case in cases)
    assert report["failures"] == [case for case in cases if not case["passed"]]
    assert [case["id"] for case in report["failures"]] == [3, 5, 6, 7, 9, 10]
    stats = report["route_stats"]
    assert set(stats) == {"faq", "cache", "rag", "general", "fallback", "error"}
    for route, count, passed, p50, p90 in [
        ("faq", 1, 1, 10, 10), ("cache", 1, 1, 20, 20), ("rag", 3, 1, 40, 72),
        ("general", 2, 1, 55, 59), ("fallback", 2, 1, 80, 88), ("error", 2, 0, 105, 109),
    ]:
        assert stats[route] == {"total": count, "passed": passed,
                                "total_p50_ms": pytest.approx(p50), "total_p90_ms": pytest.approx(p90)}
    assert sum(group["total"] for group in stats.values()) == report["total"]
    assert {"total", "completed", "errors", "error_distribution", "route_accuracy", "rag_total",
            "rag_source_hits", "rag_source_hit_rate", "route_errors", "stage_mean_ms", "stage_p50_ms",
            "stage_p90_ms", "total_p50_ms", "total_p90_ms", "refused_questions", "results",
            "false_refusals", "false_answers", "trace_enabled", "api_trace_allowed", "note"} <= report.keys()
    for result in report["results"]:
        assert {"question", "expected_route", "route", "route_correct", "expected_doc", "source_hit",
                "error_code", "is_error", "answer", "sources", "total_ms", "stage_ms"} <= result.keys()
    serialized = json.dumps(report)
    for private in ("sk-fake-exception-secret", "user:password", "internal-private-data", "private-history", "private-answer"):
        assert private not in serialized


@pytest.mark.parametrize("route,source_id,passed,legacy_hit", [
    ("faq", "faq:1", True, False),
    ("cache", "faq:1", True, False),
    ("rag", "other-doc", False, False),
    ("rag", "expected-doc", True, True),
])
def test_answer_with_reference_uses_actual_route_for_case_pass_but_preserves_rag_metrics(
        route, source_id, passed, legacy_hit, monkeypatch, capsys):
    source = SimpleNamespace(index=1, doc_id=source_id, title="资料", score=0.9)
    monkeypatch.setattr(run_eval, "run_chat", AsyncMock(
        return_value=SimpleNamespace(route=route, answer="说明", sources=[source])))
    report = asyncio.run(run_eval.evaluate(
        [{"id": "reference-case", "question": "参考问题", "expected_route": "answer", "expected_doc": "doc.md"}],
        settings=Settings(_env_file=None), services=object(), doc_ids={"doc.md": "expected-doc"}))
    case = report["cases"][0]
    assert case["matched_by"] == route
    assert case["source_hit"] is (legacy_hit if route == "rag" else None)
    assert case["passed"] is passed
    assert case["error_types"] == ([] if passed else ["来源未命中"])
    assert report["failures"] == ([] if passed else [case])
    assert report["route_stats"][route]["total"] == 1
    assert report["route_stats"][route]["passed"] == int(passed)
    # 旧 results 和 RAG 指标仍将携带参考文档的 FAQ/cache 行计入分母。
    assert report["results"][0]["route_correct"] is True
    assert report["results"][0]["source_hit"] is legacy_hit
    assert report["rag_total"] == 1 and report["rag_source_hits"] == int(legacy_hit)
    assert report["rag_source_hit_rate"] == float(legacy_hit)
    run_eval.print_report(report)
    assert f"RAG sources 命中率：{float(legacy_hit):.2%} ({int(legacy_hit)}/1)" in capsys.readouterr().out


def test_empty_report_and_mixed_refusal_expectation(monkeypatch):
    settings = Settings(_env_file=None)
    report = asyncio.run(run_eval.evaluate([], settings=settings, services=object()))
    assert report["cases"] == report["failures"] == []
    assert report["meta"]["eval_file"] == {"path": None, "line_count": None, "row_count": 0, "sha256": None}
    assert all(group == {"total": 0, "passed": 0, "total_p50_ms": None, "total_p90_ms": None}
               for group in report["route_stats"].values())
    monkeypatch.setattr(run_eval, "run_chat", AsyncMock(return_value=SimpleNamespace(route="fallback", answer="拒答", sources=[])))
    report = asyncio.run(run_eval.evaluate([{"question": "测试", "expected_route": ["rag", "fallback"]}],
                                           settings=settings, services=object()))
    assert report["cases"][0]["expected"]["should_refuse"] is None
    assert report["cases"][0]["passed"] is True and "id" not in report["cases"][0]


def test_source_names_use_ingest_hash_and_strip_paths(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "source.md").write_text("# 文档", encoding="utf-8")
    monkeypatch.setattr(run_eval, "resolve_data_path", lambda _: tmp_path)
    names = run_eval.source_name_index({"/private/expected.md": "injected-id"})
    assert names[hashlib.sha256(b"nested/source.md").hexdigest()[:24]] == "source.md"
    assert names["injected-id"] == "expected.md"


@pytest.mark.parametrize("label", [None, "baseline-test"])
def test_cli_exports_label_and_hash_of_exact_input_without_changing_console(tmp_path, monkeypatch, capsys, label):
    data = b'{"id":"one","question":"hello","expected_route":"general"}\n\n'
    dataset, output = tmp_path / "data.jsonl", tmp_path / "report.json"
    dataset.write_bytes(data)
    settings = Settings(_env_file=None, warmup_on_start=False, llm_api_key="sk-fake-cli-key")
    service = SimpleNamespace(aclose=AsyncMock(), warmup=AsyncMock())
    monkeypatch.setattr(run_eval, "Services", Mock(return_value=service))
    monkeypatch.setattr(run_eval, "run_chat", AsyncMock(return_value=SimpleNamespace(route="general", answer="hello", sources=[])))
    monkeypatch.setattr(run_eval, "perf_counter", Mock(side_effect=[0, 0.01]))
    args = ["--eval-file", str(dataset), "--output", str(output)]
    if label is not None:
        args.extend(["--label", label])
    assert run_eval.main(args, settings=settings) == 0
    exported = output.read_text(encoding="utf-8")
    report = json.loads(exported)
    assert report["meta"]["label"] == (label or "")
    assert report["meta"]["eval_file"] == {"path": str(dataset), "line_count": 2, "row_count": 1,
                                            "sha256": hashlib.sha256(data).hexdigest()[:12]}
    assert report["failures"] == [] and report["cases"][0]["id"] == "one"
    assert "sk-fake-cli-key" not in exported
    service.aclose.assert_awaited_once()
    service.warmup.assert_not_awaited()
    assert capsys.readouterr().out == (
        "总题数：1；完成：1；错误：0\n错误分布：无\n路由准确率：100.00%\n"
        "RAG sources 命中率：0.00% (0/0)\n各阶段平均耗时(ms)：无 trace 数据\n"
        "各阶段 p50(ms)：无\n各阶段 p90(ms)：无\n总耗时(ms)：p50=10.000，p90=10.000\n"
        "路由错误的题目：\n无\n误拒答（应答却 fallback）：0 题\n无\n误答（应拒却回答）：0 题\n无\n"
    )
