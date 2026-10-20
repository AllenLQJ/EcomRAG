import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import FaqHit, RouteDecision
from app.faq.cache import FaqCache
from app.llm.generator import GeneratedAnswer
from app.products import COLUMNS, ProductCards, ProductRepo
from app.retrieval.embedder import FakeEmbedder
from app.retrieval.retriever import OUTPUT_FIELDS, Retriever
from faq_fakes import FakeRedis
from chat_fakes import document
from ingest.product_metadata import ProductSourceIndex
from test_chat_stream import events
from test_products_load import FakeConnection, FakeCursor
from test_online_retrieval import FakeMilvus, hit


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
                          product_hidden_skus=["item-0"])
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
                                        ("null", None), ("[]", []), ("[invalid", [])])
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


def test_optional_card_callback_failure_preserves_rag_answer(app_factory, caplog):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services.knowledge()
    services.product_cards = Mock(side_effect=TimeoutError("PRIVATE_OPTIONAL_ERROR"))
    with TestClient(app) as api:
        result = api.post("/api/v1/chat", json=dict(session_id="timeout", message="参数")).json()
    assert result["route"] == "rag" and "products" not in result
    assert "PRIVATE_OPTIONAL_ERROR" not in caplog.text


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
    repo.fetch.assert_called_once_with([expected])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("route", ["faq", "cache", "general", "sensitive", "low", "no_answer", "no_product", "db_error"])
def test_other_paths_omit_products_and_do_not_query(app_factory, tmp_path, stream, route):
    app = app_factory(speculative_retrieval=False, chat_log_enabled=False)
    services = app.state.services
    service, repo = cards(tmp_path, [product()])
    services.product_cards = Mock(wraps=service.for_sources)
    if route in {"faq", "cache"}:
        hit = FaqHit(faq_id=1, question="参数", answer="FAQ", score=1)
        if route == "cache":
            cache = FaqCache(client=FakeRedis())
            cache.set("参数", hit)
            hit = cache.get("参数")
        services.faq_lookup.return_value = hit
    elif route == "sensitive":
        services.route.return_value = RouteDecision(intent="sensitive", rewritten_query="参数")
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
    if route != "db_error":
        repo.fetch.assert_not_called()
    if route not in {"no_product", "db_error"}:
        services.product_cards.assert_not_called()
