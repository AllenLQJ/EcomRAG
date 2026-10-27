import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import Settings
from app.contracts import RetrievedDoc
from app.graph import planner
from app.graph.lookup import GraphLookup, render
from app.llm.generator import GeneratedAnswer
from app.pipeline import REFUSAL, run_chat
from app.schemas import ChatRequest
from chat_fakes import FakeServices, document
from ingest.graph_data import CAVEAT, load_products

PRODUCTS = load_products()
PAIR_ROW = {"src_id": "hengdian-a1", "src": "衡电 A1 充电器", "rel": "CAN_POWER", "level": "规格层面匹配",
            "detail": "C 口单口最高 20 瓦 ≥ 受电 10 瓦", "cable": "受电端自带充电线",
            "dst_id": "qiguang-l1", "dst": "栖光 L1 桌面台灯"}


class FakeRecord(dict):
    def data(self):
        return dict(self)


class FakeDriver:
    def __init__(self, rows_for=None):
        self.queries, self.closed = [], False
        self.rows_for = rows_for or (lambda query, params: [])

    def execute_query(self, query, params=None, **options):
        self.queries.append((query, params, options))
        return [FakeRecord(row) for row in self.rows_for(query.text, params)], None, None

    def close(self):
        self.closed = True


def lookup_with(rows_for=None, **overrides):
    driver = FakeDriver(rows_for)
    factory = Mock(return_value=driver)
    settings = Settings(_env_file=None, graph_enabled=True, neo4j_password="test-password", **overrides)
    return GraphLookup(settings, driver_factory=factory, products=PRODUCTS), driver, factory


def test_lookup_renders_pair_relation_as_document():
    lookup, driver, _ = lookup_with(lambda query, params: [PAIR_ROW] if query == planner.Q_PAIR else [])
    doc = lookup.lookup("衡电 A1 能给栖光 L1 供电吗")
    assert doc.title == "商品兼容关系（知识图谱）" and doc.doc_id == "graph:power"
    assert doc.score_provider == "graph" and doc.skus == ["hengdian-a1", "qiguang-l1"]
    assert "供电端：衡电 A1 充电器；关系：可供电；结论：规格层面匹配" in doc.content
    assert "hengdian-a1" not in doc.content and doc.content.endswith(CAVEAT)
    assert doc.snippet.startswith("供电端：衡电 A1 充电器")
    query, _, options = driver.queries[0]
    assert query.timeout == 1.5 and options == {"database_": "neo4j", "routing_": "r"}


@pytest.mark.parametrize("question", ["栖光 L1 多少钱", "学生有优惠吗", "怎么申请退货？"])
def test_lookup_skips_questions_without_specific_template(question):
    lookup, _, factory = lookup_with()
    assert lookup.lookup(question) is None
    factory.assert_not_called()


def test_lookup_without_rows_returns_none_and_reuses_driver():
    lookup, driver, factory = lookup_with()
    assert lookup.lookup("衡电 A1 能给栖光 L1 供电吗") is None
    assert lookup.lookup("衡电 A2 能给栖光 L1 供电吗") is None
    factory.assert_called_once()
    lookup.close()
    assert driver.closed


def test_render_names_ids_lists_and_truncates():
    names = {"hengdian-a1": "衡电 A1 充电器", "ext-laptop-65w": "65 瓦电脑"}
    rows = [{"hub_id": "hengdian-a1", "rel": "PASSTHROUGH", "ext_id": "ext-laptop-65w",
             "src_ids": ["hengdian-a1"], "path": ["甲", "乙"]}]
    text = render([("标题", rows)], names, 500)
    assert "扩展器：衡电 A1 充电器；关系：可直通供电；外部设备：65 瓦电脑；供电商品：衡电 A1 充电器；连接路径：甲 → 乙" in text
    long = render([("标题", [{"detail": "很长" * 100}] * 20)], names, 500)
    assert len(long) <= 500 and "（结果较多，已截断）" in long and long.endswith(CAVEAT)


def graph_doc():
    return RetrievedDoc(parent_id="graph:power", doc_id="graph:power", title="商品兼容关系（知识图谱）",
                        source="graph", content="供电端：衡电 A1；结论：规格层面匹配", snippet="供电端：衡电 A1",
                        score=1.0, skus=["hengdian-a1"], score_provider="graph")


