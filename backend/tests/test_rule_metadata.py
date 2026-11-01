"""规则元数据（租户/类目/生效期/版本）：建集合、写入、过滤表达式、加权排序、内部接口。全部离线。"""
import dataclasses
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config
from app.internal_api import KbWriter, UpsertBody
from app.retrieval.embedder import FakeEmbedder
from app.retrieval.retriever import Retriever, RULE_OUTPUT_FIELDS, rule_expr, rule_scope
from app.retrieval.store import MilvusStore, NEVER_EXPIRES, RULE_DEFAULTS
from ingest.loaders import Document
from ingest.splitter import chunk_record, split_document
from test_ingest_retrieval import FakeDataType, FakeMilvusClient, Recorder


class RuleDataType(FakeDataType):
    BOOL, INT64, INT32 = "BOOL", "INT64", "INT32"


class SchemaRecorder(Recorder):
    pass


class RuleClient(FakeMilvusClient):
    def __init__(self, existing_fields=None):
        super().__init__()
        self.existing_fields = existing_fields
        self.schema = SchemaRecorder()
        if existing_fields is not None:
            self.created = {"collection_name": "kb_chunks"}

    def create_schema(self, **kwargs):
        return self.schema

    def describe_collection(self, collection_name, timeout=None):
        return {"fields": [{"name": n} for n in self.existing_fields]}


def chunk(rule=None):
    c = split_document(Document("a", "标题", "内容。", "a.md", "after_sales"))[0]
    return dataclasses.replace(c, rule=rule)


def vectors(n=1):
    return FakeEmbedder().encode(["x"] * n)


# ---------------- store
def test_new_collection_gets_rule_fields_only_when_enabled():
    plain, ruled = RuleClient(), RuleClient()
    MilvusStore(client=plain, data_type=RuleDataType).ensure_collection()
    MilvusStore(client=ruled, data_type=RuleDataType, rule_metadata=True).ensure_collection()
    names = lambda c: [i["field_name"] for i in c.schema.items if "field_name" in i and "datatype" in i]
    assert "tenant_id" not in names(plain)
    assert {"tenant_id", "catalog_category_id", "rule_source", "is_demo_rule", "effective_from", "effective_to", "version"} <= set(names(ruled))


def test_existing_collection_must_match_flag_in_both_directions():
    base = ["chunk_id", "child_text", "parent_id", "parent_text", "doc_id", "title", "source", "category", "dense", "sparse"]
    with pytest.raises(ValueError, match="缺少规则元数据"):
        MilvusStore(client=RuleClient(base), data_type=RuleDataType, rule_metadata=True).ensure_collection()
    with pytest.raises(ValueError, match="已有集合含规则元数据"):
        MilvusStore(client=RuleClient(base + ["tenant_id"]), data_type=RuleDataType).ensure_collection()
    MilvusStore(client=RuleClient(base), data_type=RuleDataType).ensure_collection()


def test_upsert_fills_public_defaults_and_keeps_explicit_rule():
    client = RuleClient()
    store = MilvusStore(client=client, data_type=RuleDataType, rule_metadata=True)
    explicit = {"tenant_id": "T1", "catalog_category_id": "C1", "rule_source": "沙盒规则", "is_demo_rule": True,
                "effective_from": 100, "effective_to": 200, "version": 3}
    store.upsert([chunk(None)], vectors())
    store.upsert([dataclasses.replace(chunk(None), chunk_id="b#0#0", rule=explicit)], vectors())
    public, private = client.rows["a#0#0"], client.rows["b#0#0"]
    assert {k: public[k] for k in RULE_DEFAULTS} == RULE_DEFAULTS and public["effective_to"] == NEVER_EXPIRES
    assert {k: private[k] for k in RULE_DEFAULTS} == explicit


def test_rule_metadata_is_refused_instead_of_silently_dropped_when_disabled():
    store = MilvusStore(client=RuleClient(), data_type=RuleDataType)
    with pytest.raises(ValueError, match="规则元数据写入需显式启用"):
        store.upsert([chunk({"tenant_id": "T1"})], vectors())


@pytest.mark.parametrize("rule,needle", [({"effective_from": 5, "effective_to": 1}, "不能晚于"),
                                          ({"tenant_id": "界" * 40}, "tenant_id")])
def test_invalid_rule_values_rejected(rule, needle):
    store = MilvusStore(client=RuleClient(), data_type=RuleDataType, rule_metadata=True)
    with pytest.raises(ValueError, match=needle):
        store.upsert([chunk(rule)], vectors())


def test_default_chunk_record_is_unchanged_and_rule_is_flattened():
    assert "rule" not in chunk_record(chunk(None))
    assert chunk_record(chunk({"tenant_id": "T1", "version": 2}))["tenant_id"] == "T1"


# ---------------- retriever
class SearchClient:
    def __init__(self):
        self.calls = []

    def hybrid_search(self, **kwargs):
        self.calls.append(kwargs)
        return [[{"distance": 0.5, "entity": {
            "parent_id": "p1", "doc_id": "d1", "title": "t", "source": "s", "parent_text": "P", "child_text": "C",
            "tenant_id": "T1", "catalog_category_id": "", "rule_source": "沙盒规则", "is_demo_rule": True,
            "effective_from": 0, "effective_to": NEVER_EXPIRES, "version": 4}}]]


def retriever(**kw):
    client = SearchClient()
    seen = {"requests": [], "ranker": None}

    def request_factory(**params):
        seen["requests"].append(params)
        return params

    def rrf(k):
        seen["ranker"] = ("rrf", k)
        return "RRF"

    def weighted(*weights):
        seen["ranker"] = ("weighted", weights)
        return "WEIGHTED"
    r = Retriever(client, FakeEmbedder(), None, rerank_enabled=False, request_factory=request_factory,
                  rrf_factory=rrf, weighted_factory=weighted, **kw)
    return r, client, seen


