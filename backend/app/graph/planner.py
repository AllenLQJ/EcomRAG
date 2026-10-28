"""图谱查询规划：规则实体链接 → 关键词意图 → Cypher 模板；纯函数，不访问数据库。

在线问答（app.graph.lookup）与离线演示（eval.graph_demo）共用。
"""
from __future__ import annotations

import re

from ingest.graph_data import EXTERNAL

SCENE_WORDS = {"commuting": ["通勤"], "business_travel": ["出差", "旅行"], "dormitory": ["宿舍"],
               "gift_selection": ["送礼"], "home_reading": ["居家阅读"], "office_desktop": ["办公桌面"],
               "short_outdoor_trips": ["户外"], "sports_fitness": ["运动健身", "健身"],
               "starter_setup": ["入门配置"], "student_study": ["学生学习", "学生"]}
EXTERNAL_PATTERNS = [
    (r"HDMI", "ext-monitor-hdmi"), (r"显示器|传画面|传视频|视频输出|输出视频", "ext-monitor-c-video"),
    (r"有线耳机", "ext-wired-earphone"), (r"音频线|有线音源|有线连", "ext-audio-cable"),
    (r"手机", "ext-phone-c"), (r"AA ?电池", "ext-aa-battery"),
]
INTENT_WORDS = [
    ("multi", r"同时|三件|两件|三个口"),
    ("storage", r"收纳|装进|装哪|能装|放进|放哪|适合放|内胆包|收在一起"),
    ("signal", r"显示器|视频|画面|HDMI|有线|音频|插在|接到|转接"),
    ("power", r"充电|供电|补电|充电器|充电宝|适配器|瓦"),
]
# 只提到一个非供电商品时，问句要在找供电端或问能否搭配，才按供电关系查询。
SOURCE_CUES = r"店里|店内|哪|什么|代替|能用|可以用|能不能用|能配|配什么|行不行|行吗"
PACKAGE_WORDS = r"送|附带|自带|包装|带不带"
CATEGORY_WORDS = {"台灯": "桌面台灯", "耳机": "无线耳机", "音箱": "蓝牙音箱", "充电宝": "充电设备", "充电器": "充电设备",
                  "加湿器": "香薰加湿", "键盘": "键盘鼠标", "鼠标": "键盘鼠标", "数据线": "数据线与配件", "线材": "数据线与配件",
                  "收纳包": "收纳包",
                  "保温杯": "保温杯"}

# 先列规格层面匹配，再列部分/接口层面匹配，最后列不匹配；截断时保留最有用的行。
LEVEL_RANK = ("CASE WHEN level STARTS WITH '规格层面匹配' THEN 0 "
              "WHEN level IS NULL OR level CONTAINS '匹配' THEN 1 ELSE 2 END")
# 在线只取 MAX_ROWS + 1 行（多出的一行用于判断是否截断）；离线演示与召回评测用默认上限取全量。
LIMIT = "\nLIMIT $limit"
FULL_LIMIT = 1000

