"""手动真实环境演示：python -m eval.graph_demo '衡电 A1 能给栖光 L1 供电吗'。

流程：规则实体链接 → 关键词意图 → 选 Cypher 模板 → 只读查询 Neo4j → 打印实体、Cypher 与结果。
不调用模型；查询规划与在线问答共用 app.graph.planner。结论只代表规格层面匹配。
--eval 对 data/graph/eval_graph.jsonl 逐题统计 expected_skus 召回；--cypher 直接执行只读 Cypher。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from app.graph.planner import plan_queries, result_ids
from ingest.graph_data import CAVEAT, DEFAULT_OUT, load_products
from ingest.graph_load import connect_neo4j, neo4j_config
from ingest.loaders import resolve_data_path


def run_read(driver, database, query, params):
    # routing_="r" 即 RoutingControl.READ：以读访问模式执行，服务端会拒绝写操作。
    records, _, _ = driver.execute_query(query, params, database_=database, routing_="r")
    return [record.data() for record in records]


def answer(text, products, runner):
    linked, intent, plans = plan_queries(text, products)
    results = [(title, query, params, runner(query, params)) for title, query, params in plans]
    # 只统计查询结果里出现的 id，不把问句里链接到的实体算作图谱找到的答案。
    found = {i for *_, rows in results for i in result_ids(rows)}
    levels = " ".join(str(v) for *_, rows in results for row in rows for k, v in row.items() if k == "level")
    return {"linked": linked, "intent": intent, "results": results, "found": found,
            "caveat": CAVEAT if "匹配" in levels else ""}


def print_answer(text, outcome):
    linked = outcome["linked"]
    print(f"问题：{text}")
    print(f"实体：{linked['entities'] or '无'}  场景：{linked['scenes'] or '无'}  "
          f"品类：{linked['categories'] or '无'}  意图：{outcome['intent']}")
    if not outcome["results"]:
        print("没有匹配的查询模板；可用 --cypher 直接查询")
    for title, query, params, rows in outcome["results"]:
        print(f"\n== {title}\n{query}\n参数：{json.dumps(params, ensure_ascii=False)}")
        if not rows:
            print("（无结果：图中不存在满足条件的路径）")
        for row in rows:
            print("  " + json.dumps(row, ensure_ascii=False))
    if outcome["caveat"]:
        print(f"\n{outcome['caveat']}")


def evaluate(path, products, runner):
    totals = defaultdict(lambda: [0, 0.0, 0])   # kind -> [题数, 召回和, 全命中]
    misses = []
    with resolve_data_path(path).open(encoding="utf-8") as handle:
        cases = [json.loads(line) for line in handle if line.strip()]
    for case in cases:
        # 多轮题把历史用户轮拼在前面做实体链接，模拟改写后的完整问句。
        text = " ".join([turn["content"] for turn in case.get("history", []) if turn["role"] == "user"]
                        + [case["question"]])
        outcome = answer(text, products, runner)
        expected = set(case["expected_skus"])
        hit = expected & outcome["found"]
        recall = len(hit) / len(expected) if expected else 1.0
        stat = totals[case["kind"]]
        stat[0] += 1
        stat[1] += recall
        stat[2] += recall == 1.0
        if recall < 1.0:
            misses.append({"id": case["id"], "kind": case["kind"], "intent": outcome["intent"],
                           "missing": sorted(expected - hit)})
    count = sum(s[0] for s in totals.values())
    print(f"共 {count} 题；按题型统计 expected_skus 召回：")
    for kind, (n, recall_sum, full) in sorted(totals.items()):
        print(f"  {kind:<14} 题数 {n:>3}  平均召回 {recall_sum / n:.2f}  全命中 {full}/{n}")
    overall = sum(s[1] for s in totals.values()) / count if count else 0
    print(f"总体平均召回 {overall:.2f}，全命中 {sum(s[2] for s in totals.values())}/{count}")
    for miss in misses:
        print("  未全命中 " + json.dumps(miss, ensure_ascii=False))
    return {"count": count, "recall": overall, "misses": misses}


def main(argv=None, *, driver_factory=None, products=None):
    parser = argparse.ArgumentParser(description="图谱演示：实体链接 + Cypher 模板查询 Neo4j；不加载 .env")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("question", nargs="?", help="中文问题")
    group.add_argument("--eval", nargs="?", const=f"{DEFAULT_OUT}/eval_graph.jsonl", help="批量统计召回")
    group.add_argument("--cypher", help="直接执行只读 Cypher")
    parser.add_argument("--plan-only", action="store_true", help="只打印实体链接与查询计划，不连接 Neo4j")
    args = parser.parse_args(argv)
    products = load_products() if products is None else products
    if args.plan_only:
        if not args.question:
            parser.error("--plan-only 需要提供问题")
        linked, intent, plans = plan_queries(args.question, products)
        print(json.dumps({"linked": linked, "intent": intent,
                          "plans": [{"title": t, "cypher": q, "params": p} for t, q, p in plans]},
                         ensure_ascii=False, indent=2))
        return 0
    try:
        config = neo4j_config()
        driver = (driver_factory or connect_neo4j)(config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    def runner(query, params):
        return run_read(driver, config["database"], query, params)

    try:
        if args.cypher:
            for row in runner(args.cypher, {}):
                print(json.dumps(row, ensure_ascii=False, default=str))
        elif args.eval:
            evaluate(args.eval, products, runner)
        else:
            print_answer(args.question, answer(args.question, products, runner))
    except Exception as exc:
        # 只回显异常类型；Cypher 语法错误等可用 Neo4j Browser（7474）进一步排查。
        print(f"图谱查询失败：{type(exc).__name__}，请检查 Neo4j 是否已启动并完成 ingest.graph_load --yes",
              file=sys.stderr)
        return 1
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
