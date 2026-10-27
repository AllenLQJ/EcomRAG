"""python -m ingest.graph_load：把 data/graph 图数据导入 Neo4j；默认只预览，--yes 才写入。

只读进程环境（NEO4J_URI、NEO4J_USER、NEO4J_PASSWORD、NEO4J_DATABASE），不加载 .env。
所有节点带 :Ecom 标签，导入在单个写事务内先清空 :Ecom 子图再重建，不触碰库中其他数据。
"""
from __future__ import annotations

import argparse
import csv
import importlib
import os
import sys
from collections import defaultdict

from ingest.graph_data import DEFAULT_OUT, load_products
from ingest.loaders import resolve_data_path

NODE_LABELS = {"Product", "Series", "Category", "External", "Port", "Interface", "Item", "Scene", "Doc"}
REL_TYPES = {
    "IN_SERIES", "IN_CATEGORY", "RELATED_TO", "HAS_PORT", "HAS_INTERFACE", "INCLUDES", "NOT_INCLUDES",
    "FEATURES", "DESCRIBED_IN", "DESCRIBES", "CAN_HOLD",
    # derived_edges.csv 中的推导关系
    "CAN_POWER", "NOT_COMPATIBLE", "CABLE_SUPPORTS", "CABLE_INSUFFICIENT", "PASSTHROUGH",
    "PASSTHROUGH_INSUFFICIENT", "CONNECTS_TO", "POWERS",
}
NUMERIC = {"port_count", "power_w", "combined_max_w", "need_w", "c_out_ports"}
BATCH = 500


class Graph:
    def __init__(self):
        self.nodes = {}   # id -> (label, props)
        self.rels = defaultdict(list)   # type -> [(src, dst, props)]

    def node(self, label, node_id, **props):
        if label not in NODE_LABELS:
            raise ValueError(f"未知节点标签：{label}")
        current = self.nodes.get(node_id)
        if current is not None and current[0] != label:
            raise ValueError(f"节点 {node_id} 标签冲突：{current[0]} / {label}")
        merged = dict(current[1]) if current else {}
        merged.update({key: clean(key, value) for key, value in props.items()})
        merged["id"] = node_id
        self.nodes[node_id] = (label, merged)
        return node_id

    def rel(self, rel_type, src, dst, **props):
        if rel_type not in REL_TYPES:
            raise ValueError(f"未知关系类型：{rel_type}")
        self.rels[rel_type].append((src, dst, {key: clean(key, value) for key, value in props.items()}))

    def validate(self):
        missing = sorted({node for rows in self.rels.values() for src, dst, _ in rows
                          for node in (src, dst) if node not in self.nodes})
        if missing:
            raise ValueError(f"关系端点缺少节点：{missing[:10]}")

    def counts(self):
        labels = defaultdict(int)
        for label, _ in self.nodes.values():
            labels[label] += 1
        return dict(sorted(labels.items())), {key: len(rows) for key, rows in sorted(self.rels.items())}


def clean(key, value):
    """CSV 空串存为缺省属性；功率、口数等转数字，便于 Cypher 比较。"""
    if value is None or value == "":
        return None
    if key in NUMERIC and isinstance(value, str):
        number = float(value)
        return int(number) if number.is_integer() and key in {"port_count", "c_out_ports"} else number
    return value


