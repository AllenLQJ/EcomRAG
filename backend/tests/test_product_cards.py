import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import products as product_cards_module
from app.config import Settings
from app.contracts import FaqHit, RetrievedDoc, RouteDecision
from app.faq.cache import FaqCache
from app.llm.generator import GeneratedAnswer
from app.products import COLUMNS, ProductCards, ProductNameIndex, ProductRepo
from app.schemas import ChatResponse
from app.retrieval.embedder import FakeEmbedder
from app.retrieval.retriever import OUTPUT_FIELDS, Retriever
from faq_fakes import FakeRedis
from chat_fakes import document
from ingest.product_metadata import ProductSourceIndex
from test_chat_stream import events
from test_products_load import FakeConnection, FakeCursor
from test_online_retrieval import FakeMilvus, hit


# 与真实 SDK 一样由 dependency_error 识别，完全离线。
MilvusError = type("MilvusException", (Exception,), {"__module__": "pymilvus.exceptions"})
# 模拟驱动异常，保持测试不依赖可选 SDK 的导入。
ProgrammingError = type("ProgrammingError", (Exception,), {"__module__": "pymysql.err"})
OperationalError = type("OperationalError", (Exception,), {"__module__": "pymysql.err"})
PRIVATE_ERROR = ("SELECT * FROM private_database.private_table WHERE sku='PRIVATE_INPUT'; "
                 "host=PRIVATE_HOST user=PRIVATE_USER password=PRIVATE_PASSWORD")


@pytest.fixture(autouse=True)
def isolated_card_warning_limit(monkeypatch):
    monkeypatch.setattr(product_cards_module, "_warning_times", {})


def card_warnings(caplog):
    return [record for record in caplog.records
            if record.getMessage().startswith("product_cards_unavailable")]


def assert_safe_card_warning(record, exception_type, mysql_errno, is_timeout, stage):
    expected = dict(exception_type=exception_type, mysql_errno=mysql_errno,
                    is_timeout=is_timeout, stage=stage)
    assert record.levelno == logging.WARNING
    for key, value in expected.items():
        assert getattr(record, key) == value
        assert f"{key}={value}" in record.getMessage()
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    # 原始异常不能藏在 args、extra 或 traceback 中供其他 handler 读取。
    captured = repr(record.__dict__)
    for secret in ("SELECT", "private_database", "private_table", "PRIVATE_INPUT", "PRIVATE_HOST",
                   "PRIVATE_USER", "PRIVATE_PASSWORD", "PRIVATE_OPTIONAL_ERROR"):
        assert secret not in captured


class SchemaMilvus(FakeMilvus):
    def __init__(self, results, *, with_skus=False):
        super().__init__(results)
        self.fields = OUTPUT_FIELDS + (["skus"] if with_skus else [])
        self.describe_collection = Mock(side_effect=lambda **kw: {
            "fields": [{"name": field} for field in self.fields]})

    def hybrid_search(self, **kwargs):
        invalid = set(kwargs["output_fields"]) - set(self.fields)
        if invalid:
            self.calls.append(kwargs)
            raise MilvusError(f"field {sorted(invalid)[0]} not exist in schema")
        return super().hybrid_search(**kwargs)


def metadata_retriever(client):
    return Retriever(client, FakeEmbedder(), rerank_enabled=False, skus_metadata=True,
                     request_factory=lambda **kw: kw, rrf_factory=lambda **kw: kw)


def product(sku="qiguang-l1", **changes):
    row = dict(sku_id=sku, name="栖光 L1 台灯", category="桌面台灯",
               specs={"库存": "99 件", "价格": "199 元", "额定功率": "6 瓦",
                      "色温档位": "三档", "亮度调节": "三档"}, image_main=f"{sku}/main.webp",
               price=199, stock=99)
    row.update(changes)
    return row


def doc(sku=None, *, source="doc.md", index=1):
    return document(index).model_copy(update={"skus": sku, "source": source})


def cards(tmp_path, rows, **options):
    repo = Mock(spec=ProductRepo)
    repo.fetch.return_value = rows
    service = ProductCards(Settings(_env_file=None, **options), repo=repo,
                           source_index=ProductSourceIndex(), images_root=tmp_path)
    return service, repo


def test_batch_query_order_dedup_skip_missing_hidden_and_cap(tmp_path):
    service, repo = cards(tmp_path, [product(f"item-{i}") for i in reversed(range(6))],
                          product_hidden_skus=["item-0"], product_cards_max=3)
    result = service.for_sources([doc(["missing", "item-0", "item-2", "item-1"]),
                                  doc(["item-2", "item-3", "item-4", "item-5"], index=2)])
    assert [card.sku for card in result] == ["item-2", "item-1", "item-3"]
    repo.fetch.assert_called_once_with(["missing", "item-2", "item-1", "item-3", "item-4", "item-5"])
    for card in result:
        assert set(card.model_dump()) == {"sku", "name", "category", "highlights", "image_url"}
        assert card.highlights == ["额定功率：6 瓦", "色温档位：三档"]
        assert "199" not in card.model_dump_json() and "99" not in card.model_dump_json()


