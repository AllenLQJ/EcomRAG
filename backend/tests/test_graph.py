import csv
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from app.graph import planner
from eval import graph_demo
from ingest import graph_data, graph_load
from ingest.loaders import resolve_data_path


def generated():
    if not hasattr(generated, "cache"):
        generated.cache = graph_data.generate()
    return generated.cache


def table(name):
    return list(csv.DictReader(io.StringIO(generated()[name])))


def edge(src, dst):
    return next(row for row in table("derived_edges.csv") if row["src"] == src and row["dst"] == dst)


class FakeRecord:
    def __init__(self, row):
        self.row = row

    def data(self):
        return dict(self.row)


class FakeTx:
    def __init__(self):
        self.calls = []

    def run(self, query, **params):
        self.calls.append((query, params))


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute_write(self, function, *args):
        tx = FakeTx()
        function(tx, *args)
        self.driver.transactions.append(tx)


class FakeDriver:
    def __init__(self, rows_for=None):
        self.queries, self.transactions, self.closed = [], [], False
        self.rows_for = rows_for or (lambda query, params: [])

    def execute_query(self, query, params=None, **options):
        self.queries.append((query, params, options))
        return [FakeRecord(row) for row in self.rows_for(query, params or {})], None, None

    def session(self, database=None):
        self.database = database
        return FakeSession(self)

    def close(self):
        self.closed = True