Q_PAIR = """MATCH (a:Ecom)-[r:CAN_POWER|NOT_COMPATIBLE]->(b:Ecom)
WHERE a.id IN $ids AND b.id IN $ids
RETURN a.id AS src_id, a.name AS src, type(r) AS rel, r.level AS level, r.detail AS detail, r.cable AS cable,
       b.id AS dst_id, b.name AS dst
ORDER BY """ + LEVEL_RANK + ", src_id, dst_id" + LIMIT
Q_SOURCES = """MATCH (a:Product)-[r:CAN_POWER|NOT_COMPATIBLE]->(b:Ecom {id: $dst})
RETURN a.id AS src_id, a.name AS src, type(r) AS rel, r.level AS level, r.detail AS detail, b.id AS dst_id
ORDER BY """ + LEVEL_RANK + ", src_id" + LIMIT
Q_PACKAGE = """MATCH (p:Ecom)-[r:INCLUDES|NOT_INCLUDES]->(i:Item)
WHERE p.id IN $ids AND i.kind IN ['adapter', 'cable', 'audio_cable', 'battery']
RETURN p.id AS sku_id, p.name AS product, type(r) AS rel, i.name AS item, r.cable_end AS cable_end
ORDER BY sku_id, rel, item""" + LIMIT
Q_LAPTOP = """MATCH (s:Product)-[r1:CAN_POWER]->(e:External {id: $ext})<-[r2:CABLE_SUPPORTS]-(c:Product)
RETURN s.id AS src_id, s.name AS source, c.id AS cable_id, c.name AS cable, r1.detail AS source_detail,
       r2.detail AS cable_detail, e.id AS ext_id
ORDER BY src_id, cable_id""" + LIMIT
Q_TO_EXTERNAL = """MATCH (a:Ecom)-[r]->(e:External {id: $ext})
WHERE a.id IN $ids
RETURN a.id AS src_id, a.name AS src, type(r) AS rel, r.level AS level, r.detail AS detail, e.id AS ext_id
ORDER BY """ + LEVEL_RANK + ", src_id" + LIMIT
Q_HUB = """MATCH (s:Product)-[:CAN_POWER]->(e:External {id: $ext})<-[:CABLE_SUPPORTS]-(c:Product),
      (h:Product {id: 'lianshu-h1'})-[p:PASSTHROUGH|PASSTHROUGH_INSUFFICIENT]->(e)
RETURN s.id AS src_id, s.name AS source, c.id AS cable_id, c.name AS cable, h.id AS hub_id, type(p) AS rel,
       p.detail AS hub_detail, e.id AS ext_id
ORDER BY src_id, cable_id""" + LIMIT
Q_HUB_ONLY = """MATCH (h:Product {id: 'lianshu-h1'})-[p]->(e:External {id: $ext})
RETURN h.id AS hub_id, type(p) AS rel, p.level AS level, p.detail AS detail, e.id AS ext_id
ORDER BY """ + LEVEL_RANK + ", rel" + LIMIT
Q_MULTI = """MATCH (s:Product)
WHERE s.id IN $ids AND s.c_out_ports IS NOT NULL
OPTIONAL MATCH (s)-[r:CAN_POWER]->(d:Ecom) WHERE d.id IN $ids
RETURN s.id AS src_id, s.name AS source, s.c_out_ports AS c_out_ports, s.combined_max_w AS combined_max_w,
       s.two_c_devices AS two_devices, s.three_c_devices AS three_devices,
       collect({dst_id: d.id, level: r.level, detail: r.detail}) AS devices
ORDER BY src_id""" + LIMIT
Q_MULTI_ALL = """MATCH (s:Product) WHERE s.c_out_ports IS NOT NULL
RETURN s.id AS src_id, s.name AS source, s.c_out_ports AS c_out_ports, s.combined_max_w AS combined_max_w,
       s.two_c_devices AS two_devices, s.three_c_devices AS three_devices
ORDER BY src_id""" + LIMIT
Q_SIGNAL = """MATCH (a:Ecom)-[r:CONNECTS_TO|NOT_COMPATIBLE|POWERS]-(b:Ecom)
WHERE a.id IN $ids AND r.rule IN ['R7_signal', 'R8_battery']
RETURN startNode(r).id AS src_id, startNode(r).name AS src, type(r) AS rel, r.level AS level, r.detail AS detail,
       endNode(r).id AS dst_id, endNode(r).name AS dst
ORDER BY """ + LEVEL_RANK + ", src_id, dst_id" + LIMIT
Q_SIGNAL_2HOP = """MATCH (a:Ecom)-[:CONNECTS_TO]-(b:Ecom)-[r:CONNECTS_TO]-(c:Product)
WHERE a.id IN $ids AND c.category IN $categories AND NOT c.id IN $ids
RETURN a.id AS src_id, b.id AS via_id, b.name AS via, type(r) AS rel, r.level AS level, r.detail AS detail,
       c.id AS dst_id, c.name AS dst
ORDER BY """ + LEVEL_RANK + ", dst_id, via_id" + LIMIT
Q_INTERFACES = """MATCH (p:Product)-[:HAS_INTERFACE]->(i:Interface)
WHERE p.id IN $ids
RETURN p.id AS sku_id, p.name AS product, i.kind AS kind, i.connector AS connector, i.direction AS direction,
       i.port_count AS ports, i.spec AS spec, i.note AS note
ORDER BY sku_id, kind""" + LIMIT
Q_SERIES_INTERFACES = """MATCH (p:Product)-[:HAS_INTERFACE]->(i:Interface)
WHERE p.series = $series AND i.kind IN ['audio', 'wireless', 'video', 'data']
RETURN p.id AS sku_id, p.name AS product, i.kind AS kind, i.connector AS connector, i.direction AS direction,
       i.spec AS spec
ORDER BY sku_id""" + LIMIT
Q_PACKAGE_FILTER = """MATCH (p:Product)-[r:INCLUDES|NOT_INCLUDES]->(i:Item)
WHERE i.kind = $kind AND ($series IS NULL OR p.series = $series)
RETURN p.id AS sku_id, p.name AS product, type(r) AS rel, i.name AS item
ORDER BY sku_id""" + LIMIT
Q_INCOMPATIBLE = """MATCH (s:Product)-[r:NOT_COMPATIBLE]->(d:Product)
RETURN d.id AS dst_id, d.name AS product, r.detail AS detail, collect(s.id) AS src_ids
ORDER BY dst_id""" + LIMIT
Q_NO_CHARGER = """MATCH (p:Product)-[:HAS_PORT]->(port:Port)
WHERE port.role IN ['host_powered', 'battery'] AND ($series IS NULL OR p.series = $series)
RETURN p.id AS sku_id, p.name AS product, port.role AS role, port.note AS note
ORDER BY sku_id""" + LIMIT
Q_CABLES = """MATCH (p:Product)-[:HAS_PORT]->(port:Port {role: 'cable'})
WHERE port.power_w IS NOT NULL
RETURN p.id AS sku_id, p.name AS product, port.power_w AS power_w, port.note AS note
ORDER BY power_w DESC, sku_id""" + LIMIT
Q_PATH = """MATCH p = shortestPath((x:Ecom)-[:CONNECTS_TO*..4]-(y:Ecom))
WHERE x.id IN $ids AND y.id IN $ids AND x.id < y.id
RETURN [n IN nodes(p) | n.id] AS path_ids, [n IN nodes(p) | n.name] AS path
ORDER BY size(path_ids)""" + LIMIT
Q_HOLD = """MATCH (b:Product)-[r:CAN_HOLD]->(x:Ecom)
WHERE b.id IN $ids OR x.id IN $ids OR ($bags_only AND b.category = '收纳包' AND x.category IN $categories)
RETURN b.id AS bag_id, b.name AS bag, x.id AS item_id, x.name AS item, r.level AS level, r.basis AS basis,
       r.note AS note
ORDER BY """ + LEVEL_RANK + ", bag_id, item_id" + LIMIT
Q_SCENE = """MATCH (s:Scene)-[f:FEATURES {origin: 'doc'}]->(p:Product)
WHERE s.id IN $scenes
RETURN s.name AS scene, p.id AS sku_id, p.name AS product, f.source AS source
ORDER BY scene, sku_id""" + LIMIT
Q_SCENE_POWER = """MATCH (s:Scene)-[:FEATURES {origin: 'doc'}]->(a:Product)-[r:CAN_POWER]->(b:Product)
      <-[:FEATURES {origin: 'doc'}]-(s)
WHERE s.id IN $scenes
RETURN s.name AS scene, a.id AS src_id, a.name AS src, r.level AS level, r.detail AS detail, r.cable AS cable,
       b.id AS dst_id, b.name AS dst
ORDER BY """ + LEVEL_RANK + ", src_id, dst_id" + LIMIT
Q_SCENE_SOURCE = """MATCH (s:Scene)-[:FEATURES {origin: 'doc'}]->(d:Product)<-[r:CAN_POWER|NOT_COMPATIBLE]-(a:Product)
WHERE s.id IN $scenes AND a.id IN $ids AND (size($categories) = 0 OR d.category IN $categories)
RETURN s.name AS scene, a.id AS src_id, a.name AS src, type(r) AS rel, r.level AS level, r.detail AS detail,
       r.cable AS cable, d.id AS dst_id, d.name AS dst
ORDER BY """ + LEVEL_RANK + ", dst_id, src_id" + LIMIT
Q_CATEGORY_SOURCES = """MATCH (c:Category)<-[:IN_CATEGORY]-(d:Product)<-[r:CAN_POWER|NOT_COMPATIBLE]-(s:Product)
WHERE c.id IN $categories AND ($series IS NULL OR d.series = $series)
RETURN d.id AS dst_id, d.name AS product, type(r) AS rel, r.level AS level, collect(s.id) AS src_ids
ORDER BY """ + LEVEL_RANK + ", dst_id" + LIMIT
Q_NEIGHBORS = """MATCH (a:Ecom)-[r]-(b:Ecom)
WHERE a.id IN $ids AND NOT type(r) IN ['HAS_PORT', 'HAS_INTERFACE', 'DESCRIBES', 'IN_SERIES', 'IN_CATEGORY']
RETURN a.id AS src_id, type(r) AS rel, r.level AS level, b.id AS dst_id, b.name AS dst LIMIT 40"""