def test_candidates_bounded_and_no_row_cache(tmp_path):
    service, repo = cards(tmp_path, [product("item-1")])
    source = doc([f"item-{i}" for i in range(120)])
    assert service.for_sources([source])
    assert len(repo.fetch.call_args.args[0]) == 100
    repo.fetch.return_value = []  # 删除/下架下一次立即反映。
    assert service.for_sources([source]) == []
    assert repo.fetch.call_count == 2


@pytest.mark.parametrize("base,expected", [("", "qiguang-l1/main.webp"),
                         ("https://images.example.test/products/", "https://images.example.test/products/qiguang-l1/main.webp")])
def test_image_url_and_placeholder_transition(tmp_path, base, expected):
    service, _ = cards(tmp_path, [product()], image_base_url=base)
    sources = [doc(["qiguang-l1"])]
    assert service.for_sources(sources)[0].image_url == expected
    folder = tmp_path / "qiguang-l1"
    folder.mkdir()
    (folder / "main.svg").write_text("placeholder")
    marker = folder / "main.placeholder"
    marker.touch()
    assert service.for_sources(sources)[0].image_url == expected.replace(".webp", ".svg")
    marker.unlink()
    assert service.for_sources(sources)[0].image_url == expected


@pytest.mark.parametrize("path", [None, "", "https://evil.test/image.webp", "//evil.test/image.webp",
                                  "../secret.png", "qiguang-l1/main.webp?price=199", "another/main.webp"])
def test_untrusted_image_path_is_null(tmp_path, path):
    service, _ = cards(tmp_path, [product(image_main=path)])
    assert service.for_sources([doc(["qiguang-l1"])])[0].image_url is None


@pytest.mark.parametrize("change", [dict(name="台灯 售价 199 元"), dict(category="未知分类"),
                                    dict(specs="broken JSON"), dict(specs=[]),
                                    dict(specs={"额定功率": "库存 12", "价格": "199 元"})])
def test_invalid_or_commercial_data_skips_card(tmp_path, change):
    service, _ = cards(tmp_path, [product(**change)])
    assert service.for_sources([doc(["qiguang-l1"])]) == []


def test_json_specs_one_safe_highlight_and_no_answer_text_inference(tmp_path):
    service, repo = cards(tmp_path, [product(specs=json.dumps({"额定功率": "6 瓦", "色温档位": "price 12"}))])
    assert service.for_sources([doc(["qiguang-l1"])])[0].highlights == ["额定功率：6 瓦"]
    assert service.for_sources([doc(source="product_catalog.md")]) == []
    assert service.for_sources([doc([], source="product_qiguang-l1.md")]) == []
    assert repo.fetch.call_count == 1


def test_manifest_metadata_priority_and_failure_degrades(tmp_path, caplog):
    (tmp_path / "manifest.csv").write_text("file,skus\nproduct_guide_lamps.md,qiguang-l1\n", encoding="utf-8")
    service, repo = cards(tmp_path, [product()])
    service.index = ProductSourceIndex([tmp_path])
    assert service.for_sources([doc(source="sub/product_guide_lamps.md")])[0].sku == "qiguang-l1"
    repo.fetch.reset_mock()
    service.for_sources([doc(["other-l2"], source="product_guide_lamps.md")])
    repo.fetch.assert_called_once_with(["other-l2"])
    repo.fetch.side_effect = OSError("PRIVATE_DB_PASSWORD")
    assert service.for_sources([doc(["qiguang-l1"])]) == []
    assert "product_cards_unavailable" in caplog.text
    assert "PRIVATE_DB_PASSWORD" not in caplog.text


@pytest.mark.parametrize("stage", ["connect", "query"])
@pytest.mark.parametrize("error,errno,is_timeout", [
    (ProgrammingError(1146, PRIVATE_ERROR), 1146, False),
    (ProgrammingError("1146", PRIVATE_ERROR), None, False),
    (ProgrammingError(True, PRIVATE_ERROR), None, False),
    (TimeoutError(PRIVATE_ERROR), None, True),
    (ValueError(1146, PRIVATE_ERROR), None, False),
])
def test_repo_failure_logs_safe_diagnostics(tmp_path, caplog, stage, error, errno, is_timeout):
    connection = FakeConnection()

    class Cursor(FakeCursor):
        def execute(self, sql, params=None):
            raise error

    connection.cursor = lambda: Cursor(connection)
    factory = Mock(side_effect=error) if stage == "connect" else Mock(return_value=connection)
    service = ProductCards(Settings(_env_file=None), repo=ProductRepo(connection_factory=factory),
                           images_root=tmp_path)
    assert service.for_sources([doc(["qiguang-l1"])]) == []
    assert connection.closed == (stage == "query")
    assert len(card_warnings(caplog)) == 1
    assert_safe_card_warning(card_warnings(caplog)[0], type(error).__name__, errno, is_timeout, stage)
    assert product_cards_module._card_stage.get() is None