class GraphDataTest(unittest.TestCase):
    def test_committed_files_match_generator(self):
        folder = resolve_data_path(graph_data.DEFAULT_OUT)
        for name, text in generated().items():
            with self.subTest(name=name):
                self.assertEqual((folder / name).read_bytes().decode("utf-8"), text)

    def test_power_rules(self):
        self.assertEqual(edge("hengdian-a1", "qiguang-l1")["level"], graph_data.LEVEL_SPEC)
        self.assertEqual(edge("hengdian-a3", "qiguang-l4")["rel"], "NOT_COMPATIBLE")
        self.assertEqual(edge("hengdian-a1", "tinglan-s1")["level"], graph_data.LEVEL_IFACE)
        self.assertEqual(edge("hengdian-a2", "hengdian-p3")["level"], graph_data.LEVEL_PARTIAL)
        self.assertEqual(edge("lianshu-c1", "ext-laptop-65w")["rel"], "CABLE_INSUFFICIENT")
        self.assertEqual(edge("lianshu-c2", "ext-laptop-65w")["rel"], "CABLE_SUPPORTS")
        self.assertEqual(edge("lianshu-h1", "ext-laptop-100w")["rel"], "PASSTHROUGH_INSUFFICIENT")

    def test_dc_sinks_have_no_store_source(self):
        for sku in graph_data.DC_SINKS:
            rels = {row["rel"] for row in table("derived_edges.csv") if row["dst"] == sku}
            self.assertEqual(rels, {"NOT_COMPATIBLE"})

    def test_multi_device_ports(self):
        multi = {row["src"]: row for row in table("multi_device.csv")}
        self.assertEqual(multi["hengdian-a1"]["two_c_devices"], "否")
        self.assertEqual(multi["hengdian-a2"]["two_c_devices"], "是")
        self.assertFalse([sku for sku, row in multi.items() if row["three_c_devices"] == "是"])

    def test_doc_scenes_are_grounded(self):
        for row in table("scenes.csv"):
            if row["origin"] == "doc":
                text = (resolve_data_path("data/docs") / row["source"]).read_text(encoding="utf-8")
                self.assertIn(row["sku_id"], text)

    def test_eval_cases_reference_existing_entities_and_docs(self):
        products = graph_data.load_products()
        externals = {row[0] for row in graph_data.EXTERNAL}
        docs = {path.name for path in resolve_data_path("data/docs").glob("*.md")}
        cases = [json.loads(line) for line in generated()["eval_graph.jsonl"].splitlines()]
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            self.assertEqual(case["expected_route"], "answer")
            self.assertTrue(case["expected_doc"])
            self.assertTrue(set(case["expected_doc"]) <= docs, case["id"])
            self.assertTrue(set(case["expected_skus"]) <= set(products) | externals, case["id"])

    def test_compatibility_answers_carry_caveat(self):
        for line in generated()["eval_graph.jsonl"].splitlines():
            case = json.loads(line)
            answer = case["reference_answer"]
            # 收纳题的「类别匹配」与场景题不是兼容判断，不要求带规格层面提示。
            # 「匹配适配器」是包装物品名称，也不算兼容结论。
            body = answer.replace(graph_data.CAVEAT, "").replace("匹配适配器", "")
            if case["kind"] not in {"storage", "scene"} and "匹配" in body:
                self.assertTrue(answer.endswith(graph_data.CAVEAT), case["id"])

    def test_preview_does_not_write(self):
        with patch.object(graph_data, "generate", return_value={"x.csv": "a\n1\n"}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with patch("pathlib.Path.write_bytes") as write:
                    self.assertEqual(graph_data.main(["--out", "data/graph"]), 0)
        write.assert_not_called()


class GraphLoadTest(unittest.TestCase):
    def setUp(self):
        self.graph = graph_load.build_graph()

    def test_build_graph_counts_and_endpoints(self):
        labels, rels = self.graph.counts()
        self.assertEqual(labels["Product"], 50)
        self.assertEqual(rels["CAN_POWER"], sum(1 for r in table("derived_edges.csv") if r["rel"] == "CAN_POWER"))
        self.assertEqual(rels["DESCRIBED_IN"], len(graph_data.DOC_SCENES))
        self.graph.validate()

    def test_numeric_properties(self):
        port = self.graph.nodes["hengdian-p2#c_io"][1]
        self.assertEqual(port["power_w"], 22.5)
        self.assertEqual(port["port_count"], 1)
        self.assertIsNone(self.graph.nodes["tinglan-s1#charge_in"][1]["power_w"])

    def test_validate_rejects_missing_endpoint(self):
        graph = graph_load.Graph()
        graph.node("Product", "a", name="A")
        graph.rel("CAN_POWER", "a", "missing")
        with self.assertRaises(ValueError):
            graph.validate()

    def test_rejects_unknown_label_and_type(self):
        graph = graph_load.Graph()
        with self.assertRaises(ValueError):
            graph.node("Product`) DETACH DELETE n //", "x")
        with self.assertRaises(ValueError):
            graph.rel("KNOWS]->() DELETE x //", "a", "b")

    def test_write_graph_clears_subgraph_first(self):
        tx = FakeTx()
        graph_load.write_graph(tx, self.graph)
        self.assertEqual(tx.calls[0][0], "MATCH (n:Ecom) DETACH DELETE n")
        created = sum(len(params["rows"]) for query, params in tx.calls if "CREATE (n:Ecom:" in query)
        self.assertEqual(created, len(self.graph.nodes))
        for query, params in tx.calls[1:]:
            self.assertTrue(all(None not in row["props"].values() for row in params["rows"]))

    def test_main_preview_needs_no_password(self):
        with patch.dict(os.environ, {"NEO4J_PASSWORD": ""}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(graph_load.main([]), 0)

    def test_main_requires_password_to_write(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"NEO4J_PASSWORD": ""}):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(graph_load.main(["--yes"], driver_factory=lambda config: FakeDriver()), 1)
        self.assertIn("NEO4J_PASSWORD", stderr.getvalue())

    def test_main_writes_in_one_transaction(self):
        driver = FakeDriver()
        with patch.dict(os.environ, {"NEO4J_PASSWORD": "test-password", "NEO4J_DATABASE": "neo4j"}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(graph_load.main(["--yes"], driver_factory=lambda config: driver), 0)
        self.assertIn("CREATE CONSTRAINT ecom_id IF NOT EXISTS", driver.queries[0][0])
        self.assertEqual(len(driver.transactions), 1)
        self.assertTrue(driver.closed)

    def test_main_hides_driver_errors(self):
        class Broken(FakeDriver):
            def execute_query(self, *args, **kwargs):
                raise OSError("bolt://neo4j:secret@host failed")

        stderr = io.StringIO()
        with patch.dict(os.environ, {"NEO4J_PASSWORD": "test-password"}):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(graph_load.main(["--yes"], driver_factory=lambda config: Broken()), 1)
        self.assertNotIn("secret", stderr.getvalue())


class GraphDemoTest(unittest.TestCase):
    products = graph_data.load_products()

    def link(self, text):
        return planner.link_entities(text, self.products)["entities"]

    def test_link_aliases(self):
        self.assertEqual(self.link("衡电A1 能给栖光 L1 供电吗"), ["hengdian-a1", "qiguang-l1"])
        self.assertEqual(self.link("栖光 L1 和 L2 哪个亮"), ["qiguang-l1", "qiguang-l2"])
        self.assertEqual(self.link("hengdian-p3 怎么样"), ["hengdian-p3"])

    def test_bare_model_is_not_linked(self):
        # L1 同时是栖光台灯和纳境内胆包的型号，不带系列名时不猜测。
        self.assertEqual(self.link("L1 多少钱"), [])

    def test_link_external_devices(self):
        self.assertIn("ext-laptop-65w", self.link("65 瓦的电脑用什么充"))
        self.assertNotIn("ext-laptop-65w", self.link("65 瓦够吗"))
        self.assertIn("ext-monitor-hdmi", self.link("想接 HDMI 显示器"))

    def plan(self, text):
        linked, intent, plans = planner.plan_queries(text, self.products)
        return intent, [query for _, query, _ in plans]

    def test_intents_choose_templates(self):
        cases = [
            ("衡电 A1 能给栖光 L1 供电吗", "power", planner.Q_PAIR),
            ("栖光 L4 能用店里哪款充电器", "power", planner.Q_SOURCES),
            ("衡电 A3 配联枢 C2，65 瓦电脑够吗", "laptop", planner.Q_LAPTOP),
            ("65 瓦电脑经联枢 H1 充电行吗", "hub", planner.Q_HUB),
            ("衡电 A2 能同时给栖光 L1 和鸣澜 P2 供电吗", "multi", planner.Q_MULTI),
            ("联枢 C1 能接显示器吗", "signal", planner.Q_SIGNAL),
            ("纳境 P1 能装哪些东西", "storage", planner.Q_HOLD),
            ("通勤搭配推荐了什么", "scene", planner.Q_SCENE),
            ("通勤搭配里的充电宝能给耳机充电吗", "scene_power", planner.Q_SCENE_POWER),
            ("衡电 A2 配栖光 L5 行不行", "power", planner.Q_PAIR),
            ("哪些衡电产品自带充电线", "power", planner.Q_PACKAGE_FILTER),
            ("电脑只有 C 口，想接 HDMI 显示器和 A 口鼠标", "signal", planner.Q_SIGNAL_2HOP),
            ("办公桌面搭配里的台灯能和衡电 A1 一起用吗", "scene_source", planner.Q_SCENE_SOURCE),
            ("哪些商品不能用衡电充电器或充电宝供电？", "power", planner.Q_INCOMPATIBLE),
            ("键行哪些键盘鼠标不需要充电器？", "power", planner.Q_NO_CHARGER),
        ]
        for text, intent, query in cases:
            with self.subTest(text=text):
                got_intent, queries = self.plan(text)
                self.assertEqual(got_intent, intent)
                self.assertIn(query, queries)

    def test_hdmi_does_not_also_link_c_video_monitor(self):
        self.assertEqual(self.link("想接 HDMI 显示器"), ["ext-monitor-hdmi"])
        self.assertEqual(self.link("C 口线能接显示器吗"), ["ext-monitor-c-video"])

    def test_every_eval_case_gets_a_plan(self):
        for line in generated()["eval_graph.jsonl"].splitlines():
            case = json.loads(line)
            text = " ".join([t["content"] for t in case.get("history", []) if t["role"] == "user"]
                            + [case["question"]])
            intent, queries = self.plan(text)
            self.assertTrue(queries, case["id"])
            self.assertNotEqual(intent, "neighbors", case["id"])

    def test_answer_counts_only_result_ids(self):
        def runner(query, params):
            if query == planner.Q_PAIR:
                return [{"src_id": "hengdian-a1", "dst_id": "qiguang-l1", "level": graph_data.LEVEL_SPEC}]
            return []

        outcome = graph_demo.answer("衡电 A1 能给栖光 L1 供电吗", self.products, runner)
        self.assertEqual(outcome["found"], {"hengdian-a1", "qiguang-l1"})
        self.assertEqual(outcome["caveat"], graph_data.CAVEAT)
        empty = graph_demo.answer("衡电 A1 能给栖光 L1 供电吗", self.products, lambda q, p: [])
        self.assertEqual(empty["found"], set())
        self.assertEqual(empty["caveat"], "")

    def test_main_uses_read_routing(self):
        driver = FakeDriver(lambda query, params: [{"src_id": "hengdian-a1", "level": "规格层面匹配"}])
        with patch.dict(os.environ, {"NEO4J_PASSWORD": "test-password"}):
            with redirect_stdout(io.StringIO()) as out:
                code = graph_demo.main(["衡电 A1 能给栖光 L1 供电吗"], driver_factory=lambda config: driver)
        self.assertEqual(code, 0)
        self.assertTrue(all(options["routing_"] == "r" for _, _, options in driver.queries))
        self.assertIn(graph_data.CAVEAT, out.getvalue())
        self.assertTrue(driver.closed)

    def test_plan_only_needs_no_database(self):
        with patch.dict(os.environ, {"NEO4J_PASSWORD": ""}):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(graph_demo.main(["--plan-only", "衡电 A1 能给栖光 L1 供电吗"]), 0)
        self.assertEqual(json.loads(out.getvalue())["intent"], "power")

    def test_eval_reports_recall(self):
        def runner(query, params):
            return [{"src_id": sku} for sku in params.get("ids", [])]

        with redirect_stdout(io.StringIO()):
            report = graph_demo.evaluate(f"{graph_data.DEFAULT_OUT}/eval_graph.jsonl", self.products, runner)
        self.assertEqual(report["count"], 94)
        self.assertGreaterEqual(report["recall"], 0)