def test_filter_expression_scopes_to_public_plus_tenant_and_validity_window():
    r, client, seen = retriever(rule_metadata=True)
    with rule_scope("T1", "HOME_APPLIANCE_HEATER", now=1000):
        docs = r.retrieve_candidates(["退款"])
    expected = ('tenant_id in ["", "T1"] and effective_from <= 1000 and effective_to >= 1000 '
                'and catalog_category_id in ["", "HOME_APPLIANCE_HEATER"]')
    assert [req["expr"] for req in seen["requests"]] == [expected, expected]     # 稠密、稀疏两路都带过滤
    assert set(RULE_OUTPUT_FIELDS) <= set(client.calls[0]["output_fields"])
    assert docs[0].rule["tenant_id"] == "T1" and docs[0].rule["version"] == 4


def test_no_tenant_means_public_rules_only_and_no_category_means_no_category_clause():
    assert rule_expr({"tenant_id": None, "catalog_category_id": None, "now": 7}) == \
        'tenant_id in [""] and effective_from <= 7 and effective_to >= 7'


@pytest.mark.parametrize("bad", ['T1" or 1==1 or "', "T 1", "", "x" * 65, 'a"b', "T1\n"])
def test_scope_ids_cannot_inject_into_the_expression(bad):
    with pytest.raises(ValueError):
        with rule_scope(bad):
            pass


def test_disabled_flag_sends_no_filter_and_no_rule_fields():
    r, client, seen = retriever(rule_metadata=False)
    with rule_scope("T1"):
        r.retrieve_candidates(["退款"])
    assert all("expr" not in req for req in seen["requests"])
    assert not set(RULE_OUTPUT_FIELDS) & set(client.calls[0]["output_fields"])


def test_ranker_switch_rrf_default_weighted_07_03():
    r, client, seen = retriever()
    r.retrieve_candidates(["q"])
    assert seen["ranker"] == ("rrf", 60) and client.calls[0]["ranker"] == "RRF"
    r, client, seen = retriever(ranker="weighted", ranker_weights=(0.7, 0.3))
    r.retrieve_candidates(["q"])
    assert seen["ranker"] == ("weighted", (0.7, 0.3)) and client.calls[0]["ranker"] == "WEIGHTED"
    with pytest.raises(ValueError):
        Retriever(None, FakeEmbedder(), None, ranker="magic")


# ---------------- internal api with the flag on
def test_internal_search_reports_filtered_and_passes_scope_and_returns_rule_fields(monkeypatch):
    from chat_fakes import FakeServices, document
    from app import main
    seen = {}
    services = FakeServices()
    d = document(1, 0.9).model_copy(update={"rule": {"rule_source": "沙盒热水器规则", "is_demo_rule": True, "version": 4}})

    def retrieve(queries, rerank_query, top_k):
        from app.retrieval.retriever import RULE_SCOPE
        seen["scope"] = RULE_SCOPE.get()
        return [d]
    services.retrieve = retrieve
    services.embedder = SimpleNamespace(encode=lambda texts: SimpleNamespace(dense=[[1.0, 0.0]] + [[0.7, 0.71]] * (len(texts) - 1), sparse=[]))
    settings = config.Settings(_env_file=None, debug=False, llm_api_key="", internal_token="tok", kb_rule_metadata=True)
    app = main.create_app(settings, services=services)
    app.state.rate_limiter.cache._client = lambda: None
    with TestClient(app) as client:
        r = client.post("/api/v1/rag/search", headers={"X-Internal-Token": "tok"},
                        json={"query": "退款", "tenant_id": "T1", "catalog_category_id": "C1"})
        bad = client.post("/api/v1/rag/search", headers={"X-Internal-Token": "tok"},
                          json={"query": "退款", "tenant_id": 'T1" or 1==1 or "'})
    body = r.json()
    assert body["filtered"] is True and seen["scope"]["tenant_id"] == "T1" and seen["scope"]["catalog_category_id"] == "C1"
    hit = body["hits"][0]
    assert (hit["rule_source"], hit["is_demo_rule"], hit["version"]) == ("沙盒热水器规则", True, 4)
    assert bad.status_code == 400


def test_writer_stores_rule_only_when_collection_supports_it():
    from test_internal_api import FakeManager, FakeStore
    for enabled in (True, False):
        store = FakeStore()
        settings = config.Settings(_env_file=None, kb_rule_metadata=enabled)
        writer = KbWriter(settings, SimpleNamespace(embedder=FakeEmbedder()), store_factory=lambda s=store: s,
                          manager_factory=lambda st: FakeManager())
        body = UpsertBody(chunk_id="c1", tenant_id="T1", catalog_category_id="C1", text="标题\n正文内容。",
                          rule_source="沙盒规则", version=2, effective_from=10, effective_to=20, is_demo_rule=True)
        result = writer.upsert(body)
        rules = {c.rule["tenant_id"] if c.rule else None for c in store.rows.values()}
        assert result["rule_metadata_stored"] is enabled
        assert rules == ({"T1"} if enabled else {None})
        if enabled:
            first = next(iter(store.rows.values())).rule
            assert (first["catalog_category_id"], first["effective_from"], first["effective_to"], first["version"]) == ("C1", 10, 20, 2)
            assert first["is_demo_rule"] is True


def test_upsert_body_validates_ids_and_window():
    with pytest.raises(ValueError):
        UpsertBody(chunk_id="c", tenant_id='T"1', text="x\ny", rule_source="s")
    with pytest.raises(ValueError):
        UpsertBody(chunk_id="c", tenant_id="T1", text="x\ny", rule_source="s", effective_from=9, effective_to=1)