@pytest.mark.parametrize("stage", ["source_lookup", "build_card"])
def test_non_database_failure_stage(tmp_path, caplog, stage):
    service, repo = cards(tmp_path, [product()])
    if stage == "source_lookup":
        service._index = Mock(side_effect=ValueError(PRIVATE_ERROR))
        sources = [doc()]
    else:
        service._card = Mock(side_effect=ValueError(PRIVATE_ERROR))
        sources = [doc(["qiguang-l1"])]
    assert service.for_sources(sources) == []
    assert len(card_warnings(caplog)) == 1
    assert_safe_card_warning(card_warnings(caplog)[0], "ValueError", None, False, stage)
    if stage == "source_lookup":
        repo.fetch.assert_not_called()
    assert product_cards_module._card_stage.get() is None


def test_mysql_wrapped_socket_timeout(tmp_path, caplog):
    service, repo = cards(tmp_path, [])

    def fetch(skus):
        try:
            raise TimeoutError(PRIVATE_ERROR)
        except TimeoutError:
            raise OperationalError(2013, PRIVATE_ERROR) from None

    repo.fetch.side_effect = fetch
    assert service.for_sources([doc(["qiguang-l1"])]) == []
    assert_safe_card_warning(card_warnings(caplog)[0], "OperationalError", 2013, True, "query")


def test_card_warning_rate_limit_shared_between_instances(tmp_path, monkeypatch, caplog):
    now = [100.0]
    monkeypatch.setattr(product_cards_module, "monotonic", lambda: now[0])
    first, repo1 = cards(tmp_path, [])
    second, repo2 = cards(tmp_path, [])
    repo1.fetch.side_effect = ProgrammingError(1146, PRIVATE_ERROR)
    repo2.fetch.side_effect = ProgrammingError(1146, "a different private message")
    sources = [doc(["qiguang-l1"])]
    assert first.for_sources(sources) == second.for_sources(sources) == []
    now[0] = 159.999
    assert second.for_sources(sources) == []
    assert len(card_warnings(caplog)) == 1
    now[0] = 160.0
    assert first.for_sources(sources) == second.for_sources(sources) == []
    assert len(card_warnings(caplog)) == 2
    # 限频只影响日志，每次仍尝试查询，故障恢复后可立即返回卡片。
    repo1.fetch.side_effect = None
    repo1.fetch.return_value = [product()]
    assert first.for_sources(sources)[0].sku == "qiguang-l1"


def test_card_warning_rate_limit_keeps_error_categories_separate(monkeypatch, caplog):
    monkeypatch.setattr(product_cards_module, "monotonic", lambda: 100.0)
    errors = [(ProgrammingError(1146, PRIVATE_ERROR), "query"),
              (ProgrammingError(1045, PRIVATE_ERROR), "query"),
              (ValueError(PRIVATE_ERROR), "query"),
              (ProgrammingError(1146, PRIVATE_ERROR), "connect")]
    wrapped = ProgrammingError(1146, PRIVATE_ERROR)
    wrapped.__cause__ = TimeoutError(PRIVATE_ERROR)
    errors.append((wrapped, "query"))
    for error, stage in errors * 2:
        product_cards_module.log_product_cards_unavailable(error, stage)
    assert len(card_warnings(caplog)) == len(errors)


def test_card_warning_rate_limit_is_thread_safe(monkeypatch, caplog):
    monkeypatch.setattr(product_cards_module, "monotonic", lambda: 100.0)

    def warn(_):
        product_cards_module.log_product_cards_unavailable(ProgrammingError(1146, PRIVATE_ERROR), "query")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(warn, range(32)))
    assert len(card_warnings(caplog)) == 1


