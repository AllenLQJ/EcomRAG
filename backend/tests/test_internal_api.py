"""内部接口：令牌门禁、纯检索（不经 LLM）、知识 upsert 的幂等与旧块清理。全部离线。"""
import contextlib
import hashlib
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import config
from app.contracts import RetrievedDoc
from app.internal_api import KbWriter, UpsertBody
from app.retrieval.embedder import FakeEmbedder
from chat_fakes import FakeServices

TOKEN = "internal-test-token"


def doc(parent, score, title="退款到账", snippet="原路退款 1–5 个工作日"):
    return RetrievedDoc(parent_id=parent, doc_id="d", title=title, source="s.md", content="父块", snippet=snippet, score=score)


class FakeStore:
    collection_name, timeout_s = "kb_chunks", 5

    def __init__(self, rows=None, shared=None):
        self.rows = shared if shared is not None else {}
        self.deleted, self.flushed, self.target = [], 0, None

    def for_collection(self, name):
        self.target = name
        return self

    def upsert(self, chunks, vectors):
        assert len(vectors.dense) == len(chunks)
        for chunk in chunks:
            self.rows[chunk.chunk_id] = chunk

    def delete_ids(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self.rows.pop(i, None)

    def flush_load(self):
        self.flushed += 1

    def _connect(self):
        store = self

        class Client:
            def query(self, collection_name, filter, output_fields, timeout):
                doc_id = filter.split('"')[1]
                return [{"chunk_id": k} for k, c in store.rows.items() if c.doc_id == doc_id]
        return Client()


class FakeManager:
    def __init__(self, current="kb_chunks_v1"):
        self._current, self.saved = current, 0
        self.state = {"versions": {current: {"status": "published"}} if current else {}}
        self.events = []

    @contextlib.contextmanager
    def locked(self):
        self.events.append("lock")
        yield
        self.events.append("unlock")

    def recover(self):
        self.events.append("recover")

    def current(self):
        return self._current

    def save(self):
        self.saved += 1


@pytest.fixture
def make_app(app_factory):
    def build(token=TOKEN, writer=None, services=None):
        values = {"debug": False, "llm_api_key": "", "internal_token": token}
        settings = config.Settings(_env_file=None, **values)
        from app import main
        app = main.create_app(settings, services=services or FakeServices(), kb_writer=writer)
        app.state.rate_limiter.cache._client = lambda: None
        return app
    return build


H = {"X-Internal-Token": TOKEN}


def test_endpoints_do_not_exist_without_a_configured_token(make_app):
    with TestClient(make_app(token="")) as client:
        assert client.post("/api/v1/rag/search", json={"query": "退款"}, headers=H).status_code == 404
        assert client.post("/internal/v1/kb/upsert", json={}, headers=H).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}, {"X-Internal-Token": ""}])
def test_wrong_or_missing_token_is_401(make_app, headers):
    with TestClient(make_app()) as client:
        r = client.post("/api/v1/rag/search", json={"query": "退款"}, headers=headers)
        assert r.status_code == 401 and r.json()["code"] == 40101
        assert client.post("/internal/v1/kb/upsert", json={}, headers=headers).status_code == 401


class VecEmbedder:
    """按文本返回预设的稠密向量（二维），用来控制余弦。"""
    def __init__(self, table):
        self.table = table

    def encode(self, texts):
        return SimpleNamespace(dense=[self.table[t] for t in texts], sparse=[None] * len(texts))


def test_search_maps_docs_to_hits_without_llm_or_guest_cookie(make_app):
    services = FakeServices()
    # 查询向量 (1,0)；p1 余弦 0.70 -> 校准 1.0；p2 余弦 0.575 -> 0.5
    services.embedder = VecEmbedder({"退款多久到账": [1.0, 0.0], "原路退款 1–5 个工作日": [0.70, math.sqrt(1 - 0.49)],
                                     "回到申请里提交": [0.575, math.sqrt(1 - 0.575 ** 2)]})
    services.retrieve.return_value = [doc("p2", 0.8, "补材料", "回到申请里提交"), doc("p1", 0.99)]
    with TestClient(make_app(services=services)) as client:
        r = client.post("/api/v1/rag/search", json={"query": "退款多久到账", "tenant_id": "T1",
                                                    "catalog_category_id": "X", "top_k": 2}, headers=H)
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [h["chunk_id"] for h in hits] == ["p1", "p2"]                       # 按校准分排序，不是按原重排分
    assert hits[0]["score"] == 1.0 and hits[1]["score"] == 0.5
    assert hits[0]["cosine"] == 0.7 and hits[0]["rerank_score"] == 0.99          # 原始分仅供参考
    assert hits[0]["text"] == "原路退款 1–5 个工作日" and hits[0]["rule_source"] == "退款到账" and hits[0]["is_demo_rule"] is False
    assert "set-cookie" not in r.headers                     # 不给服务间调用方种游客 cookie
    services.retrieve.assert_called_once()
    assert services.retrieve.call_args.args[0] == ["退款多久到账"] and services.retrieve.call_args.kwargs["top_k"] == 2
    services.route.assert_not_called()                       # 没有经过路由
    services.generate_answer.assert_not_called()             # 也没有生成：不调 LLM


def test_saturated_rerank_scores_do_not_make_irrelevant_hits_look_confident(make_app):
    """远程重排对库外问题也给 ~1.0；对外的 score 必须来自余弦校准，低相关就是低分。"""
    services = FakeServices()
    services.embedder = VecEmbedder({"跨境海淘税费": [1.0, 0.0], "原路退款 1–5 个工作日": [0.45, math.sqrt(1 - 0.45 ** 2)]})
    services.retrieve.return_value = [doc("p1", 0.999999)]
    with TestClient(make_app(services=services)) as client:
        hit = client.post("/api/v1/rag/search", json={"query": "跨境海淘税费"}, headers=H).json()["hits"][0]
    assert hit["rerank_score"] == 0.999999 and hit["score"] == 0.0 and hit["score"] < 0.75