def build_aliases(products):
    """商品别名：全名、「系列 型号」、去空格写法和 SKU；单独型号易歧义（如 L1），不作别名。"""
    aliases = {}
    for sku, row in products.items():
        parts = row["name"].split(" ")
        series_model = " ".join(parts[:2])
        for alias in {row["name"], series_model, series_model.replace(" ", ""), sku}:
            aliases[alias] = sku
    return dict(sorted(aliases.items(), key=lambda item: -len(item[0])))


def link_entities(text, products, aliases=None):
    aliases = build_aliases(products) if aliases is None else aliases
    found, taken = [], [False] * len(text)
    for alias, sku in aliases.items():
        for match in re.finditer(re.escape(alias), text):
            span = range(match.start(), match.end())
            if any(taken[i] for i in span):
                continue
            for i in span:
                taken[i] = True
            found.append((match.start(), sku))
    # 「系列 + 后续型号」：如「栖光 L1 和 L2」中的 L2 继承前面的系列。
    for match in re.finditer(r"([一-鿿]{2})\s?([A-Z]\d)(?:[、和与及,，\s]+([A-Z]\d))+", text):
        series = match.group(1)
        for model in re.findall(r"[A-Z]\d", match.group(0))[1:]:
            sku = aliases.get(f"{series} {model}")
            if sku:
                found.append((match.start(), sku))
    for match in re.finditer(r"(\d+)\s*瓦", text):
        ext = f"ext-laptop-{match.group(1)}w"
        if "电脑" in text or "笔记本" in text:
            if any(e[0] == ext for e in EXTERNAL):
                found.append((match.start(), ext))
    for pattern, ext in EXTERNAL_PATTERNS:
        if ext == "ext-monitor-c-video" and "HDMI" in text:
            continue
        match = re.search(pattern, text)
        if match:
            found.append((match.start(), ext))
    entities = list(dict.fromkeys(sku for _, sku in sorted(found)))
    scenes = [f"scene:{sid}" for sid, words in SCENE_WORDS.items() if any(word in text for word in words)]
    series = next((row["series"] for row in products.values() if row["series"] in text), None)
    categories = list(dict.fromkeys(f"category:{value}" for word, value in CATEGORY_WORDS.items() if word in text))
    return {"entities": entities, "scenes": scenes, "series": series, "categories": categories}