@pytest.mark.parametrize("mapping", [False, True])
def test_repo_selects_only_public_columns_and_closes_owned_connection(mapping):
    connection = FakeConnection()

    class Cursor(FakeCursor):
        def fetchall(self):
            row = product()
            return [row if mapping else tuple(row[key] for key in COLUMNS)]

    connection.cursor = lambda: Cursor(connection)
    repo = ProductRepo(connection_factory=lambda: connection)
    rows = repo.fetch(["qiguang-l1", "bad' OR 1=1"])
    sql, params = connection.calls[0]
    assert sql == "SELECT sku_id, name, category, specs, image_main FROM products WHERE sku_id IN (%s, %s)"
    assert params == ("qiguang-l1", "bad' OR 1=1")
    assert rows[0]["sku_id"] == "qiguang-l1"
    assert connection.closed and not connection.commits


@pytest.mark.parametrize("url", ["file:///images", "//images.test", "https://user:pass@images.test", "https://images.test?a=1"])
def test_image_base_url_validation(url):
    with pytest.raises(ValueError, match="IMAGE_BASE_URL"):
        Settings(_env_file=None, image_base_url=url)


@pytest.mark.parametrize("raw,expected", [('["a-1","b-2"]', ["a-1", "b-2"]),
                                        ("null", None), ("[]", []), ("[invalid", None),
                                        ("  [invalid", None), ("  []  ", []), (" null ", None)])
def test_retriever_optional_metadata_roundtrip(raw, expected):
    result = hit("p1", .8)
    result["entity"]["skus"] = raw
    client = FakeMilvus([[result]])
    retriever = Retriever(client, FakeEmbedder(), rerank_enabled=False, skus_metadata=True,
                          request_factory=lambda **kw: kw, rrf_factory=lambda **kw: kw)
    retrieved = retriever.retrieve(["参数"], "参数")[0]
    assert retrieved.skus == expected
    assert client.calls[0]["output_fields"] == OUTPUT_FIELDS + ["skus"]
    assert "skus" not in retrieved.model_dump()


@pytest.mark.parametrize("raw,expected", [("[invalid", ["qiguang-l1"]),
                                        ("null", ["qiguang-l1"]), ("[]", [])])
def test_corrupt_metadata_allows_filename_fallback(tmp_path, raw, expected):
    service, repo = cards(tmp_path, [product()])
    source = RetrievedDoc.model_validate({**doc(source="product_qiguang-l1.md").model_dump(), "skus": raw})
    assert [card.sku for card in service.for_sources([source])] == expected
    assert repo.fetch.call_count == bool(expected)


def test_missing_metadata_field_degrades_once_and_survives_alias_rollback(caplog):
    result = hit("p1", .8)
    result["entity"]["skus"] = '["qiguang-l1"]'
    client = SchemaMilvus([[result], [hit("p2", .7)], [hit("p3", .6)]], with_skus=True)
    retriever = metadata_retriever(client)
    assert retriever.retrieve(["参数"], "参数")[0].skus == ["qiguang-l1"]
    # 同一个 alias 随时可切回无字段的旧集合，不在进程中固定实体版本。
    client.fields = OUTPUT_FIELDS
    assert retriever.retrieve(["参数"], "参数")[0].skus is None
    assert retriever.retrieve(["参数"], "参数")[0].skus is None
    assert [call["output_fields"] for call in client.calls] == [
        OUTPUT_FIELDS + ["skus"], OUTPUT_FIELDS + ["skus"], OUTPUT_FIELDS, OUTPUT_FIELDS]
    client.describe_collection.assert_called_once_with(collection_name="kb_chunks", timeout=10)
    assert caplog.text.count("product_skus_metadata_unavailable") == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("source", ["product_qiguang-l1.md", "product_guide_lamps.md"])
def test_old_collection_metadata_enabled_keeps_rag_and_fallback_cards(app_factory, tmp_path, caplog, stream, source):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False, product_skus_metadata=True)
    services = app.state.services.knowledge()
    result = hit("p1", .8)
    result["entity"]["source"] = source
    client = SchemaMilvus([[result], [result]])
    retriever = metadata_retriever(client)
    services.retrieve = Mock(wraps=retriever.retrieve)
    service, repo = cards(tmp_path, [product()])
    if source == "product_guide_lamps.md":
        manifest = tmp_path / "manifest.csv"
        manifest.write_text("file,skus\nproduct_guide_lamps.md,qiguang-l1\n", encoding="utf-8")
        service.index = ProductSourceIndex([manifest])
    services.product_cards = Mock(wraps=service.for_sources)
    with TestClient(app) as api:
        for i in range(2):
            response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                                json=dict(session_id=f"old-schema-{i}", message="台灯参数"))
            assert response.status_code == 200
            body = events(response.text)[-1][1] if stream else response.json()
            assert body["route"] == "rag"
            assert [card["sku"] for card in body["products"]] == ["qiguang-l1"]
    assert repo.fetch.call_count == 2
    assert len(client.calls) == 3  # 首次失败后重试一次，后续不再请求 skus。
    assert caplog.text.count("product_skus_metadata_unavailable") == 1


