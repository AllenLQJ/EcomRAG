import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import AppError
from app.logging_config import configure_logging, log_exception, request_id
from app.main import create_app
from chat_fakes import FakeServices

QUESTION = "PRIVATE_QUESTION_ONLY_IN_DB_27"
ANSWER = "PRIVATE_ANSWER_ONLY_IN_DB_83"
KEY = "FAKE_SECRET_KEY_91"


def make_app(tmp_path, *, enabled=True, writer=None, services=None, **overrides):
    settings = Settings(_env_file=None, log_dir=str(tmp_path / "logs"), log_to_console=False,
                        llm_api_key=KEY, chat_log_enabled=enabled, **overrides)
    writer = writer or SimpleNamespace(write=AsyncMock(), ensure_schema=AsyncMock())
    fake = services or FakeServices()
    fake.chat_general.return_value = ANSWER
    app = create_app(settings, services=fake, chat_log_writer=writer)
    app.state.rate_limiter.cache._client = lambda: None
    return app, writer


def ask(client):
    return client.post("/api/v1/chat?private_query=DO_NOT_LOG_QUERY", json={
        "session_id": "session-test", "message": QUESTION, "debug": False,
    })


def test_full_request_metadata_log_and_database_originals(tmp_path, caplog):
    app, writer = make_app(tmp_path)
    with TestClient(app) as client:
        result = ask(client)
    assert result.status_code == 200 and result.json()["trace"] is None
    row = writer.write.call_args.args[0]
    assert (row.question, row.answer, row.session_id, row.route, row.error_code) == (
        QUESTION, ANSWER, "session-test", "general", None)
    assert row.request_id == result.headers["X-Request-ID"]
    assert row.latency_ms >= 0 and row.sources == []
    content = (tmp_path / "logs/app.log").read_text()
    # 模拟测试工具自己的请求行，只送入捕获器；它不属于应用日志。
    caplog.handler.handle(logging.LogRecord("httpx2", logging.INFO, __file__, 1,
                                          "POST ?private_query=DO_NOT_LOG_QUERY", (), None))
    # 测试客户端/第三方库日志不属于应用输出；只检查文件及 app.* 记录。
    app_records = "\n".join(record.getMessage() for record in caplog.records
                            if record.name.startswith("app."))
    for output in (content, app_records):
        for secret in (QUESTION, ANSWER, KEY, "DO_NOT_LOG_QUERY", "private_query"):
            assert secret not in output
    assert "chat_done" in content and "request_done" in content
    assert "router" in content and "generate" in content  # trace 被内部记录但不向外返回
    assert "session-test" in content and "route=general" in content and "documents=0" in content
    assert f'request_id={row.request_id}' in content
    assert "method=POST" in content and "status=200" in content


def test_background_failure_cannot_change_response(tmp_path, caplog):
    writer = SimpleNamespace(write=AsyncMock(side_effect=RuntimeError(QUESTION + ANSWER + KEY)),
                             ensure_schema=AsyncMock())
    app, _ = make_app(tmp_path, writer=writer)
    with TestClient(app) as client:
        response = ask(client)
    assert response.status_code == 200 and response.json()["answer"] == ANSWER
    assert "对话记录写入失败" in caplog.text
    assert QUESTION not in caplog.text and ANSWER not in caplog.text and KEY not in caplog.text


def test_app_error_background_row(tmp_path):
    fake = FakeServices()
    fake.route.side_effect = AppError(50201, 502, "模型服务暂时不可用")
    app, writer = make_app(tmp_path, services=fake)
    with TestClient(app) as client:
        response = ask(client)
    assert response.status_code == 502
    row = writer.write.call_args.args[0]
    assert row.answer == "" and row.route == "error" and row.error_code == 50201
    assert row.question == QUESTION and row.request_id == response.json()["request_id"]


def test_rate_limit_app_error_also_records(tmp_path):
    app, writer = make_app(tmp_path, rate_limit_per_min=1)
    app.state.rate_limiter.clock = lambda: 100000
    with TestClient(app) as client:
        assert ask(client).status_code == 200
        assert ask(client).status_code == 429
    assert writer.write.await_count == 2
    row = writer.write.call_args.args[0]
    assert row.route == "error" and row.error_code == 42901


def test_chat_log_disabled_does_not_write_or_migrate(tmp_path):
    app, writer = make_app(tmp_path, enabled=False)
    with TestClient(app) as client:
        assert ask(client).status_code == 200
    writer.write.assert_not_awaited()
    writer.ensure_schema.assert_not_awaited()


def test_startup_schema_failure_is_warning_only(tmp_path, caplog):
    writer = SimpleNamespace(write=AsyncMock(), ensure_schema=AsyncMock(side_effect=RuntimeError(KEY)))
    app, _ = make_app(tmp_path, writer=writer)
    with TestClient(app) as client:
        assert ask(client).status_code == 200
    assert "对话表补列失败" in caplog.text and KEY not in caplog.text