def power_relation(text, linked):
    """供电类关键词还需要另一端：第二个实体、供电商品，或「店里哪款能用」这类找供电端的问法。

    「听岚 S1 的充电盒防水吗」只是商品自身属性，不查图谱。
    """
    entities = linked["entities"]
    if not entities:
        return "哪" in text  # 「哪些商品不需要充电器」这类汇总问法
    if len(entities) >= 2 or any(e.startswith("hengdian-") for e in entities):
        return True
    return "category:充电设备" in linked["categories"] or bool(re.search(SOURCE_CUES, text))


def detect_intent(text, linked):
    entities = linked["entities"]
    if any(e.startswith("ext-laptop-") for e in entities):
        return "hub" if "lianshu-h1" in entities else "laptop"
    power_words = False
    for intent, pattern in INTENT_WORDS:
        if re.search(pattern, text):
            if intent == "power" and linked["scenes"] and not entities:
                return "scene_power"
            if intent == "power" and not power_relation(text, linked):
                power_words = True
                continue
            return intent
    if linked["scenes"]:
        # 「办公桌面搭配里的台灯能和衡电 A1 一起用吗」：场景里的商品与指定供电商品的关系。
        return "scene_source" if any(e.startswith("hengdian-") for e in entities) else "scene"
    if len(entities) >= 2 and any(e.startswith("hengdian-") for e in entities):
        # 「衡电 A2 配栖光 L5 行不行」这类没有充电字样的问法，按供电关系处理。
        return "power"
    if power_words and entities and re.search(PACKAGE_WORDS, text):
        # 「买栖光 L1 会送电源适配器吗」：只查包装含/不含，不查谁能给它供电。
        return "package"
    return "neighbors"