@pytest.mark.parametrize("failure", ["permission denied", "deadline exceeded", "connection refused",
                                   "field parent_text not exist in schema"])
def test_real_milvus_errors_still_return_dependency_503(app_factory, caplog, failure):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    client = SchemaMilvus([])
    client.hybrid_search = Mock(side_effect=MilvusError(failure))
    services.retrieve = Mock(wraps=metadata_retriever(client).retrieve)
    with TestClient(app) as api:
        response = api.post("/api/v1/chat", json=dict(session_id="dependency", message="参数"))
    assert response.status_code == 503
    client.describe_collection.assert_not_called()
    client.hybrid_search.assert_called_once()
    assert "product_skus_metadata_unavailable" not in caplog.text


@pytest.mark.parametrize("schema_failure", [False, True])
def test_schema_must_confirm_missing_field_before_downgrade(app_factory, caplog, schema_failure):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    client = SchemaMilvus([], with_skus=True)
    client.hybrid_search = Mock(side_effect=MilvusError("field skus not found"))
    if schema_failure:
        client.describe_collection.side_effect = MilvusError("permission denied")
    retriever = metadata_retriever(client)
    services.retrieve = Mock(wraps=retriever.retrieve)
    with TestClient(app) as api:
        response = api.post("/api/v1/chat", json=dict(session_id="schema-check", message="参数"))
    assert response.status_code == 503
    assert retriever.skus_metadata
    client.hybrid_search.assert_called_once()
    assert "product_skus_metadata_unavailable" not in caplog.text


def test_hidden_skus_use_shared_parser_and_warn_without_raw_values(monkeypatch, tmp_path, caplog):
    values = ["qiguang-l1", "qiguang-l1", "QIGUANG-L1", " qiguang-l1 ", "bad--sku", "x" * 65,
              "../PRIVATE_SKU", None, 123]
    monkeypatch.setenv("PRODUCT_HIDDEN_SKUS", json.dumps(values))
    settings = Settings(_env_file=None)
    assert settings.product_hidden_skus == ["qiguang-l1"]
    assert caplog.text.count("PRODUCT_HIDDEN_SKUS 含非法 SKU") == 1
    assert "PRIVATE_SKU" not in caplog.text
    service, repo = cards(tmp_path, [product()], product_hidden_skus=values)
    assert service.for_sources([doc(["qiguang-l1"])]) == []
    repo.fetch.assert_not_called()


def test_default_manifests_conflict_warns_and_stops_filename_fallback(tmp_path, monkeypatch, caplog):
    settings = Settings(_env_file=None)
    assert settings.product_manifest_paths == [
        "data/docs_new", "data/docs_staging/manifest.csv", "data/docs/manifest.csv"]
    for folder, sku in [("docs_new/drafts", "qiguang-l1"), ("docs_staging", "PRIVATE_SKU"),
                        ("docs", "qiguang-l2")]:
        path = tmp_path / "data" / folder
        path.mkdir(parents=True)
        (path / "manifest.csv").write_text(f"file,skus\nproduct_qiguang-l1.md,{sku}\n", encoding="utf-8")
    service, repo = cards(tmp_path, [product()])
    service.index = None
    monkeypatch.setattr("app.products.resolve_data_path", lambda path: tmp_path / path)
    for source in ["product_qiguang-l1.md", "flattened/product_qiguang-l1.md"]:
        assert service.for_sources([doc(source=source)]) == []
    repo.fetch.assert_not_called()
    assert caplog.text.count("product_manifest_ambiguous") == 1
    assert "product_qiguang-l1.md" in caplog.text
    assert str(tmp_path) not in caplog.text and "PRIVATE_SKU" not in caplog.text


@pytest.mark.parametrize("text", ["price199", "199USD", "USD199", "RMB199", "CNY199", "￥199", "199 元"])
def test_commercial_filter_remains_conservative_for_concatenated_prices(tmp_path, text):
    service, _ = cards(tmp_path, [product(name="台灯 " + text)])
    assert service.for_sources([doc(["qiguang-l1"])]) == []
    service, _ = cards(tmp_path, [product(specs={"额定功率": text})])
    assert service.for_sources([doc(["qiguang-l1"])]) == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error,errno,is_timeout", [
    (TimeoutError("PRIVATE_OPTIONAL_ERROR"), None, True),
    (ProgrammingError(1146, PRIVATE_ERROR), 1146, False),
    (ValueError(PRIVATE_ERROR), None, False),
])
def test_optional_card_callback_failure_preserves_rag_answer(app_factory, caplog, stream, error, errno, is_timeout):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    services.product_cards = Mock(side_effect=error)
    with TestClient(app) as api:
        for i in range(2):
            response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                                json=dict(session_id=f"callback-{i}", message="参数"))
            assert response.status_code == 200
            result = events(response.text)[-1][1] if stream else response.json()
            assert result["route"] == "rag" and "products" not in result
            assert result["answer"] == "说明[1]"
    assert len(card_warnings(caplog)) == 1
    assert_safe_card_warning(card_warnings(caplog)[0], type(error).__name__, errno, is_timeout, "product_cards")