def read_csv(folder, name):
    with (folder / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_graph(data_dir=DEFAULT_OUT, products=None, docs_dir="data/docs"):
    folder = resolve_data_path(data_dir)
    products = load_products() if products is None else products
    doc_names = {path.name for path in resolve_data_path(docs_dir).glob("*.md")}
    multi = {row["src"]: row for row in read_csv(folder, "multi_device.csv")}
    graph = Graph()

    for sku, row in products.items():
        extra = multi.get(sku, {})
        graph.node("Product", sku, name=row["name"], series=row["series"], category=row["category"],
                   tagline=row["tagline"], positioning=row["positioning"],
                   c_out_ports=extra.get("c_out_ports"), combined_max_w=extra.get("combined_max_w"),
                   two_c_devices=extra.get("two_c_devices"), three_c_devices=extra.get("three_c_devices"))
        graph.rel("IN_SERIES", sku, graph.node("Series", f"series:{row['series']}", name=row["series"]))
        graph.rel("IN_CATEGORY", sku, graph.node("Category", f"category:{row['category']}", name=row["category"]))
        doc = f"product_{sku}.md"
        if doc in doc_names:
            graph.rel("DESCRIBES", graph.node("Doc", doc, name=doc), sku)
    for sku, row in products.items():
        for related in filter(None, row["related_skus"].split(";")):
            if related in products:
                graph.rel("RELATED_TO", sku, related)

    for row in read_csv(folder, "external_devices.csv"):
        graph.node("External", row["ext_id"], name=row["name"], connector=row["connector"],
                   need_w=row["need_w"], note=row["note"])
    for row in read_csv(folder, "power_specs.csv"):
        port_id = graph.node("Port", f"{row['sku_id']}#{row['port_id']}",
                             **{k: v for k, v in row.items() if k not in {"sku_id", "name", "category"}})
        graph.rel("HAS_PORT", row["sku_id"], port_id)
    for i, row in enumerate(read_csv(folder, "interfaces.csv")):
        iface_id = graph.node("Interface", f"{row['sku_id']}#if{i}",
                              **{k: v for k, v in row.items() if k not in {"sku_id", "name"}})
        graph.rel("HAS_INTERFACE", row["sku_id"], iface_id)
    for row in read_csv(folder, "package_items.csv"):
        if row["item_kind"] in {"manual", "product_body"}:
            continue
        item = graph.node("Item", f"item:{row['item']}", name=row["item"], kind=row["item_kind"])
        graph.rel("INCLUDES" if row["included"] == "是" else "NOT_INCLUDES", row["sku_id"], item,
                  cable_end=row["cable_end"])
    for row in read_csv(folder, "storage_fit.csv"):
        target = row["item"]
        if target not in products and target not in graph.nodes:
            target = graph.node("Item", f"item:{target}", name=target, kind="other")
        graph.rel("CAN_HOLD", row["bag"], target, level=row["level"], basis=row["basis"], note=row["note"])
    for row in read_csv(folder, "scenes.csv"):
        scene = graph.node("Scene", f"scene:{row['scene_id']}", name=row["scene"])
        graph.rel("FEATURES", scene, row["sku_id"], origin=row["origin"], source=row["source"], reason=row["reason"])
        if row["source"]:
            graph.rel("DESCRIBED_IN", scene, graph.node("Doc", row["source"], name=row["source"]))
    for row in read_csv(folder, "derived_edges.csv"):
        graph.rel(row["rel"], row["src"], row["dst"], level=row["level"], rule=row["rule"], detail=row["detail"],
                  cable=row["cable"])
    # 同一场景文档会被多行引用，DESCRIBED_IN 去重。
    graph.rels["DESCRIBED_IN"] = list({(s, d): (s, d, p) for s, d, p in graph.rels["DESCRIBED_IN"]}.values())
    graph.validate()
    return graph


def neo4j_config():
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        raise RuntimeError("未设置 NEO4J_PASSWORD；CLI 不读取 .env，请先在当前 shell 中 export")
    return {"uri": os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
            "user": os.environ.get("NEO4J_USER", "neo4j"), "password": password,
            "database": os.environ.get("NEO4J_DATABASE", "neo4j")}


def connect_neo4j(config):
    try:
        driver_module = importlib.import_module("neo4j")
    except ImportError as exc:
        raise RuntimeError("图谱导入需要 neo4j 驱动，请在当前 Python 环境安装项目已声明的依赖") from exc
    return driver_module.GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))


def batches(rows, size=BATCH):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def write_graph(tx, graph):
    """在调用方提供的写事务内执行；标签和关系类型已白名单校验后才拼进 Cypher。"""
    tx.run("MATCH (n:Ecom) DETACH DELETE n")
    by_label = defaultdict(list)
    for node_id, (label, props) in graph.nodes.items():
        by_label[label].append({"id": node_id, "props": {k: v for k, v in props.items() if v is not None}})
    for label, rows in sorted(by_label.items()):
        for chunk in batches(rows):
            tx.run(f"UNWIND $rows AS row CREATE (n:Ecom:{label}) SET n = row.props", rows=chunk)
    for rel_type, rows in sorted(graph.rels.items()):
        payload = [{"src": s, "dst": d, "props": {k: v for k, v in p.items() if v is not None}} for s, d, p in rows]
        for chunk in batches(payload):
            tx.run(f"UNWIND $rows AS row MATCH (a:Ecom {{id: row.src}}) MATCH (b:Ecom {{id: row.dst}}) "
                   f"CREATE (a)-[r:{rel_type}]->(b) SET r = row.props", rows=chunk)


def load(graph, driver, database):
    # 唯一约束不能与数据写入放在同一事务，先单独幂等创建。
    driver.execute_query("CREATE CONSTRAINT ecom_id IF NOT EXISTS FOR (n:Ecom) REQUIRE n.id IS UNIQUE",
                         database_=database)
    with driver.session(database=database) as session:
        session.execute_write(write_graph, graph)


def main(argv=None, *, driver_factory=None):
    parser = argparse.ArgumentParser(description="导入图数据到 Neo4j；不加载 .env，默认只预览，--yes 才写入")
    parser.add_argument("--data-dir", default=DEFAULT_OUT, help="图数据目录，默认 data/graph")
    parser.add_argument("--yes", action="store_true", help="清空 :Ecom 子图并重新导入")
    args = parser.parse_args(argv)
    try:
        graph = build_graph(args.data_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"图数据读取失败：{exc}", file=sys.stderr)
        return 1
    labels, rels = graph.counts()
    print("节点：" + "，".join(f"{k} {v}" for k, v in labels.items()) + f"（共 {len(graph.nodes)}）")
    print("关系：" + "，".join(f"{k} {v}" for k, v in rels.items()) + f"（共 {sum(rels.values())}）")
    if not args.yes:
        print("预览模式未写入；确认后加 --yes", file=sys.stderr)
        return 0
    try:
        config = neo4j_config()
        driver = (driver_factory or connect_neo4j)(config)
        try:
            load(graph, driver, config["database"])
        finally:
            driver.close()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # 不回显驱动异常，避免把连接串或认证信息写进终端日志。
        print("Neo4j 导入失败，请检查服务是否启动、NEO4J_URI 与账号密码", file=sys.stderr)
        return 1
    print(f"已导入 {len(graph.nodes)} 个节点、{sum(rels.values())} 条关系")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