def run(services, *, history=None, **overrides):
    overrides.setdefault("speculative_retrieval", False)
    chat = ChatRequest(session_id="test", message="原问题", debug=True, history=history or [])
    return asyncio.run(run_chat(chat, settings=Settings(_env_file=None, allow_trace=True, **overrides),
                                services=services))


def test_graph_document_is_first_context_and_cited_source():
    services = FakeServices().knowledge()
    services.graph_lookup = AsyncMock(return_value=graph_doc())
    services.generate_answer.return_value = GeneratedAnswer("衡电 A1 规格层面匹配[1]；父块完整内容[2]", {1, 2})
    result = run(services)
    services.graph_lookup.assert_awaited_once_with("原问题")
    docs = services.generate_answer.await_args.args[1]
    assert [doc.doc_id for doc in docs] == ["graph:power", "d1"]
    assert [(source.index, source.doc_id) for source in result.sources] == [(1, "graph:power"), (2, "d1")]


@pytest.mark.parametrize("retrieved", [[], [document(score=0.05).model_copy(update={"score_provider": "local"})]])
def test_graph_hit_answers_instead_of_low_score_refusal(retrieved, caplog):
    services = FakeServices().knowledge()
    services.retrieve.return_value = retrieved
    services.graph_lookup = AsyncMock(return_value=graph_doc())
    with caplog.at_level(logging.INFO, logger="app.pipeline"):
        result = run(services)
    assert result.route == "rag"
    assert [doc.doc_id for doc in services.generate_answer.await_args.args[1]] == ["graph:power"]
    assert "graph_answer_without_docs" in caplog.text


def test_low_score_without_graph_hit_still_refuses():
    services = FakeServices().knowledge()
    services.retrieve.return_value = [document(score=0.05).model_copy(update={"score_provider": "local"})]
    services.graph_lookup = AsyncMock(return_value=None)
    result = run(services)
    assert result.route == "fallback" and result.answer == REFUSAL
    services.generate_answer.assert_not_awaited()


async def slow_lookup(question):
    await asyncio.sleep(1)


@pytest.mark.parametrize("lookup", [AsyncMock(side_effect=ConnectionError("bolt down")), slow_lookup])
def test_graph_failure_or_timeout_falls_back_to_vector_context(lookup, caplog):
    services = FakeServices().knowledge()
    services.graph_lookup = lookup
    result = run(services, graph_timeout_s=0.05)
    assert result.route == "rag"
    assert [doc.doc_id for doc in services.generate_answer.await_args.args[1]] == ["d1"]
    graph = [item for item in result.trace if item.stage == "graph"]
    assert graph[0].hit is False and graph[0].detail == "unavailable"
    assert "graph_unavailable" in caplog.text


def test_general_route_skips_graph_and_multiturn_uses_rewritten_query():
    services = FakeServices()
    services.graph_lookup = AsyncMock(return_value=None)
    run(services)
    services.graph_lookup.assert_not_awaited()
    services.knowledge()
    run(services, history=[{"role": "user", "content": "衡电 A1 怎么样"},
                           {"role": "assistant", "content": "可以快充"}])
    services.graph_lookup.assert_awaited_once_with("改写后的检索问题")


def test_lookup_trace_comes_from_worker_thread():
    lookup, _, _ = lookup_with(lambda query, params: [PAIR_ROW] if query == planner.Q_PAIR else [])
    services = FakeServices().knowledge()
    services.graph_lookup = lookup.lookup
    services.generate_answer.return_value = GeneratedAnswer("衡电 A1 能给栖光 L1 供电，规格层面匹配[1]", {1})
    chat = ChatRequest(session_id="test", message="衡电 A1 能给栖光 L1 供电吗", debug=True)
    result = asyncio.run(run_chat(chat, settings=Settings(_env_file=None, allow_trace=True,
                                                          speculative_retrieval=False), services=services))
    graph = [item for item in result.trace if item.stage == "graph"]
    assert graph[0].hit is True and graph[0].detail == "power" and graph[0].candidates == 1
    assert result.sources[0].doc_id == "graph:power"