@pytest.mark.parametrize("stream", [False, True])
def test_optional_card_deadline_preserves_answer(app_factory, caplog, stream):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False, product_cards_timeout_s=0.01)
    services = app.state.services.knowledge()

    async def slow_cards(docs, answer):
        await asyncio.Event().wait()

    services.product_cards = slow_cards
    with TestClient(app) as api:
        response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                            json=dict(session_id="card-deadline", message="参数"))
    assert response.status_code == 200
    result = events(response.text)[-1][1] if stream else response.json()
    assert result["route"] == "rag" and result["answer"] == "说明[1]" and "products" not in result
    assert len(card_warnings(caplog)) == 1
    assert_safe_card_warning(card_warnings(caplog)[0], "TimeoutError", None, True, "product_cards")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("answer,expected", [("回答[2]", "item-2"), ("回答", "item-1")])
def test_http_and_sse_use_final_citations(app_factory, tmp_path, stream, answer, expected):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    services.retrieve.return_value = [doc(["item-1"]), doc(["item-2"], index=2)]
    services.generate_answer.return_value = GeneratedAnswer(answer, {1})  # 文本引用优先于元数据。
    service, repo = cards(tmp_path, [product("item-1"), product("item-2")])
    services.product_cards = Mock(wraps=service.for_sources)
    with TestClient(app) as api:
        response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                            json=dict(session_id="cards", message="台灯参数"))
    assert response.status_code == 200
    result = events(response.text)[-1][1] if stream else response.json()
    assert result["route"] == "rag"
    assert [card["sku"] for card in result["products"]] == [expected]
    repo.fetch.assert_called_once_with(None)  # 冷名称索引与来源行在同一次查询读取。
    assert services.product_cards.call_args.args[1] == answer


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("route", ["faq", "cache", "general", "sensitive", "low", "no_answer", "no_product", "db_error"])
def test_other_paths_omit_products_and_do_not_query(app_factory, tmp_path, stream, route):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services
    service, repo = cards(tmp_path, [product()])
    services.product_cards = Mock(wraps=service.for_sources)
    if route in {"faq", "cache"}:
        hit = FaqHit(faq_id=1, question="参数", answer="灵码衡电 P1 充电宝", score=1)
        if route == "cache":
            cache = FaqCache(client=FakeRedis())
            cache.set("参数", hit)
            hit = cache.get("参数")
        services.faq_lookup.return_value = hit
    elif route == "sensitive":
        services.route.return_value = RouteDecision(intent="sensitive", rewritten_query="参数")
    elif route == "general":
        services.chat_general.return_value = "灵码衡电 P1 充电宝"
    elif route != "general":
        services.knowledge()
        services.retrieve.return_value = [doc(["qiguang-l1"])]
        if route == "low":
            services.retrieve.return_value = []
        elif route == "no_answer":
            services.generate_answer.return_value = GeneratedAnswer("NO_ANSWER", set())
        elif route == "no_product":
            services.retrieve.return_value = [doc()]
        else:
            repo.fetch.side_effect = ConnectionError("DB down")
    with TestClient(app) as api:
        response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                            json=dict(session_id="omit", message="参数"))
    assert response.status_code == 200
    result = events(response.text)[-1][1] if stream else response.json()
    assert "products" not in result
    if route not in {"db_error", "no_product"}:
        repo.fetch.assert_not_called()
    elif route == "no_product":
        repo.fetch.assert_called_once_with(None)
    if route not in {"no_product", "db_error"}:
        services.product_cards.assert_not_called()


def charging_products():
    return [product(f"hengdian-{model.lower()}", name=f"灵码衡电 {model} {kind}",
                    category="充电设备", specs={"设备类型": kind, "输出规格": "通用 C 型接口"})
            for model, kind in [("P1", "充电宝"), ("P2", "充电宝"), ("P3", "充电宝"),
                                ("A1", "充电器"), ("A2", "充电器"), ("A3", "充电器")]]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("omit_brand", [False, True])