def test_uncaught_exception_records_safe_stack_and_uniform_response(tmp_path, caplog):
    fake = FakeServices()
    fake.route.side_effect = RuntimeError(QUESTION + ANSWER + KEY)
    app, writer = make_app(tmp_path, services=fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = ask(client)
    assert response.status_code == 500 and response.json()["code"] == 50001
    for secret in (QUESTION, ANSWER, KEY):
        assert secret not in caplog.text
        assert secret not in (tmp_path / "logs/app.log").read_text()
    records = [record for record in caplog.records if record.name == "app.main"
               and record.getMessage().startswith("unhandled_exception")]
    assert len(records) == 1 and "RuntimeError" in records[0].getMessage() and "stack=" in records[0].getMessage()
    writer.write.assert_awaited_once()
    row = writer.write.call_args.args[0]
    assert (row.question, row.answer, row.route, row.error_code) == (QUESTION, "", "error", 50001)
    assert row.request_id == response.json()["request_id"]
    assert "status=500" in caplog.text


def test_contextvars_concurrent_requests_do_not_mix(tmp_path, caplog):
    import httpx
    app, _ = make_app(tmp_path)
    async def route(question, history):
        await asyncio.sleep(0)
        from app.contracts import RouteDecision
        return RouteDecision(intent="general", rewritten_query="店铺服务")
    app.state.services.route = route
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await asyncio.gather(*[client.post("/api/v1/chat", json={"session_id": f"s{i}", "message": QUESTION})
                                          for i in range(4)])
    responses = asyncio.run(run())
    ids = {response.json()["request_id"] for response in responses}
    assert len(ids) == 4
    rows = [record for record in caplog.records if record.name == "app.main" and "chat_done" in record.getMessage()]
    assert len(rows) == 4 and {record.request_id for record in rows} == ids
    assert request_id.get() == "-"


def test_logging_rotation_settings_and_no_duplicate_handlers(tmp_path):
    settings = Settings(_env_file=None, log_dir=str(tmp_path), log_backup_days=9, log_to_console=False)
    configure_logging(settings)
    configure_logging(settings)
    owned = [handler for handler in logging.getLogger().handlers if getattr(handler, "_ecomrag", False)]
    assert len(owned) == 1
    handler = owned[0]
    assert handler.when == "MIDNIGHT" and handler.backupCount == 9
    assert handler.baseFilename.endswith("app.log")
    assert handler.utc is True


def test_http_client_loggers_effective_level_is_at_least_warning(tmp_path):
    loggers = [logging.getLogger(name) for name in ("httpx", "httpx2", "httpcore")]
    previous = [logger.level for logger in loggers]
    root = logging.getLogger()
    previous_root = root.level
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        configure_logging(Settings(_env_file=None, log_dir=str(tmp_path), log_to_console=False))
        for logger in loggers:
            assert logger.getEffectiveLevel() >= logging.WARNING, logger.name
            assert not logger.isEnabledFor(logging.INFO), logger.name
    finally:
        for logger, level in zip(loggers, previous):
            logger.setLevel(level)
        root.setLevel(previous_root)


def test_safe_exception_helper_drops_message_and_source_code(caplog):
    try:
        raise RuntimeError(QUESTION + ANSWER + KEY)
    except RuntimeError as exc:
        log_exception(logging.getLogger("app.test"), exc)
    assert QUESTION not in caplog.text and ANSWER not in caplog.text and KEY not in caplog.text
    assert "RuntimeError" in caplog.text and "test_app_observability.py" in caplog.text


def test_rag_sources_saved_in_background_record(tmp_path):
    fake = FakeServices().knowledge()
    app, writer = make_app(tmp_path, services=fake)
    with TestClient(app) as client:
        response = ask(client)
    assert response.status_code == 200
    row = writer.write.call_args.args[0]
    assert row.route == "rag" and row.strategy == "direct"
    assert row.sources == response.json()["sources"]
    assert row.sources[0]["doc_id"] == "d1"


def test_disabled_also_skips_error_rows(tmp_path):
    fake = FakeServices()
    fake.route.side_effect = AppError(50201, 502, "模型服务暂时不可用")
    app, writer = make_app(tmp_path, services=fake, enabled=False)
    with TestClient(app) as client:
        assert ask(client).status_code == 502
    writer.write.assert_not_awaited()


def test_default_test_app_uses_tmp_logs_without_touching_repository(app_factory, isolated_log_dir):
    from pathlib import Path
    roots = (Path(__file__).resolve().parents[1] / "logs", Path(__file__).resolve().parents[2] / "logs")
    def snapshot():
        return {path: (path.stat().st_size, path.stat().st_mtime_ns)
                for root in roots for path in root.rglob("*") if path.is_file()}
    before = snapshot()
    assert Path(Settings(_env_file=None).log_dir) == isolated_log_dir
    with TestClient(app_factory()) as client:
        assert ask(client).status_code == 200
    assert (isolated_log_dir / "app.log").is_file()
    assert snapshot() == before