def test_calibration_bounds_and_config_guard():
    from app.internal_api import calibrated
    assert calibrated(0.45, 0.45, 0.70) == 0.0 and calibrated(0.70, 0.45, 0.70) == 1.0
    assert calibrated(0.2, 0.45, 0.70) == 0.0 and calibrated(0.95, 0.45, 0.70) == 1.0
    assert calibrated(0.575, 0.45, 0.70) == 0.5
    with pytest.raises(ValueError):
        calibrated(0.5, 0.7, 0.7)


def test_search_empty_and_validation(make_app):
    services = FakeServices()
    services.embedder = VecEmbedder({})
    services.retrieve.return_value = []
    with TestClient(make_app(services=services)) as client:
        assert client.post("/api/v1/rag/search", json={"query": "库外问题"}, headers=H).json() == {"hits": []}
        for bad in ({"query": ""}, {"query": "  "}, {"query": "问" * 501}, {"query": "x", "top_k": 0}, {"query": "x", "top_k": 21}):
            assert client.post("/api/v1/rag/search", json=bad, headers=H).status_code == 400


def body(**over):
    values = dict(chunk_id="c" * 64, tenant_id="T1", text="热水器漏水能退吗？\n质量问题 15 天内可退换。", rule_source="沙盒热水器退换规则", version=1)
    values.update(over)
    return UpsertBody(**values)


def writer(store=None, manager=None, batch=16):
    settings = config.Settings(_env_file=None, embed_batch_size=batch)
    services = SimpleNamespace(embedder=FakeEmbedder())
    return KbWriter(settings, services, store_factory=lambda: store, manager_factory=lambda s: manager)


def test_upsert_writes_stable_doc_under_publish_lock_and_marks_modified_in_place():
    store, manager = FakeStore(), FakeManager()
    result = writer(store, manager).upsert(body())
    source = KbWriter.source_of("c" * 64)
    doc_id = hashlib.sha256(source.encode()).hexdigest()[:24]
    assert result["milvus_pk"] == doc_id and result["chunks"] == len(store.rows) >= 1
    assert all(k.startswith(doc_id + "#") for k in store.rows)
    chunk = next(iter(store.rows.values()))
    assert chunk.category == "after_sales" and chunk.title == "热水器漏水能退吗？" and "沙盒热水器退换规则" in chunk.parent_text
    assert manager.events == ["lock", "recover", "unlock"]
    assert manager.state["versions"]["kb_chunks_v1"]["status"] == "modified-in-place" and manager.saved == 1
    assert store.target == "kb_chunks_v1" and store.flushed == 1       # 固定写入当前发布的实体，而不是别名


def test_upsert_is_idempotent_same_rows_no_growth():
    store, manager = FakeStore(), FakeManager()
    w = writer(store, manager)
    first = w.upsert(body())
    keys = set(store.rows)
    second = w.upsert(body())
    assert set(store.rows) == keys and first["milvus_pk"] == second["milvus_pk"] and second["removed_stale"] == 0


def test_republish_with_shorter_text_removes_stale_high_index_chunks():
    store, manager = FakeStore(), FakeManager()
    w = writer(store, manager)
    long_text = "标题行\n" + "质量问题可以退换。" * 200
    w.upsert(body(text=long_text))
    before = len(store.rows)
    assert before > 1
    result = w.upsert(body(text="标题行\n只剩一句。"))
    assert len(store.rows) == result["chunks"] < before and result["removed_stale"] == before - result["chunks"]
    assert set(store.deleted) and not (set(store.deleted) & set(store.rows))


def test_different_chunk_ids_are_different_documents_and_do_not_delete_each_other():
    store, manager = FakeStore(), FakeManager()
    w = writer(store, manager)
    a, b = w.upsert(body(chunk_id="a" * 64)), w.upsert(body(chunk_id="b" * 64))
    assert a["milvus_pk"] != b["milvus_pk"] and len(store.rows) == a["chunks"] + b["chunks"]


def test_upsert_without_published_version_still_writes_to_alias():
    store, manager = FakeStore(), FakeManager(current=None)
    writer(store, manager).upsert(body())
    assert store.target is None and store.rows and manager.saved == 0


def test_upsert_endpoint_maps_success_validation_and_dependency_errors(make_app):
    class Boom:
        def upsert(self, body):
            raise ConnectionError("milvus down")
    store, manager = FakeStore(), FakeManager()
    with TestClient(make_app(writer=writer(store, manager))) as client:
        ok = client.post("/internal/v1/kb/upsert", json={"chunk_id": "c1", "tenant_id": "T1", "text": "标题\n正文内容。",
                                                         "rule_source": "来源", "version": 1}, headers=H)
        assert ok.status_code == 200 and ok.json()["chunk_id"] == "c1"
        assert client.post("/internal/v1/kb/upsert", json={"chunk_id": "c1", "tenant_id": "T1", "text": "  ",
                                                           "rule_source": "来源"}, headers=H).status_code == 400
    with TestClient(make_app(writer=Boom())) as client:
        down = client.post("/internal/v1/kb/upsert", json={"chunk_id": "c1", "tenant_id": "T1", "text": "标题\n正文。",
                                                           "rule_source": "来源"}, headers=H)
        assert down.status_code == 503 and "milvus" not in down.text