def test_catalog_answer_six_cards_in_answer_order(app_factory, tmp_path, stream, omit_brand):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    rows = charging_products()
    # 来源是两块无 SKU 的总览；数据库行顺序与正文顺序无关。
    services.retrieve.return_value = [doc(source="data/docs/product_catalog.md", index=i) for i in (1, 2)]
    names = [row["name"].removeprefix("灵码") if omit_brand else row["name"] for row in rows]
    answer = "、".join(names) + "。[1][2]"
    services.generate_answer.return_value = GeneratedAnswer(answer, {1, 2})
    service, repo = cards(tmp_path, list(reversed(rows)))
    services.product_cards = Mock(wraps=service.for_sources)
    with TestClient(app) as api:
        response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                            json=dict(session_id="catalog", message="介绍一下灵码的充电设备有哪些款"))
    assert response.status_code == 200
    result = events(response.text)[-1][1] if stream else response.json()
    assert result["answer"] == answer
    assert [card["sku"] for card in result["products"]] == [row["sku_id"] for row in rows]
    assert len(result["sources"]) == 2
    repo.fetch.assert_called_once_with(None)
    for card in result["products"]:
        assert set(card) == {"sku", "name", "category", "highlights", "image_url"}
        assert not any(word in json.dumps(card) for word in ("price", "stock", "199", "99"))


def test_names_longest_boundary_category_and_ambiguity():
    rows = [product("short", name="听岚 S1"), product("ten", name="听岚 S10"),
            product("long", name="听岚 S1 无线耳机"),
            product("charger", name="灵码衡电 P1 充电器"),
            product("powerbank", name="灵码衡电 P1 充电宝"),
            product("duplicate-a", name="灵码同名 X1 耳机"),
            product("duplicate-b", name="灵码同名 X1 耳机"),
            product("brand-a", name="灵码共享 X1 耳机"),
            product("brand-b", name="共享 X1 耳机")]
    index = ProductNameIndex(rows)
    assert index.match("听岚 S10；听岚 S1 无线耳机；衡电 P1 充电器；衡电 P1 充电宝") == [
        "ten", "long", "charger", "powerbank"]
    assert index.match("听岚 S100；听岚 XS1；衡电 P1；同名 X1 耳机；共享 X1 耳机") == []
    assert index.match("灵码共享 X1 耳机；听岚 S1，听岚 S1") == ["brand-a", "short"]
    assert index.match("灵码衡电\nP1  充电宝") == ["powerbank"]
    # 子串出现在较长名称内部时，也只保留长名称。
    assert ProductNameIndex([product("a", name="耳机"), product("b", name="听岚 S1 耳机")]).match(
        "听岚 S1 耳机") == ["b"]
    # 旧表名称自身不含品牌时，带品牌的答案仍可包含该完整商品名。
    assert ProductNameIndex([product("bank", name="衡电 P1 充电宝")]).match(
        "灵码衡电 P1 充电宝") == ["bank"]


def test_sources_first_dedup_and_skip_invalid_hidden_missing(tmp_path):
    rows = charging_products()
    rows[1]["specs"] = {"输出规格": "售价 199 元"}
    service, repo = cards(tmp_path, rows, product_cards_max=3, product_hidden_skus=[rows[2]["sku_id"]])
    answer = "、".join(row["name"] for row in rows)
    result = service.for_sources([doc(["missing", rows[-1]["sku_id"], rows[2]["sku_id"]])], answer)
    assert [card.sku for card in result] == [rows[-1]["sku_id"], rows[0]["sku_id"], rows[3]["sku_id"]]
    repo.fetch.assert_called_once_with(None)
    repo.fetch.reset_mock()
    assert [card.sku for card in service.for_sources([doc([rows[-1]["sku_id"]])], answer)] == [
        rows[-1]["sku_id"], rows[0]["sku_id"], rows[3]["sku_id"]]
    assert repo.fetch.call_count == 1
    assert rows[2]["sku_id"] not in repo.fetch.call_args.args[0]


