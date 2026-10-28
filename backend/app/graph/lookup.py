"""在线图谱查询：问句 → 查询计划 → 只读 Cypher → 一份「规格层面兼容关系」资料。

只在问句链接到商品/外部设备/系列且意图命中具体模板时查询；无结果返回 None。
驱动在首次查询时创建；导入本模块不加载 neo4j，也不读取配置文件。
"""
from __future__ import annotations

import importlib
import threading
from time import perf_counter

from app.contracts import RetrievedDoc
from app.graph.planner import Q_NEIGHBORS, build_aliases, plan_queries, result_ids
from app.tracing import record
from ingest.graph_data import CAVEAT, EXTERNAL, load_products

TITLE = "商品兼容关系（知识图谱）"
PREAMBLE = "以下为店内商品知识图谱按商品参数推导的关系，非实测结果："
MAX_ROWS = 20
MAX_LINE = 300  # GRAPH_MAX_CHARS 最小 500，保证至少一行结果能放下
TRUNCATED = "（结果较多，已截断）"
# 查询只取 MAX_ROWS + 1 行，不知道总数；模板已按匹配程度排序，列出的是最匹配的部分。
MORE_ROWS = "- 其余结果未列出（已按匹配程度优先列出）"
REL_LABELS = {
    "CAN_POWER": "可供电", "NOT_COMPATIBLE": "不匹配", "INCLUDES": "包装含", "NOT_INCLUDES": "包装不含",
    "CONNECTS_TO": "可连接", "POWERS": "可供电", "CAN_HOLD": "可收纳", "CABLE_SUPPORTS": "线材功率满足",
    "CABLE_INSUFFICIENT": "线材功率不足", "PASSTHROUGH": "可直通供电", "PASSTHROUGH_INSUFFICIENT": "直通功率不足",
}
# 按查询返回列的顺序输出；*_id 列只用于商品卡片和名称替换，不直接写进资料。
FIELD_LABELS = {
    "scene": "场景", "src": "供电端", "source": "供电端", "bag": "收纳包", "product": "商品", "rel": "关系",
    "dst": "对象", "item": "物品", "via": "经由", "cable": "线材", "level": "结论", "detail": "依据",
    "basis": "依据", "source_detail": "供电端依据", "cable_detail": "线材依据", "hub_detail": "扩展器依据",
    "cable_end": "线材接口", "kind": "类型", "connector": "接口", "direction": "方向", "ports": "口数",
    "spec": "规格", "power_w": "最高功率（瓦）", "role": "供电方式", "c_out_ports": "C 口输出数",
    "combined_max_w": "多口合计最高功率（瓦）", "two_devices": "两台同时", "three_devices": "三台同时",
    "path": "连接路径", "note": "备注",
}
NAMED_IDS = {"ext_id": "外部设备", "hub_id": "扩展器", "src_ids": "供电商品"}


def should_query(linked, intent, plans):
    """一跳邻居噪声大；纯场景问法由场景文档检索覆盖，避免「学生」等泛词误触发。"""
    if not plans or all(query == Q_NEIGHBORS for _, query, _ in plans):
        return False
    return not (intent == "scene" and not linked["entities"])


def render_value(value, names):
    if isinstance(value, list):
        return "、".join(names.get(v, str(v)) for v in value if v not in (None, ""))
    return "" if value is None else names.get(value, str(value)) if isinstance(value, str) else str(value)


def render_row(row, names):
    parts = []
    for key, value in row.items():
        if key in NAMED_IDS:
            text = render_value(value, names)
            label = NAMED_IDS[key]
        elif key == "devices":
            text = "；".join(f"{names.get(d['dst_id'], d['dst_id'])}（{d.get('level') or '未注明'}）"
                            for d in value or [] if isinstance(d, dict) and d.get("dst_id"))
            label = "各设备"
        elif key in FIELD_LABELS:
            text = render_value(REL_LABELS.get(value, value) if key == "rel" else value, names)
            if key == "path":
                text = text.replace("、", " → ")
            label = FIELD_LABELS[key]
        else:
            continue
        if text:
            parts.append(f"{label}：{text}")
    return "；".join(parts)


def render(results, names, max_chars):
    lines = [PREAMBLE]
    for title, rows in results:
        if not rows:
            continue
        lines.append(f"【{title}】")
        lines.extend(f"- {render_row(row, names)}"[:MAX_LINE] for row in rows[:MAX_ROWS])
        if len(rows) > MAX_ROWS:
            lines.append(MORE_ROWS)
    budget = max_chars - len(CAVEAT) - len(TRUNCATED) - 2
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            kept.append(TRUNCATED)
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join([*kept, CAVEAT])


def ordered_ids(rows):
    found = []
    for row in rows:
        found.extend(sorted(result_ids([row]) - set(found)))
    return found


class GraphLookup:
    def __init__(self, settings, *, driver_factory=None, products=None):
        self.settings = settings
        self._driver_factory = driver_factory or self._connect
        self._products = products
        self._aliases = self._names = None
        self._driver = None
        self._lock = threading.Lock()

    def _connect(self):
        neo4j = importlib.import_module("neo4j")
        timeout = self.settings.graph_timeout_s
        return neo4j.GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password.get_secret_value()),
            connection_timeout=timeout, connection_acquisition_timeout=timeout,
            max_transaction_retry_time=0)

    def _load(self):
        with self._lock:
            if self._aliases is None:
                products = load_products() if self._products is None else self._products
                self._products = products
                self._aliases = build_aliases(products)
                self._names = {**{sku: row["name"] for sku, row in products.items()},
                               **{row[0]: row[1] for row in EXTERNAL}}

    def _connection(self):
        with self._lock:
            if self._driver is None:
                self._driver = self._driver_factory()
            return self._driver

    def _read(self, driver, query, params):
        neo4j = importlib.import_module("neo4j")
        # 读路由 + 单条查询超时；服务端拒绝写操作，超时由 Neo4j 中止查询。
        records, _, _ = driver.execute_query(neo4j.Query(query, timeout=self.settings.graph_timeout_s), params,
                                             database_=self.settings.neo4j_database, routing_="r")
        return [item.data() for item in records]

    def lookup(self, question):
        started = perf_counter()
        self._load()
        linked, intent, plans = plan_queries(question, self._products, self._aliases, limit=MAX_ROWS + 1)
        if not should_query(linked, intent, plans):
            record("graph", started, hit=False, detail="skipped")
            return None
        driver = self._connection()
        results = []
        for title, query, params in plans:
            # 查询串行执行；整体超过超时就放弃，避免超时后工作线程仍继续逐条查询。
            if perf_counter() - started > self.settings.graph_timeout_s:
                raise TimeoutError("graph lookup exceeded timeout")
            results.append((title, self._read(driver, query, params)))
        rows = [row for _, found in results for row in found]
        record("graph", started, hit=bool(rows), detail=intent, candidates=len(rows))
        if not rows:
            return None
        content = render(results, self._names, self.settings.graph_max_chars)
        linked_skus = [e for e in linked["entities"] if e in self._products]
        skus = list(dict.fromkeys([*linked_skus, *(i for i in ordered_ids(rows) if i in self._products)]))
        first = next(line[2:] for line in content.split("\n") if line.startswith("- "))
        return RetrievedDoc(parent_id=f"graph:{intent}", doc_id=f"graph:{intent}", title=TITLE, source="graph",
                            content=content, snippet=first[:60], score=1.0, skus=skus, score_provider="graph")

    def close(self):
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None