def plan_queries(text, products, aliases=None, limit=FULL_LIMIT):
    """返回 (链接结果, 意图, [(说明, Cypher, 参数)])；纯函数，不访问数据库。"""
    linked = link_entities(text, products, aliases)
    intent = detect_intent(text, linked)
    ids, scenes = linked["entities"], linked["scenes"]
    product_ids = [e for e in ids if e in products]
    laptop = next((e for e in ids if e.startswith("ext-laptop-")), None)
    plans = []
    if intent in {"laptop", "hub"}:
        plans.append(("可行组合：供电端与线材都满足功率", Q_LAPTOP, {"ext": laptop}))
        if product_ids:
            plans.append(("所提商品与该电脑的关系", Q_TO_EXTERNAL, {"ext": laptop, "ids": product_ids}))
        if intent == "hub":
            plans.append(("扩展器直通", Q_HUB_ONLY, {"ext": laptop}))
            plans.append(("经扩展器的四跳链路", Q_HUB, {"ext": laptop}))
    elif intent == "multi":
        if any(products.get(e, {}).get("category") == "充电设备" for e in product_ids):
            plans.append(("供电商品口数与对各设备的匹配", Q_MULTI, {"ids": product_ids}))
        else:
            plans.append(("全部供电商品的口数", Q_MULTI_ALL, {}))
    elif intent == "storage":
        plans.append(("收纳适配", Q_HOLD, {"ids": product_ids, "bags_only": not any(
            products.get(e, {}).get("category") == "收纳包" for e in product_ids),
            "categories": [c.split(":", 1)[1] for c in linked["categories"]]}))
    elif intent == "signal":
        if ids:
            plans.append(("信号连接关系", Q_SIGNAL, {"ids": ids}))
        if product_ids:
            plans.append(("接口明细", Q_INTERFACES, {"ids": product_ids}))
        if not product_ids and linked["series"]:
            plans.append(("系列商品的接口", Q_SERIES_INTERFACES, {"series": linked["series"]}))
        if ids and linked["categories"]:
            # 「C 口电脑接 HDMI 显示器和 A 口鼠标」：显示器 → H1 → 鼠标需要两跳。
            plans.append(("两跳连接到所提品类", Q_SIGNAL_2HOP,
                          {"ids": ids, "categories": [c.split(":", 1)[1] for c in linked["categories"]]}))
        if len(ids) >= 2:
            plans.append(("实体之间的连接路径", Q_PATH, {"ids": ids}))
    elif intent == "power":
        sources = [e for e in product_ids if products[e]["category"] == "充电设备"]
        sinks = [e for e in product_ids if e not in sources] or sources[1:]
        if sources and sinks:
            plans.append(("指定供电商品与受电商品", Q_PAIR, {"ids": product_ids}))
        elif sinks:
            plans.append(("能给该商品供电的店内商品", Q_SOURCES, {"dst": sinks[0]}))
        elif re.search(r"包装|自带|附带", text):
            # 同时返回含与不含，便于对照「哪些带、哪些不带」。
            kind = "adapter" if "适配器" in text else "cable"
            plans.append(("按包装物品汇总", Q_PACKAGE_FILTER, {"kind": kind, "series": linked["series"]}))
        elif re.search(r"不需要|不用", text):
            plans.append(("不需要充电的商品（主机供电或干电池）", Q_NO_CHARGER, {"series": linked["series"]}))
            if linked["categories"]:
                plans.append(("按品类汇总供电关系", Q_CATEGORY_SOURCES,
                              {"categories": linked["categories"], "series": linked["series"]}))
        elif re.search(r"不能|无法|没法|不匹配", text):
            plans.append(("店内供电商品都不匹配的商品", Q_INCOMPATIBLE, {}))
        elif re.search(r"线", text) and re.search(r"\d+\s*瓦", text):
            plans.append(("线材供电能力", Q_CABLES, {}))
        elif linked["categories"]:
            plans.append(("按品类汇总供电关系", Q_CATEGORY_SOURCES,
                          {"categories": linked["categories"], "series": linked["series"]}))
        if product_ids:
            plans.append(("包装内的适配器与线材", Q_PACKAGE, {"ids": product_ids}))
    elif intent == "package" and product_ids:
        plans.append(("包装内的适配器与线材", Q_PACKAGE, {"ids": product_ids}))
    elif intent == "scene_power":
        plans.append(("场景内商品之间的供电关系", Q_SCENE_POWER, {"scenes": scenes}))
    elif intent == "scene_source":
        plans.append(("指定供电商品与场景内商品", Q_SCENE_SOURCE,
                      {"scenes": scenes, "ids": product_ids,
                       "categories": [c.split(":", 1)[1] for c in linked["categories"]]}))
    if scenes and intent not in {"scene_power", "scene_source"}:
        plans.append(("场景商品（来自场景文档）", Q_SCENE, {"scenes": scenes}))
        if intent == "power":
            plans.append(("场景内商品之间的供电关系", Q_SCENE_POWER, {"scenes": scenes}))
    if not plans and ids:
        plans.append(("一跳邻居", Q_NEIGHBORS, {"ids": ids}))
    return linked, intent, [(title, query, {**params, "limit": limit}) for title, query, params in plans]


def result_ids(rows):
    found = set()
    for row in rows:
        for key, value in row.items():
            if key.endswith("_id") and isinstance(value, str):
                found.add(value)
            elif key.endswith("_ids") and isinstance(value, list):
                found.update(v for v in value if isinstance(v, str))
            elif key == "devices" and isinstance(value, list):
                found.update(v["dst_id"] for v in value if isinstance(v, dict) and v.get("dst_id"))
    return found