def test_name_cache_ttl_only_caches_names_and_refreshes_rows(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(product_cards_module, "monotonic", lambda: now[0])
    rows = charging_products()
    service, repo = cards(tmp_path, rows)
    answer = rows[0]["name"]
    assert service.for_sources([], answer)[0].highlights[0] == "设备类型：充电宝"
    repo.fetch.assert_called_once_with(None)
    repo.fetch.reset_mock()
    # 同名的新鲜规格；不复用旧行。
    repo.fetch.return_value = [{**rows[0], "specs": {"设备类型": "便携充电宝"}}]
    now[0] = 159.999
    assert service.for_sources([], answer)[0].highlights == ["设备类型：便携充电宝"]
    repo.fetch.assert_called_once_with([rows[0]["sku_id"]])
    repo.fetch.return_value = []
    assert service.for_sources([], answer) == []  # 已删除，不等待 TTL。
    repo.fetch.return_value = [{**rows[0], "name": "灵码新名称 P1 充电宝"}]
    assert service.for_sources([], answer) == []  # 改名后不能沿用旧名称补卡。
    now[0] = 160.0
    assert service.for_sources([], "新名称 P1 充电宝")[0].sku == rows[0]["sku_id"]
    assert repo.fetch.call_args.args == (None,)
    assert repo.fetch.call_count == 4  # 每请求恰好一次。
    repo.fetch.reset_mock()
    assert service.for_sources([], "没有任何完整商品名") == []
    repo.fetch.assert_called_once_with([])  # ProductRepo 对空列表不建连。


@pytest.mark.parametrize("maximum", [1, 3, 6, 12])
def test_product_cards_max_applies_to_sources_and_mentions(tmp_path, maximum):
    rows = [product(f"lamp-{i}", name=f"灵码栖光 L{i} 台灯") for i in range(14)]
    service, _ = cards(tmp_path, rows, product_cards_max=maximum)
    assert len(service.for_sources([doc([row["sku_id"] for row in rows])])) == maximum
    assert len(service.for_sources([], "、".join(row["name"] for row in rows))) == maximum


def test_max_setting_environment_default_bounds_and_response_contract(monkeypatch):
    assert Settings(_env_file=None).product_cards_max == 6
    monkeypatch.setenv("PRODUCT_CARDS_MAX", "12")
    assert Settings(_env_file=None).product_cards_max == 12
    for invalid in (0, 13, -1, 1.5):
        with pytest.raises(ValueError):
            Settings(_env_file=None, product_cards_max=invalid)
    card = dict(sku="lamp-1", name="台灯", category="桌面台灯", highlights=["额定功率：6 瓦"])
    base = dict(request_id="r", session_id="s", answer="说明", route="rag")
    assert len(ChatResponse(**base, products=[card] * 12).products) == 12
    assert ChatResponse.model_json_schema()["properties"]["products"]["anyOf"][0]["maxItems"] == 12
    with pytest.raises(ValueError):
        ChatResponse(**base, products=[card] * 13)


@pytest.mark.parametrize("stream", [False, True])
def test_only_answer_names_not_user_question_create_cards(app_factory, tmp_path, stream):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    services.retrieve.return_value = [doc(source="product_catalog.md")]
    service, repo = cards(tmp_path, charging_products())
    services.product_cards = Mock(wraps=service.for_sources)
    with TestClient(app) as api:
        response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                            json=dict(session_id="answer-only", message="介绍灵码衡电 P1 充电宝"))
    result = events(response.text)[-1][1] if stream else response.json()
    assert result["answer"] == "说明[1]" and "products" not in result
    repo.fetch.assert_called_once_with(None)


@pytest.mark.parametrize("error", [ProgrammingError(1146, PRIVATE_ERROR), TimeoutError(PRIVATE_ERROR)])
@pytest.mark.parametrize("stream", [False, True])
def test_catalog_query_failure_preserves_answer(app_factory, tmp_path, caplog, error, stream):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    services.retrieve.return_value = [doc(source="product_catalog.md")]
    answer = charging_products()[0]["name"] + "。[1]"
    services.generate_answer.return_value = GeneratedAnswer(answer, {1})
    service, repo = cards(tmp_path, [])
    repo.fetch.side_effect = error
    services.product_cards = Mock(wraps=service.for_sources)
    with TestClient(app) as api:
        response = api.post("/api/v1/chat" + ("/stream" if stream else ""),
                            json=dict(session_id="catalog-failure", message="充电设备有哪些款"))
    assert response.status_code == 200
    result = events(response.text)[-1][1] if stream else response.json()
    assert result["answer"] == answer and "products" not in result
    assert len(card_warnings(caplog)) == 1
    assert "PRIVATE" not in caplog.text


def test_full_table_refresh_only_reads_public_columns():
    connection = FakeConnection()

    class Cursor(FakeCursor):
        def fetchall(self):
            return [tuple(product()[key] for key in COLUMNS)]

    connection.cursor = lambda: Cursor(connection)
    repo = ProductRepo(connection_factory=lambda: connection)
    assert repo.fetch(None)[0]["sku_id"] == "qiguang-l1"
    assert connection.calls == [("SELECT sku_id, name, category, specs, image_main FROM products", ())]
    assert connection.closed


def test_concurrent_name_refresh_and_queries_stay_one_per_request(tmp_path):
    rows = charging_products()
    service, repo = cards(tmp_path, rows)
    answer = "、".join(row["name"] for row in rows)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.for_sources([], answer), range(16)))
    assert all([card.sku for card in result] == [row["sku_id"] for row in rows] for result in results)
    assert repo.fetch.call_count == 16
    assert sum(call.args == (None,) for call in repo.fetch.call_args_list) == 1
