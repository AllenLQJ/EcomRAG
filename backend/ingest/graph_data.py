"""python -m ingest.graph_data：从商品表和场景文档生成 data/graph 图数据与多跳评测集。

端口规格、外部设备、收纳和场景为人工整理，写在本文件；兼容关系由规则推导。
所有兼容结论只代表「规格层面匹配」，不代表实际一定可用。默认只预览差异，--yes 才写文件。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

from ingest.loaders import resolve_data_path

DEFAULT_OUT = "data/graph"
SOURCES = ["hengdian-a1", "hengdian-a2", "hengdian-a3", "hengdian-p1", "hengdian-p2", "hengdian-p3"]
CABLES = ["lianshu-c1", "lianshu-c2", "lianshu-c3"]
DC_SINKS = {"qiguang-l4", "qiguang-l6", "muxi-a1"}
LEVEL_SPEC = "规格层面匹配"
LEVEL_IFACE = "接口层面匹配（受电需求未注明）"
LEVEL_PARTIAL = "规格层面部分匹配（输出低于受电端输入上限）"
CAVEAT = "兼容结论仅为规格层面判断，实际能否使用还取决于供电端、线材和受电设备的协商，请以商品页参数为准。"

# ---------- 人工整理：外部设备（店内不售，用于演示约束推理） ----------
EXTERNAL = [
    ("ext-laptop-45w", "需 45 瓦 C 口供电的轻薄电脑", "USB-C", 45, "示例设备，店内不售"),
    ("ext-laptop-65w", "需 65 瓦 C 口供电的轻薄电脑", "USB-C", 65, "示例设备，店内不售"),
    ("ext-laptop-100w", "需 100 瓦 C 口供电的电脑", "USB-C", 100, "示例设备，店内不售"),
    ("ext-monitor-hdmi", "HDMI 接口显示器", "HDMI", "", "店内不售"),
    ("ext-monitor-c-video", "支持 C 口视频输入的显示器", "USB-C", "", "店内不售"),
    ("ext-wired-earphone", "3.5 毫米有线耳机", "3.5mm", "", "店内不售"),
    ("ext-audio-cable", "3.5 毫米音频线", "3.5mm~3.5mm", "", "店内不售；鸣澜音箱均不含"),
    ("ext-c-a-cable", "C 转 A 线", "USB-C~USB-A", "", "店内不售"),
    ("ext-computer-c", "带 C 口的电脑", "USB-C", "", "店内不售"),
    ("ext-computer-a", "带 A 口的电脑", "USB-A", "", "店内不售"),
    ("ext-phone-c", "仅有 C 口的手机", "USB-C", "", "店内不售；受电需求因机型而异"),
    ("ext-aa-battery", "AA 电池", "AA电池", "", "店内不售；键行 M1 不随附"),
]

# ---------- 人工整理：场景 ----------
# doc 来自 data/docs 场景文档；constructed 为扩展构造，仅供图谱演示，不作为店铺推荐结论。
DOC_SCENES = {
    "commuting": ("通勤", "product_scene_commuting.md", ["tinglan-n2", "muche-c2", "najing-p1", "hengdian-p1"]),
    "business_travel": ("出差旅行", "product_scene_business_travel.md",
                        ["hengdian-p3", "lianshu-c2", "jianxing-m1", "najing-t1"]),
    "dormitory": ("宿舍", "product_scene_dormitory.md", ["qiguang-l3", "tinglan-q1", "muche-c4", "najing-p2"]),
    "gift_selection": ("送礼", "product_scene_gift_selection.md", ["qiguang-l5", "muxi-a1", "muche-c5", "najing-p2"]),
    "home_reading": ("居家阅读", "product_scene_home_reading.md", ["qiguang-l6", "muche-c1", "minglan-a1"]),
    "office_desktop": ("办公桌面", "product_scene_office_desktop.md",
                       ["qiguang-l2", "jianxing-k2", "jianxing-m2", "lianshu-h1"]),
    "short_outdoor_trips": ("户外短途", "product_scene_short_outdoor_trips.md",
                            ["tinglan-b1", "muche-c6", "minglan-p2"]),
    "sports_fitness": ("运动健身", "product_scene_sports_fitness.md", ["tinglan-k1", "muche-c3", "najing-p1"]),
    "starter_setup": ("入门配置", "product_scene_starter_setup.md", ["tinglan-s1", "hengdian-a1", "lianshu-c1"]),
    "student_study": ("学生学习", "product_scene_student_study.md",
                      ["qiguang-l1", "jianxing-k1", "jianxing-m3", "lianshu-c1"]),
}
CONSTRUCTED_SCENES = {
    "commuting": ["tinglan-s2", "tinglan-c2", "muche-c1"],
    "business_travel": ["hengdian-a3", "lianshu-h1", "najing-p2", "tinglan-m1"],
    "dormitory": ["muxi-h1", "minglan-p1", "hengdian-a2"],
    "home_reading": ["qiguang-l1", "muxi-h2"],
    "office_desktop": ["jianxing-k3", "lianshu-c3", "muche-c1", "hengdian-a3"],
    "short_outdoor_trips": ["minglan-r1", "hengdian-p2", "najing-t1"],
    "sports_fitness": ["tinglan-b1"],
    "student_study": ["qiguang-l3", "hengdian-a1", "muche-c2"],
    "home_audio": ["minglan-d1", "minglan-d2", "lianshu-a1"],
    "bedside_rest": ["minglan-a1", "muxi-h1", "qiguang-l5"],
    "remote_meeting": ["tinglan-m1", "tinglan-s2", "lianshu-h1", "lianshu-c3"],
    "drawing_reading": ["qiguang-l6", "qiguang-l4", "muxi-a1"],
}
EXTRA_SCENE_LABELS = {"home_audio": "客厅影音", "bedside_rest": "卧室床边", "remote_meeting": "远程会议",
                      "drawing_reading": "绘画与阅读"}

PACKAGE_KINDS = [(r"适配器", "adapter"), (r"转接头", "special_adapter"), (r"音频线", "audio_cable"),
                 (r"充电线|供电线|C 型线|数据线$", "cable"), (r"接收器", "receiver"), (r"AA 电池", "battery"),
                 (r"说明书|说明卡", "manual")]
CABLE_ENDS = {"C 型充电线": "USB-C~未注明", "C 型线": "USB-C~未注明"}


def load_products(path="data/products/products.csv"):
    with resolve_data_path(path).open(encoding="utf-8-sig", newline="") as handle:
        return {row["sku_id"]: row for row in csv.DictReader(handle)}


def build_tables(products):
    """返回 {文件名: 行列表}；行值保持写入 CSV 前的原始类型。"""
    specs = {sku: json.loads(row["specs"]) for sku, row in products.items()}
    power, interfaces = [], []

    def port(sku, role, port_id, connector, direction, count=1, v="", a="", w="", pd="", combined="",
             field="", note=""):
        power.append(dict(sku_id=sku, name=products[sku]["name"], category=products[sku]["category"], role=role,
                          port_id=port_id, connector=connector, direction=direction, port_count=count,
                          voltage_v=v, current_a=a, power_w=w, pd=pd, combined_max_w=combined,
                          spec_field=field, spec_value=specs[sku][field] if field else "", note=note))

    def iface(sku, kind, connector, direction, count=1, spec="", field="", note=""):
        interfaces.append(dict(sku_id=sku, name=products[sku]["name"], kind=kind, connector=connector,
                               direction=direction, port_count=count, spec=spec, spec_field=field,
                               spec_value=specs[sku].get(field, ""), note=note))

    # 供电端口；spec_field 指向原文字段，便于人工核对。
    for sku in ["tinglan-s1", "tinglan-s2", "tinglan-m1", "tinglan-k1", "tinglan-b1", "tinglan-n2",
                "tinglan-c2", "tinglan-q1"]:
        port(sku, "sink", "charge_in", "USB-C", "in", field="充电接口", note="受电电压与电流未注明")
    for sku, v, a, conn in [("qiguang-l1", 5, 2, "USB-C"), ("qiguang-l2", 5, 2, "USB-C"),
                            ("qiguang-l3", 5, 1, "USB-C"), ("qiguang-l4", 12, 1.5, "DC圆孔"),
                            ("qiguang-l5", 5, 1.5, "USB-C"), ("qiguang-l6", 12, 1.5, "DC圆孔"),
                            ("muxi-h1", 5, 1, "USB-C"), ("muxi-h2", 5, 2, "USB-C"), ("muxi-a1", 12, 1, "DC圆孔")]:
        note = "包装含匹配适配器" if conn == "DC圆孔" else ("内置电池，供电线同时用于充电" if sku == "qiguang-l3" else "")
        port(sku, "sink", "power_in", conn, "in", v=v, a=a, w=round(v * a, 2), field="输入要求", note=note)
    for sku in ["minglan-p1", "minglan-p2", "minglan-d1", "minglan-d2", "minglan-r1", "minglan-a1"]:
        port(sku, "sink", "charge_in", "USB-C", "in", v=5, a=2, w=10, field="充电输入")
    port("jianxing-k1", "host_powered", "wired", "USB-C", "in", v=5, a=0.5, w=2.5, field="供电方式",
         note="有线连接主机供电，无电池")
    port("jianxing-k3", "host_powered", "wired", "USB-C", "in", v=5, a=0.5, w=2.5, field="供电方式",
         note="有线连接主机供电，无电池")
    port("jianxing-m3", "host_powered", "wired", "USB-A", "in", v=5, a=0.1, w=0.5, field="供电方式",
         note="A 型有线直连主机，无电池")
    port("jianxing-k2", "sink", "charge_in", "USB-C", "in", v=5, a=1, w=5, field="供电方式", note="内置 1500 毫安时电池")
    port("jianxing-m2", "sink", "charge_in", "USB-C", "in", v=5, a=0.5, w=2.5, field="供电方式", note="内置 500 毫安时电池")
    port("jianxing-m1", "battery", "battery", "AA电池", "in", count=1, field="供电方式",
         note="可更换 AA 电池 1 节，不支持充电；电池不随附")
    port("hengdian-a1", "source", "c_out", "USB-C", "out", 1, v="5;9", a="3;2.22", w=20, pd="是", combined=20,
         field="输出规格", note="唯一明示 5 伏档电流（3 安）的供电商品")
    port("hengdian-a2", "source", "c_out", "USB-C", "out", 2, w=35, pd="是", combined=35,
         field="输出规格", note="双口同时使用时为 20 瓦与 15 瓦")
    port("hengdian-a3", "source", "c_out", "USB-C", "out", 2, w=100, pd="是", combined=100,
         field="输出规格", note="三口同时使用时为 65 瓦、20 瓦与 15 瓦")
    port("hengdian-a3", "source", "a_out", "USB-A", "out", 1, pd="否", combined=100,
         field="协议支持", note="A 型口单口上限未单列，不提供 PD")
    for sku, c_w, a_n, comb in [("hengdian-p1", 20, 1, 15), ("hengdian-p2", 22.5, 2, 15)]:
        port(sku, "source_sink", "c_io", "USB-C", "bidir", 1, w=c_w, pd="是", combined=comb, field="输出规格",
             note="C 型口兼作输入")
        port(sku, "source_sink", "a_out", "USB-A", "out", a_n, pd="否", combined=comb, field="协议支持",
             note="A 型口单口上限未单列，不提供 PD")
        port(sku, "source_sink", "recharge_in", "USB-C", "in", v="5;9", a="3;2", w=18, field="输入要求",
             note="本体补电输入，按 9 伏 2 安计 18 瓦")
    port("hengdian-p3", "source_sink", "c_io", "USB-C", "bidir", 1, w=65, pd="是", combined=65, field="输出规格",
         note="C 型口兼作输入；双口同时使用时为 45 瓦与 20 瓦")
    port("hengdian-p3", "source_sink", "c_out", "USB-C", "out", 1, w=65, pd="是", combined=65, field="输出规格",
         note="双口同时使用时为 45 瓦与 20 瓦")
    port("hengdian-p3", "source_sink", "recharge_in", "USB-C", "in", v="5-20", w=45, field="输入要求",
         note="本体补电输入上限")
    port("lianshu-c1", "cable", "cable", "USB-C~USB-C", "bidir", v=20, a=3, w=60, field="供电能力")
    port("lianshu-c2", "cable", "cable", "USB-C~USB-C", "bidir", v=20, a=5, w=100, field="供电能力", note="带电子标记芯片")
    port("lianshu-c3", "cable", "cable", "USB-C~USB-C", "bidir", v=20, a=5, w=100, field="供电能力", note="带电子标记芯片")
    port("lianshu-h1", "hub", "pd_in", "USB-C", "in", w=100, field="供电能力", note="C 型供电输入最高 100 瓦")
    port("lianshu-h1", "hub", "host_out", "USB-C", "out", w=85, field="供电能力", note="经上行口向主机供电最高 85 瓦")
    port("lianshu-a1", "cable", "adapter", "USB-C~3.5mm", "bidir", field="供电能力", note="主机供电，不提供充电输出")

    # 数据、音频、视频接口
    iface("lianshu-c1", "data", "USB-C~USB-C", "bidir", spec="USB 2.0 480Mbps", field="传输规格", note="不支持视频")
    iface("lianshu-c2", "data", "USB-C~USB-C", "bidir", spec="USB 2.0 480Mbps", field="传输规格", note="不支持视频")
    iface("lianshu-c3", "data", "USB-C~USB-C", "bidir", spec="USB 3.2 Gen 2 10Gbps", field="传输规格")
    iface("lianshu-c3", "video", "USB-C~USB-C", "bidir", spec="4K 60Hz", field="传输规格", note="仅兼容设备")
    iface("lianshu-h1", "data", "USB-C", "upstream", spec="接主机", field="连接接口")
    iface("lianshu-h1", "data", "USB-A", "downstream", 2, spec="共享 5Gbps", field="传输规格")
    iface("lianshu-h1", "video", "HDMI", "out", spec="4K 30Hz", field="传输规格")
    iface("lianshu-a1", "audio", "USB-C", "upstream", spec="接 C 型设备", field="连接接口")
    iface("lianshu-a1", "audio", "3.5mm", "out", spec="24bit/96kHz 解码", field="传输规格", note="不支持视频")
    for sku in ["minglan-d1", "minglan-d2"]:
        iface(sku, "audio", "3.5mm", "in", spec="3.5 毫米音频输入", field="连接方式", note="不含音频线")
    for sku in ["minglan-p1", "minglan-p2", "minglan-d1", "minglan-d2", "minglan-r1", "minglan-a1"]:
        iface(sku, "wireless", "蓝牙5.3", "in", spec="蓝牙 5.3", field="连接方式")
    iface("jianxing-k1", "data", "USB-C", "upstream", spec="C 型有线", field="连接方式")
    iface("jianxing-k3", "data", "USB-C", "upstream", spec="C 型有线", field="连接方式")
    iface("jianxing-m3", "data", "USB-A", "upstream", spec="A 型有线", field="连接方式")
    iface("jianxing-k2", "wireless", "蓝牙;2.4GHz", "upstream", spec="蓝牙、2.4GHz 无线", field="连接方式",
          note="附无线接收器，接收器接口未注明")
    iface("jianxing-k2", "data", "USB-C", "upstream", spec="C 型有线", field="连接方式")
    for sku in ["jianxing-m1", "jianxing-m2"]:
        iface(sku, "wireless", "蓝牙;2.4GHz", "upstream", spec="蓝牙、2.4GHz 无线", field="连接方式",
              note="附无线接收器，接收器接口未注明")

    package = build_package_items(products)
    external = [dict(ext_id=i, name=n, connector=c, need_w=w, note=note) for i, n, c, w, note in EXTERNAL]
    storage = build_storage_fit()
    scenes = build_scenes(products)
    edges, multi = derive_edges(power, package, external)
    return {"power_specs.csv": power, "interfaces.csv": interfaces, "package_items.csv": package,
            "external_devices.csv": external, "storage_fit.csv": storage, "scenes.csv": scenes,
            "derived_edges.csv": edges, "multi_device.csv": multi}


def split_items(text):
    text = re.sub(r"(及|与|和)(说明书|说明卡)", r"、\2", text)
    return [part.strip() for part in re.split(r"[、，,]", text) if part.strip()]


def build_package_items(products):
    rows = []
    for sku, row in products.items():
        for field, included in (("package_contents", "是"), ("not_included", "否")):
            for item in split_items(row[field]):
                kind = next((k for pattern, k in PACKAGE_KINDS if re.search(pattern, item)), "other")
                if item.endswith("数据线") and sku.startswith("lianshu"):
                    kind = "product_body"
                end = CABLE_ENDS.get(item, "未注明") if kind == "cable" else ""
                if sku in DC_SINKS and item == "匹配适配器":
                    end = "DC圆孔专用"
                rows.append(dict(sku_id=sku, item=item, item_kind=kind, included=included, cable_end=end, source=field))
    return rows


def build_storage_fit():
    rows = []

    def fit(bag, item, level, basis, note=""):
        rows.append(dict(bag=bag, item=item, level=level, basis=basis, note=note))

    for sku in ["tinglan-s1", "tinglan-s2", "tinglan-k1", "tinglan-n2", "tinglan-c2", "tinglan-q1"]:
        fit("najing-p1", sku, "类别匹配", "适用物品含「耳机」", "需核对耳机盒实物尺寸")
    for sku in ["tinglan-m1", "tinglan-b1"]:
        fit("najing-p1", sku, "待核对", "适用物品含「耳机」", "头戴或后挂款体积较大，外部尺寸 180×110×50 毫米不保证能装入")
    fit("najing-p1", "lianshu-c1", "类别匹配", "适用物品含「短数据线」", "C1 长 1 米")
    fit("najing-p1", "lianshu-c3", "类别匹配", "适用物品含「短数据线」", "C3 长 0.8 米")
    fit("najing-p1", "lianshu-c2", "待核对", "适用物品为「短数据线」", "C2 长 2 米，不属于短线")
    fit("najing-p1", "hengdian-a1", "类别匹配", "适用物品含「小型充电器」", "单口 20 瓦基础充电器")
    for sku in ["hengdian-a2", "hengdian-a3"]:
        fit("najing-p1", sku, "待核对", "适用物品为「小型充电器」", "多口充电器尺寸未注明")
    for sku in ["hengdian-p1", "hengdian-p2", "hengdian-p3"]:
        fit("najing-p2", sku, "类别匹配", "适用物品含「充电宝」")
    for sku in ["lianshu-c1", "lianshu-c2", "lianshu-c3", "lianshu-a1", "lianshu-h1"]:
        fit("najing-p2", sku, "类别匹配", "适用物品含「线材」")
    for sku in ["hengdian-a1", "hengdian-a2", "hengdian-a3"]:
        fit("najing-p2", sku, "类别匹配", "适用物品含「适配器」")
    for ext in ["ext-laptop-45w", "ext-laptop-65w"]:
        fit("najing-l1", ext, "尺寸条件", "机身不超过 320×225×18 毫米", "需按电脑实际尺寸核对")
    fit("najing-t1", "折叠衣物", "类别匹配", "适用物品含「折叠衣物」")
    fit("najing-t1", "独立包装小物", "类别匹配", "适用物品含「独立包装小物」")
    return rows


def build_scenes(products):
    rows = []
    labels = {sid: value[0] for sid, value in DOC_SCENES.items()}
    labels.update(EXTRA_SCENE_LABELS)
    for sid, (label, doc, skus) in DOC_SCENES.items():
        for sku in skus:
            rows.append(dict(scene_id=sid, scene=label, sku_id=sku, origin="doc", source=doc,
                             reason=products[sku]["positioning"]))
    for sid, skus in CONSTRUCTED_SCENES.items():
        for sku in skus:
            rows.append(dict(scene_id=sid, scene=labels[sid], sku_id=sku, origin="constructed", source="",
                             reason=products[sku]["positioning"]))
    return rows


def derive_edges(power, package, external):
    """按 README 中 R1–R9 规则推导关系边与多设备表。"""
    cable_skus = {row["sku_id"] for row in package if row["item_kind"] == "cable" and row["included"] == "是"}
    sources = {}
    for row in power:
        # 每个供电商品取第一条 C 口输出作为代表；A 口因店内无 C 转 A 线不参与推导。
        if (row["direction"] in {"out", "bidir"} and row["role"] in {"source", "source_sink"}
                and row["connector"] == "USB-C"):
            sources.setdefault(row["sku_id"], row)
    sinks = [row for row in power
             if row["role"] == "sink" or (row["role"] == "source_sink" and row["port_id"] == "recharge_in")]
    edges = []

    def add(src, dst, rel, level, rule, detail, cable=""):
        edges.append(dict(src=src, dst=dst, rel=rel, level=level, rule=rule, detail=detail, cable=cable))

    def cable_need(src, dst):
        if dst in cable_skus:
            return "受电端自带充电线（另一端接口未注明）"
        if src in cable_skus:
            return "供电端自带 C 型充电线（另一端接口未注明）"
        return "需另备 C 转 C 线，可选联枢 C1/C2/C3"

    for sink in sinks:
        for sku, src in sources.items():
            if sku == sink["sku_id"]:
                continue
            if sink["connector"] != "USB-C":
                add(sku, sink["sku_id"], "NOT_COMPATIBLE", "接口不一致", "R3_connector_mismatch",
                    f"受电端为 {sink['connector']} {sink['voltage_v']} 伏，店内供电商品只有 C/A 口")
                continue
            if sink["power_w"] == "":
                level, rule, detail = LEVEL_IFACE, "R2_interface_only", "接口均为 C 型；受电端未注明电压电流"
            elif float(src["power_w"]) >= float(sink["power_w"]):
                level, rule = LEVEL_SPEC, "R1_power_spec"
                detail = f"C 口单口最高 {src['power_w']} 瓦 ≥ 受电 {sink['power_w']} 瓦"
            else:
                level, rule = LEVEL_PARTIAL, "R1b_power_below_input"
                detail = f"C 口单口最高 {src['power_w']} 瓦 < 受电端输入上限 {sink['power_w']} 瓦"
            add(sku, sink["sku_id"], "CAN_POWER", level, rule, detail, cable_need(sku, sink["sku_id"]))

    # 外部电脑：供电端单口功率、线材功率、扩展器直通功率分别比较。
    cables = [row for row in power if row["role"] == "cable" and row["power_w"] != ""]
    for ext in external:
        if not str(ext["need_w"]).strip():
            continue
        need = float(ext["need_w"])
        for sku, src in sources.items():
            ok = float(src["power_w"]) >= need
            add(sku, ext["ext_id"], "CAN_POWER" if ok else "NOT_COMPATIBLE", LEVEL_SPEC if ok else "功率不足",
                "R1_power_spec" if ok else "R4_power_insufficient",
                f"C 口单口最高 {src['power_w']} 瓦 {'≥' if ok else '<'} 需求 {need:g} 瓦",
                "需线材同样满足功率" if ok else "")
        for cab in cables:
            ok = float(cab["power_w"]) >= need
            add(cab["sku_id"], ext["ext_id"], "CABLE_SUPPORTS" if ok else "CABLE_INSUFFICIENT",
                LEVEL_SPEC if ok else "线材功率不足", "R5_cable_power",
                f"线材最高 {cab['power_w']} 瓦 {'≥' if ok else '<'} 需求 {need:g} 瓦")
        ok = 85 >= need
        add("lianshu-h1", ext["ext_id"], "PASSTHROUGH" if ok else "PASSTHROUGH_INSUFFICIENT",
            LEVEL_SPEC if ok else "直通功率不足", "R6_hub_passthrough",
            f"向主机供电最高 85 瓦 {'≥' if ok else '<'} 需求 {need:g} 瓦；输入端需供电 ≥ 需求且线材满足")

    for args in [
        ("jianxing-m3", "lianshu-h1", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "M3 为 A 型有线；H1 有 A 型下行口 2 个"),
        ("lianshu-h1", "ext-computer-c", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "H1 为 C 型上行"),
        ("lianshu-h1", "ext-monitor-hdmi", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "HDMI 最高 4K 30Hz"),
        ("lianshu-c3", "ext-monitor-c-video", "CONNECTS_TO", "规格层面匹配（仅兼容设备）", "R7_signal",
         "兼容设备可传输 4K 60Hz"),
        ("lianshu-c1", "ext-monitor-c-video", "NOT_COMPATIBLE", "不支持视频", "R7_signal", "USB 2.0，不支持视频输出"),
        ("lianshu-c2", "ext-monitor-c-video", "NOT_COMPATIBLE", "不支持视频", "R7_signal", "USB 2.0，不支持视频输出"),
        ("lianshu-a1", "ext-wired-earphone", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "C 型转 3.5 毫米音频口"),
        ("lianshu-a1", "ext-phone-c", "CONNECTS_TO", "规格层面匹配（仅兼容 C 型设备）", "R7_signal", "接 C 型设备，主机供电"),
        ("ext-audio-cable", "minglan-d1", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "D1 有 3.5 毫米音频输入；音频线店内不售"),
        ("ext-audio-cable", "minglan-d2", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "D2 有 3.5 毫米音频输入；音频线店内不售"),
        ("jianxing-m3", "ext-computer-a", "CONNECTS_TO", LEVEL_SPEC, "R7_signal", "A 型有线直连"),
        ("jianxing-k1", "ext-computer-c", "CONNECTS_TO", "接口层面匹配（附 C 型线，另一端未注明）", "R7_signal", "C 型有线"),
        ("jianxing-k3", "ext-computer-c", "CONNECTS_TO", "接口层面匹配（附 C 型线，另一端未注明）", "R7_signal", "C 型有线"),
        ("ext-aa-battery", "jianxing-m1", "POWERS", "规格层面匹配", "R8_battery", "可更换 AA 电池 1 节；不随附"),
    ]:
        add(*args)

    multi = []
    for sku in sorted(sources):
        ports = sum(int(row["port_count"]) for row in power if row["sku_id"] == sku and row["connector"] == "USB-C"
                    and row["direction"] in {"out", "bidir"})
        multi.append(dict(src=sku, c_out_ports=ports, combined_max_w=sources[sku]["combined_max_w"],
                          two_c_devices="是" if ports >= 2 else "否", three_c_devices="是" if ports >= 3 else "否",
                          note=sources[sku]["note"]))
    return edges, multi


def render_csv(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def parse_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


def build_eval(tables, products, doc_names):
    """tables 为解析后的字符串行，与读取已生成 CSV 的结果一致。"""
    edges, power, fits, scenes = (tables["derived_edges.csv"], tables["power_specs.csv"],
                                  tables["storage_fit.csv"], tables["scenes.csv"])
    multi = {row["src"]: row for row in tables["multi_device.csv"]}
    rows = []

    def name(sku):
        return products[sku]["name"] if sku in products else sku

    def edge(src, dst):
        return next(e for e in edges if e["src"] == src and e["dst"] == dst)

    def doc(*skus):
        return [f"product_{s}.md" for s in skus if f"product_{s}.md" in doc_names]

    def add(kind, hops, question, answer, skus, docs=None, rules=(), caveat=True, history=None):
        row = dict(id=f"graph-{len(rows) + 1:03d}")
        if history:
            row["history"] = [{"role": role, "content": content} for role, content in history]
        row.update(question=question, expected_route="answer",
                   expected_doc=docs if docs is not None else doc(*[s for s in skus if s in products]),
                   kind=kind, hops=hops, expected_skus=list(dict.fromkeys(skus)), graph_rules=list(rules),
                   reference_answer=answer + (CAVEAT if caveat and "匹配" in answer else ""))
        rows.append(row)

    # A. 单对供电匹配
    for src, dst, q in [
        ("hengdian-a1", "qiguang-l1", "衡电 A1 充电器能给栖光 L1 台灯供电吗？"),
        ("hengdian-a1", "qiguang-l2", "栖光 L2 夹式台灯可以用衡电 A1 供电吗"),
        ("hengdian-p1", "qiguang-l3", "用衡电 P1 充电宝给栖光 L3 充电台灯充电，规格上匹配吗？"),
        ("hengdian-a2", "qiguang-l5", "衡电 A2 配栖光 L5 折叠台灯行不行"),
        ("hengdian-a3", "qiguang-l4", "衡电 A3 能给栖光 L4 宽幅台灯供电吗？"),
        ("hengdian-p3", "qiguang-l6", "衡电 P3 充电宝可以给栖光 L6 绘读台灯供电吗"),
        ("hengdian-a1", "minglan-d2", "鸣澜 D2 音箱能用衡电 A1 充电吗？"),
        ("hengdian-p2", "minglan-p2", "出门用衡电 P2 给鸣澜 P2 音箱补电，规格对得上吗"),
        ("hengdian-p1", "minglan-r1", "衡电 P1 能给鸣澜 R1 充电吗"),
        ("hengdian-a2", "muxi-h2", "沐息 H2 加湿器用衡电 A2 供电可以吗？"),
        ("hengdian-a1", "muxi-h1", "沐息 H1 加湿器能接衡电 A1 吗"),
        ("hengdian-a3", "muxi-a1", "沐息 A1 香薰扩香器可以用衡电 A3 供电吗？"),
        ("hengdian-a1", "jianxing-k2", "键行 K2 无线键盘用衡电 A1 充电行吗"),
        ("hengdian-p1", "jianxing-m2", "衡电 P1 充电宝能给键行 M2 鼠标充电吗？"),
        ("hengdian-a1", "tinglan-s1", "听岚 S1 耳机能用衡电 A1 充电吗？"),
        ("hengdian-p3", "tinglan-m1", "衡电 P3 可以给听岚 M1 头戴耳机充电吗"),
        ("hengdian-a2", "tinglan-b1", "听岚 B1 骨传导耳机配衡电 A2 充电器可以吗"),
        ("hengdian-p2", "tinglan-q1", "衡电 P2 给听岚 Q1 耳机充电规格上行吗？"),
    ]:
        e = edge(src, dst)
        if e["rel"] == "CAN_POWER":
            ans = f"{e['level']}：{name(src)}的{e['detail']}，可以给{name(dst)}供电；线材方面，{e['cable']}。"
        else:
            ans = f"不匹配：{e['detail']}。"
            if dst in DC_SINKS:
                ans += f"{name(dst)}包装内已含匹配适配器，请使用随附适配器。"
        add("power_pair", 2, q, ans, [src, dst], rules=[e["rule"]])

    # B. 列出可用供电商品
    for dst, q in [("qiguang-l1", "栖光 L1 台灯不含适配器，店里哪些充电器或充电宝能给它供电？"),
                   ("minglan-d1", "鸣澜 D1 音箱可以用店里哪些衡电产品充电"),
                   ("muxi-h2", "沐息 H2 加湿器能配店里哪款供电设备？"),
                   ("qiguang-l3", "栖光 L3 充电台灯用什么给它充电？店里有哪些能用"),
                   ("jianxing-k2", "键行 K2 键盘充电可以用店里哪些产品？"),
                   ("tinglan-s2", "听岚 S2 耳机不带充电头，店里哪些充电器能用？"),
                   ("tinglan-k1", "听岚 K1 运动耳机能用店里哪些衡电充电设备充电"),
                   ("minglan-p1", "鸣澜 P1 音箱配什么充电器？店内有哪些可选")]:
        first = edge(SOURCES[0], dst)
        ans = f"{first['level']}的店内供电商品有：{'、'.join(name(s) for s in SOURCES)}。"
        if first["level"].startswith("接口"):
            ans += "该商品未注明受电电压电流，只能确认接口均为 C 型。"
        ans += f"{name(dst)}包装自带充电线（另一端接口未注明），衡电 A 系列充电器不含充电线。"
        add("power_list", 2, q, ans, [dst] + SOURCES, docs=doc(dst) + ["product_guide_charging_devices.md"],
            rules=[first["rule"]])

    # C. 无匹配供电商品（负例）
    for dst, q in [("qiguang-l4", "店里有没有能给栖光 L4 台灯供电的充电器？"),
                   ("qiguang-l6", "栖光 L6 的适配器丢了，能用衡电充电器代替吗"),
                   ("muxi-a1", "沐息 A1 扩香器可以用充电宝供电吗？"),
                   ("qiguang-l4", "栖光 L4 能不能用 C 口充电器？")]:
        spec = next(p for p in power if p["sku_id"] == dst)
        ans = (f"店内没有规格层面匹配的供电商品：{name(dst)}输入要求为 {spec['spec_value']}，接口为圆孔直流接口，"
               f"而衡电充电器和充电宝只有 C 型或 A 型口。{name(dst)}包装内已含匹配适配器。")
        add("no_path", 2, q, ans, [dst] + SOURCES, docs=doc(dst), rules=["R3_connector_mismatch"])

    # D. 外部电脑 + 线材
    laptops = {45: "ext-laptop-45w", 65: "ext-laptop-65w", 100: "ext-laptop-100w"}
    for w, ext in laptops.items():
        ok_src = [s for s in SOURCES if edge(s, ext)["rel"] == "CAN_POWER"]
        ok_cab = [c for c in CABLES if edge(c, ext)["rel"] == "CABLE_SUPPORTS"]
        add("laptop_combo", 3, f"我的电脑需要 {w} 瓦 C 口供电，店里哪些充电器和线可以搭配？",
            f"供电端需单口不低于 {w} 瓦：{'、'.join(name(s) for s in ok_src) or '无'}；线材需不低于 {w} 瓦："
            f"{'、'.join(name(c) for c in ok_cab) or '无'}。两者都满足才算规格层面匹配，且电脑需支持相应协议。",
            ok_src + ok_cab + [ext], docs=doc(*ok_src, *ok_cab) + ["product_guide_charging_devices.md"],
            rules=["R1_power_spec", "R5_cable_power"])
    for src, cab, w, q in [("hengdian-a3", "lianshu-c1", 65, "用衡电 A3 加联枢 C1 线给 65 瓦的电脑充电行吗？"),
                           ("hengdian-a3", "lianshu-c2", 65, "衡电 A3 配联枢 C2 线，能满足 65 瓦电脑吗"),
                           ("hengdian-p3", "lianshu-c3", 65, "衡电 P3 充电宝加联枢 C3 线能给 65 瓦电脑补电吗？"),
                           ("hengdian-a2", "lianshu-c2", 45, "衡电 A2 配联枢 C2 给 45 瓦笔记本供电够吗"),
                           ("hengdian-a3", "lianshu-c1", 45, "45 瓦的电脑用衡电 A3 和联枢 C1 可以吗？"),
                           ("hengdian-p3", "lianshu-c2", 100, "衡电 P3 和联枢 C2 能带动 100 瓦的电脑吗"),
                           ("hengdian-a3", "lianshu-c3", 100, "100 瓦电脑配衡电 A3 和联枢 C3 线，规格上够吗？")]:
        es, ec = edge(src, laptops[w]), edge(cab, laptops[w])
        ok = es["rel"] == "CAN_POWER" and ec["rel"] == "CABLE_SUPPORTS"
        ans = (("规格层面匹配：" if ok else "不匹配：")
               + f"{name(src)}的{es['detail']}；{name(cab)}的{ec['detail']}。")
        if not ok and ec["rel"] != "CABLE_SUPPORTS":
            ans += "可改用联枢 C2 或 C3（最高 100 瓦）。"
        add("laptop_combo", 3, q, ans, [src, cab, laptops[w]], docs=doc(src, cab), rules=[es["rule"], ec["rule"]])

    # E. 经扩展器直通
    for w, q in [(65, "电脑接联枢 H1 扩展器，再用衡电 A3 给 H1 供电，65 瓦的电脑够用吗？"),
                 (100, "联枢 H1 能把 100 瓦直通给电脑吗"),
                 (45, "45 瓦的电脑通过联枢 H1 充电，需要怎么搭配？")]:
        e = edge("lianshu-h1", laptops[w])
        ok = e["rel"] == "PASSTHROUGH"
        ans = ("规格层面匹配：" if ok else "不匹配：") + f"联枢 H1 的{e['detail'].split('；')[0]}。"
        if ok:
            ans += ("链路为 供电端 → C 转 C 线 → H1 供电口 → H1 上行口 → 电脑；供电端和线材也要不低于电脑需求，"
                    "例如衡电 A3 配联枢 C2 或 C3。")
        add("hub_chain", 4, q, ans, ["lianshu-h1", "hengdian-a3", "lianshu-c2", laptops[w]],
            docs=doc("lianshu-h1", "hengdian-a3"), rules=["R6_hub_passthrough", "R1_power_spec", "R5_cable_power"])

    # F. 多设备同时供电
    for src, devices, q in [
        ("hengdian-a2", ["qiguang-l1", "minglan-p2"], "衡电 A2 能同时给栖光 L1 和鸣澜 P2 供电吗？"),
        ("hengdian-a1", ["tinglan-s1", "minglan-p1"], "一个衡电 A1 能同时充听岚 S1 和鸣澜 P1 吗"),
        ("hengdian-a3", ["qiguang-l2", "muxi-h2"], "衡电 A3 两个 C 口同时接栖光 L2 和沐息 H2 行吗？"),
        ("hengdian-p3", ["minglan-r1", "tinglan-k1"], "露营时衡电 P3 能同时给鸣澜 R1 和听岚 K1 充电吗"),
        ("hengdian-p1", ["jianxing-m2", "tinglan-n2"], "衡电 P1 能同时用 C 口给键行 M2 和听岚 N2 充电吗？"),
    ]:
        m = multi[src]
        ok = m["two_c_devices"] == "是"
        ans = (f"{'规格层面匹配' if ok else '不匹配'}：{name(src)}有 {m['c_out_ports']} 个 C 型输出口，"
               f"合计上限 {m['combined_max_w']} 瓦{'（' + m['note'] + '）' if '同时使用' in m['note'] else ''}。")
        a_ports = [p for p in power if p["sku_id"] == src and p["connector"] == "USB-A"]
        if not ok and a_ports:
            ans += (f"另有 {a_ports[0]['port_count']} 个 A 型口（不提供 PD），店内没有 C 转 A 线，"
                    "A 型口能否用于这两件商品无法从规格确认。")
        elif not ok:
            ans += "没有其他输出口，无法同时连接两件设备。"
        add("multi_device", 3, q, ans, [src] + devices, rules=["R9_multi_device"])
    three = [s for s, m in multi.items() if m["three_c_devices"] == "是"]
    add("multi_device", 3, "有没有一个充电器能同时用 C 口给三件设备充电？",
        f"{'、'.join(name(s) for s in three) if three else '店内没有'}C 型输出口达到 3 个的供电商品；"
        "衡电 A3 为 C 口 2 个加 A 口 1 个，A 口需配合未在店内销售的线材。",
        SOURCES, docs=["product_guide_charging_devices.md", "product_hengdian-a3.md"], rules=["R9_multi_device"])
    add("multi_device", 3, "衡电 A3 三个口一起用时每个口多少瓦？",
        "三口同时使用时为 65 瓦、20 瓦与 15 瓦，合计最高 100 瓦。", ["hengdian-a3"], rules=["R9_multi_device"],
        caveat=False)

    # G. 充电宝补电
    for src, dst, q in [("hengdian-a1", "hengdian-p3", "衡电 A1 能给衡电 P3 充电宝充电吗？"),
                        ("hengdian-a3", "hengdian-p3", "给衡电 P3 补电用衡电 A3 合适吗"),
                        ("hengdian-a1", "hengdian-p1", "衡电 P1 充电宝本体用衡电 A1 充可以吗？"),
                        ("hengdian-a2", "hengdian-p2", "衡电 A2 给衡电 P2 充电宝补电规格匹配吗"),
                        ("hengdian-a2", "hengdian-p3", "衡电 A2 给 P3 充电宝充电行不行？")]:
        e = edge(src, dst)
        add("recharge", 2, q, f"{e['level']}：{name(src)}的{e['detail']}。{e['cable']}。", [src, dst], rules=[e["rule"]])

    # H. 信号连接
    for q, skus, ans in [
        ("键行 M3 有线鼠标能插在联枢 H1 扩展器上吗？", ["jianxing-m3", "lianshu-h1"],
         "规格层面匹配：键行 M3 为 A 型有线，联枢 H1 有 2 个 A 型下行口，H1 通过 C 型上行口接电脑。"),
        ("联枢 C1 线能接显示器传画面吗", ["lianshu-c1"],
         "不匹配：联枢 C1 为 USB 2.0，不支持视频输出。需要传视频可选联枢 C3（兼容设备可传 4K 60 赫兹）或联枢 H1 的 HDMI 口。"),
        ("用 C 口线接显示器，店里该买哪根？", ["lianshu-c3", "lianshu-c1", "lianshu-c2"],
         "联枢 C3 支持兼容设备 4K 60 赫兹视频，规格层面匹配；联枢 C1、C2 为 USB 2.0，不支持视频输出。"),
        ("电脑只有 C 口，想接 HDMI 显示器和 A 口鼠标，店里有什么方案？", ["lianshu-h1", "jianxing-m3"],
         "联枢 H1 规格层面匹配：C 型上行接电脑，HDMI 最高 4K 30 赫兹，A 型下行口 2 个可接键行 M3 等 A 型有线外设。"),
        ("手机只有 C 口，怎么用有线耳机？", ["lianshu-a1"],
         "可选联枢 A1 音频转接器：C 型转 3.5 毫米音频口，由主机供电，仅兼容 C 型设备。"),
        ("鸣澜 D1 音箱能接有线音源吗？店里有配套的线吗", ["minglan-d1"],
         "鸣澜 D1 有 3.5 毫米音频输入，但包装不含音频线，店内也未销售 3.5 毫米音频线。"),
        ("鸣澜哪几款音箱支持有线输入？", ["minglan-d1", "minglan-d2"],
         "鸣澜 D1 和 D2 支持 3.5 毫米音频输入，其余 P1、P2、R1、A1 仅蓝牙 5.3；所有鸣澜音箱都不含音频线。"),
        ("手机只有 C 口，想有线连鸣澜 D2，用联枢 A1 就够了吗？", ["lianshu-a1", "minglan-d2"],
         "还不够：联枢 A1 把 C 口转成 3.5 毫米口，还需要一根 3.5 毫米音频线连到 D2 的音频输入，这根线 D2 不含、店内也未销售。"),
        ("联枢 C2 和 C3 都能传视频吗", ["lianshu-c2", "lianshu-c3"],
         "不同：联枢 C2 为 USB 2.0，不支持视频；联枢 C3 为 USB 3.2 Gen 2，兼容设备可传 4K 60 赫兹视频。两者供电能力都是最高 100 瓦。"),
        ("键行 M3 能直接插在只有 C 口的电脑上吗？", ["jianxing-m3", "lianshu-h1"],
         "不能直接插：键行 M3 为 A 型有线接口。可经联枢 H1 的 A 型下行口转接。"),
    ]:
        add("signal", 2 if len(skus) < 3 else 3, q, ans, skus, rules=["R7_signal"])

    # I. 收纳
    for bag, q in [("najing-p1", "纳境 P1 收纳包能装哪些店里的东西？"), ("najing-p2", "充电宝和线材适合放哪个收纳包")]:
        ok = [f["item"] for f in fits if f["bag"] == bag and f["level"] == "类别匹配"]
        pending = [f["item"] for f in fits if f["bag"] == bag and f["level"] == "待核对"]
        ans = f"{name(bag)}适用物品原文类别匹配的店内商品有：{'、'.join(name(s) for s in ok)}。"
        if pending:
            ans += f"需核对实物尺寸的有：{'、'.join(name(s) for s in pending)}。"
        ans += "外部尺寸不保证所有物品都能同时装入。"
        add("storage", 2, q, ans, [bag] + ok, docs=doc(bag) + ["product_guide_storage_bags.md"],
            rules=["R10_storage_fit"], caveat=False)
    for q, skus, ans in [
        ("听岚 M1 头戴耳机能放进纳境 P1 吗？", ["najing-p1", "tinglan-m1"],
         "待核对：纳境 P1 适用物品含耳机，但外部尺寸为 180×110×50 毫米，头戴款体积较大，不保证能装入。"),
        ("联枢 C2 线适合放纳境 P1 吗", ["najing-p1", "lianshu-c2", "najing-p2"],
         "待核对：纳境 P1 适用于短数据线，联枢 C2 长 2 米；纳境 P2 适用物品含线材，更为对应。"),
        ("纳境 L1 内胆包能装多大的电脑？", ["najing-l1"],
         "纳境 L1 适用于机身不超过 320×225×18 毫米的电脑，需按电脑实际尺寸核对。"),
        ("出差想把衡电 A3、联枢 C2 和衡电 P3 收在一起，选哪个包？",
         ["najing-p2", "hengdian-a3", "lianshu-c2", "hengdian-p3"],
         "纳境 P2 的适用物品为充电宝、线材与适配器，三者都属于类别匹配；仍需核对实际装载空间。"),
    ]:
        add("storage", 2, q, ans, skus, rules=["R10_storage_fit"], caveat=False)

    # J. 场景：只用文档来源的场景
    scene_doc = {}
    for s in scenes:
        if s["origin"] == "doc":
            scene_doc.setdefault(s["scene_id"], (s["scene"], s["source"], []))[2].append(s["sku_id"])
    for sid, q in [("commuting", "通勤搭配推荐了哪些商品？"), ("student_study", "学生学习用品搭配里有哪些？"),
                   ("office_desktop", "办公桌面搭配都包含什么"), ("business_travel", "出差旅行推荐带哪几样？"),
                   ("starter_setup", "入门配置推荐了什么")]:
        label, source, skus = scene_doc[sid]
        add("scene", 2, q, f"{label}场景文档提到：{'、'.join(name(s) for s in skus)}。", skus, docs=[source],
            rules=["R11_scene_doc"], caveat=False)
    for sid, src, dst, q in [
        ("commuting", "hengdian-p1", "tinglan-n2", "通勤搭配里的充电宝能给搭配里的耳机充电吗？"),
        ("starter_setup", "hengdian-a1", "tinglan-s1", "入门配置里的充电器和耳机能配合使用吗？还要别的线吗"),
        ("business_travel", "hengdian-p3", "ext-laptop-65w", "出差搭配里的充电宝和数据线，能给 65 瓦的电脑补电吗？"),
        ("dormitory", "hengdian-a2", "qiguang-l3", "宿舍搭配里的台灯要配什么充电器？衡电 A2 可以吗"),
        ("student_study", "hengdian-a1", "qiguang-l1", "学生学习搭配的台灯需要另买适配器吗？用衡电 A1 行吗"),
        ("office_desktop", "hengdian-a1", "qiguang-l2", "办公桌面搭配里的台灯能和衡电 A1 一起用吗"),
    ]:
        label, source, _ = scene_doc[sid]
        e = edge(src, dst)
        extra = []
        if dst.startswith("ext-"):
            c = edge("lianshu-c2", dst)
            ans = (f"{label}搭配含{name(src)}与联枢 C2：{name(src)}的{e['detail']}，联枢 C2 的{c['detail']}，"
                   "两者均为规格层面匹配。")
            extra = ["lianshu-c2"]
        else:
            ans = f"{e['level']}：{name(src)}的{e['detail']}；{e['cable']}。"
            if "适配器" in products[dst]["not_included"]:
                ans = f"{name(dst)}不含适配器。" + ans
        add("scene_cross", 3, q, ans, [src, dst] + extra, docs=[source] + doc(src, dst),
            rules=["R11_scene_doc", e["rule"]])

    # K. 聚合查询
    incompatible = sorted({e["dst"] for e in edges if e["rel"] == "NOT_COMPATIBLE" and e["dst"] in products})
    add("aggregate", 2, "哪些商品不能用衡电充电器或充电宝供电？",
        f"接口不一致的有：{'、'.join(name(s) for s in incompatible)}，均为 12 伏圆孔直流接口，包装内已含匹配适配器。",
        incompatible, rules=["R3_connector_mismatch"])
    add("aggregate", 2, "栖光哪几款台灯需要自己另配适配器？",
        "栖光 L1、L2、L3、L5 不含供电适配器，均为 C 型接口；L4、L6 包装内已含匹配适配器。",
        ["qiguang-l1", "qiguang-l2", "qiguang-l3", "qiguang-l5", "qiguang-l4", "qiguang-l6"],
        rules=["R12_package"], caveat=False)
    host = [p["sku_id"] for p in power if p["role"] in {"host_powered", "battery"}]
    add("aggregate", 2, "键行哪些键盘鼠标不需要充电器？",
        "键行 K1、K3 和 M3 为有线连接、由主机供电且无电池；键行 M1 使用可更换 AA 电池（不随附）。"
        "键行 K2、M2 内置电池，需 C 型 5 伏充电。",
        host + ["jianxing-k2", "jianxing-m2"], rules=["R8_battery"], caveat=False)
    add("aggregate", 2, "哪些衡电产品自带充电线？",
        "衡电 P1、P2、P3 充电宝包装含 C 型充电线；衡电 A1、A2、A3 充电器不含充电线。",
        SOURCES, rules=["R12_package"], caveat=False)
    add("aggregate", 2, "店里哪些商品的包装里有匹配适配器？",
        "栖光 L4、栖光 L6 和沐息 A1 的包装含匹配适配器，三者都是 12 伏圆孔直流接口。",
        ["qiguang-l4", "qiguang-l6", "muxi-a1"], rules=["R12_package"], caveat=False)
    add("aggregate", 2, "哪些耳机的充电需求没写具体电压电流？",
        "听岚全部 8 款耳机（S1、S2、M1、K1、B1、N2、C2、Q1）只注明通用 C 型接口，未注明受电电压电流，"
        "因此与衡电产品只能确认接口层面匹配。",
        [p["sku_id"] for p in power if p["sku_id"].startswith("tinglan")], rules=["R2_interface_only"])
    add("aggregate", 2, "店里哪些线支持 100 瓦供电？",
        "联枢 C2 和 C3 最高 100 瓦（20 伏、5 安，带电子标记芯片）；联枢 C1 最高 60 瓦。",
        ["lianshu-c2", "lianshu-c3", "lianshu-c1"], rules=["R5_cable_power"], caveat=False)
    add("aggregate", 3, "能给 65 瓦电脑供电的充电设备里，哪个最便携？",
        "规格层面满足 65 瓦的只有衡电 A3（单口最高 100 瓦）和衡电 P3（单口最高 65 瓦）；"
        "P3 是充电宝可脱离插座使用，A3 需交流插座。重量对比请查看各自商品页。",
        ["hengdian-a3", "hengdian-p3"], rules=["R1_power_spec"])

    # L. 多轮
    add("power_list", 3, "那店里哪款充电器能配它",
        f"规格层面匹配的有：{'、'.join(name(s) for s in SOURCES)}；栖光 L1 自带供电线（另一端接口未注明）。",
        ["qiguang-l1"] + SOURCES,
        history=[("user", "栖光 L1 台灯包装里有适配器吗？"),
                 ("assistant", "栖光 L1 包装含灯体、底座、供电线及说明书，不含供电适配器。")])
    add("laptop_combo", 3, "线用联枢 C1 可以吗",
        "不匹配：联枢 C1 线材最高 60 瓦 < 需求 65 瓦，可改用联枢 C2 或 C3（最高 100 瓦）。",
        ["lianshu-c1", "ext-laptop-65w"],
        history=[("user", "我想给 65 瓦的电脑买充电器"), ("assistant", "衡电 A3 和衡电 P3 的 C 口单口功率不低于 65 瓦。")])
    add("signal", 3, "那我手机只有 C 口怎么接",
        "需要联枢 A1（C 型转 3.5 毫米）再加一根 3.5 毫米音频线，音频线 D2 不含、店内也未销售。",
        ["lianshu-a1", "minglan-d2"],
        history=[("user", "鸣澜 D2 能接有线吗？"), ("assistant", "鸣澜 D2 支持 3.5 毫米音频输入。")])
    add("multi_device", 3, "能同时给栖光 L2 和沐息 H2 供电吗",
        "规格层面匹配：衡电 A2 有 2 个 C 型输出口，双口同时使用时为 20 瓦与 15 瓦，"
        "分别不低于栖光 L2（10 瓦）和沐息 H2（10 瓦）的需求。",
        ["hengdian-a2", "qiguang-l2", "muxi-h2"],
        history=[("user", "衡电 A2 有几个口？"), ("assistant", "衡电 A2 有 2 个 C 型输出口，单口最高 35 瓦，双口合计 35 瓦。")])
    return rows


def generate(products=None, docs_dir="data/docs"):
    """返回 {文件名: 文本}；评测集基于 CSV 文本重新解析，保证与已写出的文件一致。"""
    products = load_products() if products is None else products
    texts = {name: render_csv(rows) for name, rows in build_tables(products).items()}
    doc_names = {path.name for path in resolve_data_path(docs_dir).glob("*.md")}
    cases = build_eval({name: parse_csv(text) for name, text in texts.items()}, products, doc_names)
    texts["eval_graph.jsonl"] = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases)
    return texts


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 data/graph 图数据；默认只预览差异，--yes 才写入")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出目录，默认 data/graph")
    parser.add_argument("--yes", action="store_true", help="写入文件")
    parser.add_argument("--check", action="store_true", help="与现有文件不一致时返回 1，不写入")
    args = parser.parse_args(argv)
    out = resolve_data_path(args.out)
    texts = generate()
    changed = []
    for name, text in texts.items():
        path = out / name
        # 按字节比较，避免通用换行把 CRLF 文件误判为不变。
        old = path.read_bytes().decode("utf-8") if path.exists() else None
        status = "新增" if old is None else ("不变" if old == text else "变化")
        if status != "不变":
            changed.append(name)
        print(f"{status}  {name}  {text.count(chr(10)) - (0 if name.endswith('.jsonl') else 1)} 行")
    if args.check:
        return 1 if changed else 0
    if not args.yes:
        if changed:
            print("预览模式未写入；确认后加 --yes", file=sys.stderr)
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for name in changed:
        (out / name).write_bytes(texts[name].encode("utf-8"))
    print(f"已写入 {len(changed)} 个文件到 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
