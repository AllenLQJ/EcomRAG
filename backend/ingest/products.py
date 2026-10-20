"""离线商品数据校验、文档与图片工具；不导入应用配置或模型。"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import re
import sys
from collections import Counter
from html import escape
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIELDS = (
    "sku_id", "name", "series", "category", "tagline", "positioning", "specs",
    "package_contents", "not_included", "usage_notes", "caveats", "related_skus",
    "image_main", "image_alt", "source",
)
REQUIRED_SPECS = {
    "无线耳机": ("佩戴方式", "续航说明", "重量说明", "充电接口", "降噪能力", "防护等级"),
    "桌面台灯": ("额定功率", "输入要求", "色温档位", "亮度调节", "整机重量", "供电接口", "电池与联网"),
    "保温杯": ("标称容量", "杯体高度", "空杯重量", "内胆材料", "保温测试", "清洗方式"),
    "蓝牙音箱": ("额定功率", "音乐续航", "电池容量", "充电输入", "连接方式", "整机重量", "防护等级"),
    "充电设备": ("设备类型", "输入要求", "输出规格", "接口配置", "尺寸", "重量", "电池容量"),
    "数据线与配件": ("配件类型", "连接接口", "传输规格", "供电能力", "尺寸", "重量"),
    "键盘鼠标": ("设备类型", "连接方式", "按键配置", "尺寸", "重量", "供电方式"),
    "收纳包": ("适用物品", "外部尺寸", "净重", "面料", "隔层配置", "清洁方式"),
    "香薰加湿": ("功能类型", "水箱容量", "额定功率", "输入要求", "喷雾量", "整机重量"),
}
SERIES = dict(zip(REQUIRED_SPECS, ("听岚", "栖光", "木澈", "鸣澜", "衡电", "联枢", "键行", "纳境", "沐息")))
LEGACY = {sku: f"product_{sku}.md" for sku in (
    "tinglan-s1", "tinglan-s2", "qiguang-l1", "muche-c1",
)}
# 原四款的键和值冻结；其余款必须同时具备公共键及下列统一补充键。
SUPPLEMENTAL_SPECS = {
    "无线耳机": ("单次音乐续航", "含充电盒总续航", "单只耳机重量", "充电盒接口", "完整充电时间",
               "关闭主动降噪单次续航", "关闭主动降噪含盒总续航", "开启主动降噪单次续航",
               "开启主动降噪含盒总续航", "充电接口与时间", "充电盒防水"),
    "桌面台灯": ("补充规格",),
    "保温杯": ("杯口内径",),
    "充电设备": ("协议支持",),
    "香薰加湿": ("供电接口", "电池与联网"),
}
SIMILARITY_LIMIT = 0.85
NO_BATTERY = re.compile(r"(?:无(?:内置)?|不含(?:内置)?|没有(?:内置)?)电池")
BATTERY_TERMS = ("电池", "电芯", "续航", "满电", "充电宝", "充满")
SELF_CHARGING_TERMS = ("充电", "补电", "充足电")
CAPABILITY_FIELDS = ("usage_notes", "caveats", "package_contents")
SKU_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
FORBIDDEN = re.compile(r"元|[¥￥]|价格|售价|促销|库存|现货(?:数量)?")
RETURN_POLICY = "签收之日起 7 天内商品存在质量问题可申请退货，运费由店铺承担。外观类问题需提供开箱照片或视频，客服 1–2 个工作日内审核。"
WARRANTIES = {
    category: "正常使用下的功能故障适用 12 个月有限保修。" for category in REQUIRED_SPECS
}
WARRANTIES["保温杯"] = "杯体制造缺陷保障期为 6 个月，密封圈正常磨损不属于永久免费更换。"
WARRANTIES["收纳包"] = "正常使用下的制造缺陷保障期为 6 个月，面料与拉链的正常磨损不属于永久免费更换。"
NON_ELECTRONIC_CATEGORIES = ("保温杯", "收纳包")
POLICY_LABELS = {"香薰加湿": "带电的香薰加湿设备"}
WEAR_PARTS = {"保温杯": ("密封圈",), "收纳包": ("拉链", "五金")}
MAINTENANCE = {
    "无线耳机": "擦干后再充电，不可浸泡、热水冲洗或自行拆改电池。",
    "桌面台灯": "清洁前先断开供电，用干燥软布擦拭，不可冲洗、浸泡或自行拆开电路。",
    "保温杯": "密封圈可拆下清洗并完全晾干后装回，不使用强腐蚀清洁剂。人为跌落造成凹陷需要另行核查。",
    "蓝牙音箱": "断电后用软布擦拭，充电口保持干燥，不可自行拆改电池。",
    "充电设备": "保持接口干燥与通风，发现异常发热、鼓包或破损时停止使用。",
    "数据线与配件": "拔插时握住插头，避免拉扯线身，接口进液或破损时停止使用。",
    "键盘鼠标": "清洁前断开连接与电源，用干燥软布擦拭，不可浸泡。",
    "收纳包": "按清洁方式处理并充分晾干，避免尖锐物划伤和过度装载。",
    "香薰加湿": "断电后清洁水箱并晾干，禁止带电加水或自行拆开电路。",
}
CHOOSING = {
    "无线耳机": "先选入耳、头戴或开放佩戴方式，再按相同模式比较续航、重量和降噪能力。",
    "桌面台灯": "按桌面空间选择底座、夹式或长灯头，再核对电源、色温与调光方式。",
    "保温杯": "按携带需求比较容量、杯高和空杯重量，保温效果需在相同测试条件下比较。",
    "蓝牙音箱": "便携使用关注重量和续航，固定摆放关注功率与连接方式，不用功率推断所有环境的音量。",
    "充电设备": "先区分随身补电与插座供电，再核对设备协议、单口输出和多口总输出。",
    "数据线与配件": "先核对接口，再区分充电、数据与视频需求，线长和最高规格需同时满足使用条件。",
    "键盘鼠标": "按键位、尺寸和连接方式选择，键盘与鼠标参数不能直接互相替代。",
    "收纳包": "先核对物品的实际尺寸，再比较包体尺寸和隔层，不能仅按屏幕对角线判断能否装入。",
    "香薰加湿": "区分纯加湿与可用水溶性香薰的机型，再比较水箱和喷雾量，不承诺治疗或消毒效果。",
}


def resolve_path(path):
    """data/... 支持从仓库根目录或 backend 执行，其他相对路径按当前目录。"""
    path = Path(path)
    return ROOT / path if not path.is_absolute() and path.parts[:1] == ("data",) else path


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"specs 重复键：{key}")
        result[key] = value
    return result


def required_specs(row):
    keys = REQUIRED_SPECS.get(row["category"], ())
    return keys if row["sku_id"] in LEGACY else keys + SUPPLEMENTAL_SPECS.get(row["category"], ())


def _negated(text, start, *, water=False):
    """仅豁免关键词前明确的局部否定，不以整句有一个“不”就放行。"""
    prefix = re.split(r"[，,。；;！!？?、]", text[:start])[-1].rstrip()
    negatives = r"(?:无|没有|不含|不附带|未配备|不提供|不支持|不具备|不可|不能)"
    if re.search(negatives + r"(?:内置|可充电|充电|任何|的)*$", prefix):
        return True
    if water and re.search(r"(?:不|无|未|没有|不承诺)(?:任何|的)?$", prefix):
        return True
    # 兼容原 L1 已有的限制：“不能把供电线理解为充满一次就能脱线使用”。
    return bool(re.search(r"不能把[^，。；]*理解为$", prefix))


def _positive_terms(text, terms):
    pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return [match.group() for match in re.finditer(pattern, text) if not _negated(text, match.start())]


def capability_errors(row):
    specs, errors = row["specs"], []
    # “65 克（不含电池）”是称重口径，不是设备不使用电池。
    power_values = [value for key, value in specs.items() if "电池" in key or key == "供电方式"]
    no_battery = any(NO_BATTERY.search(value) for value in power_values)
    connection = specs.get("连接方式", "")
    wired_only = row["category"] == "键盘鼠标" and "有线" in connection and not re.search(r"蓝牙|无线", connection)
    charger = row["category"] == "充电设备" and specs.get("设备类型") == "充电器"
    for field in CAPABILITY_FIELDS:
        text = row[field]
        terms = set(BATTERY_TERMS if no_battery else ())
        if no_battery and not charger:
            terms.update(SELF_CHARGING_TERMS)
        if wired_only:
            terms.update(("蓝牙", "配对", "接收器", "无线", "电池", "充电"))
        unexpected = _positive_terms(text, terms) if terms else []
        if charger:
            # 充电器可以给外设充电，但文案中不能借用自身储能或容量话术。
            unexpected += re.findall(r"电芯|容量|续航|充电宝", text)
        if unexpected:
            errors.append(f"{row['sku_id']} {field} 与供电/连接规格矛盾：{','.join(sorted(set(unexpected)))}")
    power = specs.get("供电方式", "")
    if "不支持充电" in power:
        for field in CAPABILITY_FIELDS:
            if _positive_terms(row[field], SELF_CHARGING_TERMS):
                errors.append(f"{row['sku_id']} {field}：不可充款不得描述充电能力")
        if re.search(r"(?:C\s*型|USB)[^、，。；]*线|充电线", row["package_contents"]):
            errors.append(f"{row['sku_id']} package_contents：不可充无线款不能包含 C 型线或充电线")
    return errors + protection_errors(row)


def protection_errors(row):
    protection = row["specs"].get("防护等级", "")
    if not protection:
        return []
    claim = re.compile(r"防泼溅|防水|IPX\d+", re.IGNORECASE)
    supported = {m.group().upper() for m in claim.finditer(protection)
                 if not _negated(protection, m.start(), water=True)}
    box_unprotected = bool(re.search(r"充电盒[^，。；]*不防水", protection)) or row["specs"].get("充电盒防水") == "不防水"
    errors = []
    for field in CAPABILITY_FIELDS + ("tagline", "positioning", "image_alt"):
        text = row[field]
        for match in claim.finditer(text):
            if _negated(text, match.start(), water=True):
                continue
            term = match.group().upper()
            prefix = re.split(r"[，,。；;]", text[:match.start()])[-1]
            invalid_box = box_unprotected and re.search(r"充电盒|盒体|整套|整机", prefix)
            invalid_grade = term.startswith("IPX") and term not in supported
            if not supported or invalid_box or invalid_grade:
                errors.append(f"{row['sku_id']} {field} 防护表述与 specs 不一致：{match.group()}（{protection}）")
    return errors


def text_ngrams(text, n=3):
    text = re.sub(r"[\W_]+", "", text.lower())
    return {text[index:index + n] for index in range(max(0, len(text) - n + 1))}


def text_similarity(left, right):
    a, b = text_ngrams(left), text_ngrams(right)
    return len(a & b) / len(a | b) if a or b else 1.0


def similar_pairs(rows):
    grams = {row["sku_id"]: text_ngrams(row["usage_notes"] + row["caveats"]) for row in rows}
    for left, right in combinations(rows, 2):
        if left["category"] != right["category"]:
            continue
        a, b = grams[left["sku_id"]], grams[right["sku_id"]]
        score = len(a & b) / len(a | b) if a or b else 1.0
        yield left["sku_id"], right["sku_id"], score


def read_products(path=ROOT / "data/products/products.csv"):
    """返回 specs 已解码的行；验证完成前不产生任何副作用。"""
    errors, rows = [], []
    with resolve_path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("CSV 表头必须依次为：" + ",".join(FIELDS))
        for line, raw in enumerate(reader, 2):
            prefix = f"第 {line} 行"
            if None in raw or any(value is None for value in raw.values()):
                errors.append(f"{prefix}列数不正确")
                continue
            row = {key: value.strip() for key, value in raw.items()}
            for key, value in row.items():
                if not value and key not in ("not_included", "related_skus"):
                    errors.append(f"{prefix} {key} 不能为空")
                if key != "specs" and FORBIDDEN.search(value):
                    errors.append(f"{prefix} {key} 含价格/库存等禁止字样")
                if "\n" in value or "\r" in value:
                    errors.append(f"{prefix} {key} 不允许换行")
            if not SKU_RE.fullmatch(row["sku_id"]) or len(row["sku_id"]) > 64:
                errors.append(f"{prefix} sku_id 格式不合法（最长 64 字符）")
            if len(row["name"]) > 160:
                errors.append(f"{prefix} name 超过 160 字")
            if len(row["tagline"]) > 30:
                errors.append(f"{prefix} tagline 超过 30 字")
            category = row["category"]
            if category not in REQUIRED_SPECS:
                errors.append(f"{prefix} category 不在白名单")
            elif row["series"] != SERIES[category]:
                errors.append(f"{prefix} series 与 category 不匹配")
            if row["source"] != "simulated":
                errors.append(f"{prefix} source 必须为 simulated")
            if not re.fullmatch(re.escape(row["sku_id"]) + r"/main\.(?:webp|png|jpg|jpeg|svg)", row["image_main"]):
                errors.append(f"{prefix} image_main 必须为 <sku_id>/main.webp|png|jpg|jpeg|svg")
            if re.search(r"听岚\s*S", row["name"], re.IGNORECASE) and (row["sku_id"], row["name"]) not in (
                ("tinglan-s1", "听岚 S1 无线耳机"), ("tinglan-s2", "听岚 S2 无线耳机"),
            ):
                errors.append(f"{prefix} 不允许新增听岚 S 系列编号")
            try:
                specs = json.loads(row["specs"], object_pairs_hook=_json_object)
                if not isinstance(specs, dict) or not specs:
                    raise ValueError("specs 必须为非空 JSON 对象")
                if any(not isinstance(value, str) or not value.strip() for value in specs.values()):
                    raise ValueError("specs 值必须为非空字符串，数字应带单位")
                for key, value in specs.items():
                    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.strip()):
                        errors.append(f"{prefix} specs.{key} 数字必须带单位")
                    if not re.fullmatch(r"[\u4e00-\u9fff]+", key):
                        errors.append(f"{prefix} specs 键必须为中文参数名：{key}")
                    if key != "运费门槛" and FORBIDDEN.search(key + value):
                        errors.append(f"{prefix} specs.{key} 含价格/库存等禁止字样")
                    if "\n" in value or "\r" in value:
                        errors.append(f"{prefix} specs.{key} 不允许换行")
                missing = set(required_specs(row)) - specs.keys()
                if missing:
                    errors.append(f"{prefix} specs 缺少必填键：{','.join(sorted(missing))}")
                if row["sku_id"] not in LEGACY and category in REQUIRED_SPECS:
                    extra = specs.keys() - set(required_specs(row)) - {"运费门槛"}
                    if extra:
                        errors.append(f"{prefix} specs 含未声明的补充键：{','.join(sorted(extra))}")
                row["specs"] = specs
            except ValueError as exc:
                errors.append(f"{prefix} specs 非法：{exc}")
                row["specs"] = {}
            rows.append(row)
    if not rows:
        errors.append("CSV 不能为空")
    for key in ("sku_id", "name"):
        for value, count in Counter(row[key] for row in rows).items():
            if count > 1:
                errors.append(f"{key} 重复：{value}")
    ids = {row["sku_id"] for row in rows}
    for row in rows:
        for related in row["related_skus"].split(";") if row["related_skus"] else ():
            if related not in ids:
                errors.append(f"{row['sku_id']} related_skus 引用不存在：{related}")
            elif related == row["sku_id"]:
                errors.append(f"{row['sku_id']} related_skus 不应引用自身")
        errors.extend(capability_errors(row))
    for left, right, score in similar_pairs(rows):
        if score > SIMILARITY_LIMIT:
            errors.append(f"{left} 与 {right} 同品类文案相似度 {score:.3f} 超过 {SIMILARITY_LIMIT}，请按规格重写")
    if errors:
        raise ValueError("\n".join(errors))
    return rows


def _md(value):
    return value.replace("|", "\\|")


def render_product(row, by_id):
    category = row["category"]
    lines = [f"# 灵码{row['name']}", "", "本文说明本商品的信息与使用要求。" + row["positioning"],
             f"主要特点：{row['tagline']}。", f"包装含{row['package_contents']}。不包含：{row['not_included'] or '无另列项目'}。",
             "", "## 参数表", "", "| 项目 | 参数 |", "| --- | --- |"]
    lines.extend(f"| {_md(key)} | {_md(value)} |" for key, value in row["specs"].items())
    lines.extend(["", "### 参数口径与限制", "", row["caveats"], "", "## 使用与口径", "", row["usage_notes"],
                  "", "## 与相关款比较", "", CHOOSING[category]])
    for sku in row["related_skus"].split(";") if row["related_skus"] else ():
        other = by_id[sku]
        # 参数直接引用表中事实，不在模板中推算或补充商品能力。
        facts = "；".join(f"{key}：{value}" for key, value in other["specs"].items())
        lines.append(f"对照灵码{other['name']}（{sku}）：{other['positioning']} 参数：{facts}。")
    maintenance = MAINTENANCE[category]
    if category == "充电设备" and row["specs"].get("设备类型") == "充电器":
        maintenance = "保持插脚与接口干燥，清洁前断开交流电源；异常发热或破损时停止使用。"
    lines.extend(["比较时应保持测试条件一致，不应仅因名称相近混用参数。", "", "### 保养与售后", "",
                  maintenance + WARRANTIES[category] + RETURN_POLICY, ""])
    return "\n".join(lines)


def render_catalog(rows):
    categories = [category for category in REQUIRED_SPECS if any(row["category"] == category for row in rows)]
    lines = ["# 灵码在售商品总览", "", "以下内容摘自各商品详情，用于了解商品品类与选购方向。", "",
             "## 你这里有什么商品？你们卖什么？", "", f"目前收录{len(rows)}款商品，分为" + "、".join(categories) + ("九个品类。" if len(categories) == 9 else f"{len(categories)}个品类。")]
    for category in categories:
        lines.extend(["", f"## {category}", "", "选购方向：" + CHOOSING[category]])
        if category == "无线耳机":
            models = [row["series"] + " " + row["name"].split()[1]
                      for row in rows if row["category"] == category]
            lines.extend(["", f"无线耳机共 {len(models)} 款，包括" + "、".join(models)
                          + "，可按佩戴方式或使用场景选择。"])
        for row in rows:
            if row["category"] == category:
                lines.extend(["", f"### 灵码{row['name']}", "", f"SKU：{row['sku_id']}。定位：{row['positioning']}",
                              f"主要特点：{row['tagline']}。",
                              "关键参数：" + "；".join(f"{key} {value}" for key, value in row["specs"].items()) + "。",
                              row["caveats"]])
    lines.extend(["", f"详细参数以各商品详情为准。目前资料收录的商品为以上 {len(rows)} 款。", ""])
    return "\n".join(lines)


def render_warranty(rows, docs_dir=ROOT / "data/docs"):
    """仅替换原政策的适用范围段；原章节、固定句式及后续正文逐字保留。"""
    original = (Path(docs_dir) / "after_sales_warranty.md").read_text(encoding="utf-8")
    title, _, body = original.split("\n\n", 2)
    categories = [category for category in REQUIRED_SPECS if any(row["category"] == category for row in rows)]
    electronic = [POLICY_LABELS.get(category, category) for category in categories if category not in NON_ELECTRONIC_CATEGORIES]
    non_electronic = [category for category in categories if category in NON_ELECTRONIC_CATEGORIES]
    scope = "本文说明灵码的保修与日常维护规则。"
    if electronic:
        scope += "电子类商品——" + "、".join(electronic) + "——自签收起 12 个月有限保修，针对正常使用下确认的非人为功能故障。"
    if non_electronic:
        wear = [part for category in non_electronic for part in WEAR_PARTS[category]]
        scope += "、".join(non_electronic) + "按 6 个月的制造缺陷保障，" + "/".join(wear) + "等正常磨损部件不作永久更换承诺。"
    return title + "\n\n" + scope + "\n\n" + body


def parameter_table(text):
    """仅提取本商品参数表，排除相关款比较中的数字。"""
    section = text.split("## 参数表", 1)[1].split("\n#", 1)[0]
    return {match[0].strip(): match[1].strip().replace("\\|", "|") for match in
            re.findall(r"^\| ([^|]+) \| (.+) \|$", section, re.MULTILINE)
            if match[0].strip() not in ("项目", "---")}


def check_legacy(documents, docs_dir=ROOT / "data/docs"):
    """按原参数项核对数值与单位；同时保护非数值规格和所有原有限制句。"""
    differences = []
    compact = lambda value: re.sub(r"\s+", "", value)
    for sku, filename in LEGACY.items():
        if sku not in documents:
            differences.append(f"{sku}：缺少原有 SKU")
            continue
        original = (Path(docs_dir) / filename).read_text(encoding="utf-8")
        if documents[sku].splitlines()[0] != original.splitlines()[0]:
            differences.append(f"{sku} / 原商品名称：原值={original.splitlines()[0]}；生成值={documents[sku].splitlines()[0]}")
        actual = parameter_table(documents[sku])
        for key, expected in parameter_table(original).items():
            if compact(actual.get(key, "")) != compact(expected):
                differences.append(f"{sku} / {key}：原值={expected}；生成值={actual.get(key, '缺失')}")
        # 原表以外含否定的完整句也必须保留，避免只保数值却丢失限制。
        for sentence in re.split(r"[。\n]", original):
            sentence = sentence.strip()
            if sentence and not sentence.startswith(("#", "|")) and re.search(r"不|无|不能|没有", sentence):
                if compact(sentence) not in compact(documents[sku]):
                    differences.append(f"{sku} / 原有限制缺失：{sentence}")
    if differences:
        raise ValueError("现有 4 款事实一致性校验失败：\n" + "\n".join(differences))


def generate(rows, out=ROOT / "data/docs_generated", docs_dir=ROOT / "data/docs"):
    out = resolve_path(out)
    protected = (ROOT / "data/docs").resolve()
    if out.resolve() == protected or protected in out.resolve().parents:
        raise ValueError("不得覆盖 data/docs，请输出到 data/docs_generated 或其他独立目录")
    by_id = {row["sku_id"]: row for row in rows}
    missing = set(LEGACY) - by_id.keys()
    if missing:
        raise ValueError("现有 4 款事实一致性校验失败：缺少原有 SKU：" + ", ".join(sorted(missing)))
    documents = {sku: render_product(row, by_id) for sku, row in by_id.items()}
    check_legacy(documents, docs_dir)
    warranty = render_warranty(rows, docs_dir)
    # 全量回归检查成功后才写文件，失败时不会留下部分结果。
    out.mkdir(parents=True, exist_ok=True)
    for sku, content in documents.items():
        (out / f"product_{sku}.md").write_text(content, encoding="utf-8")
    (out / "product_catalog.md").write_text(render_catalog(rows), encoding="utf-8")
    (out / "after_sales_warranty.md").write_text(warranty, encoding="utf-8")
    return len(documents)


def image_prompts(rows, out=ROOT / "data/products/image_prompts.md"):
    out = resolve_path(out)
    lines = ["# 商品图片生成提示", "", "## 统一风格总提示", "",
             "干净浅灰背景、柔和棚拍光、产品居中、1:1；无文字、无水印、无品牌 logo。统一的品牌点缀色朱砂 #E07A3F 仅作为极小的配饰色，不改变产品主体颜色。",
             "仅表现可见外观；容量、功率、续航等参数只帮助理解产品，不在画面上绘成文字或图表。不要绘制未包含的配件。", ""]
    for row in rows:
        lines.extend([f"## {row['sku_id']}", "", f"固定输出文件名：data/images/{row['sku_id']}/main.webp",
                      f"原图建议放置：data/images_src/{row['sku_id']}/main.png",
                      f"画面描述：{row['name']}。{row['positioning']} {row['image_alt']}。",
                      "规格参考：" + "；".join(f"{key}：{value}" for key, value in row["specs"].items()) + "。",
                      f"不包含：{row['not_included'] or '无另列项目'}。", "建议尺寸：1024 × 1024 像素，1:1；构建后主图约 800px 宽、缩略图约 320px 宽。", ""])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


COLORS = dict(zip(REQUIRED_SPECS, ("#E8EEF0", "#F2EFE5", "#E7EEE8", "#ECE9F1", "#E8ECF0", "#EEECE6", "#E6EBEA", "#EEE7E3", "#E8EDE7")))
SHAPES = {
    "无线耳机": '<path d="M290 380v-50a110 110 0 0 1 220 0v50" fill="none" stroke="#76858A" stroke-width="28"/><rect x="270" y="350" width="55" height="110" rx="24"/><rect x="475" y="350" width="55" height="110" rx="24"/>',
    "桌面台灯": '<path d="M310 490h180M400 490V300l80-55" stroke="#76858A" stroke-width="20" fill="none"/><path d="M430 220h100l35 70H395z"/>',
    "保温杯": '<rect x="335" y="245" width="130" height="260" rx="28"/><rect x="332" y="225" width="136" height="38" rx="12"/>',
    "蓝牙音箱": '<rect x="275" y="265" width="250" height="210" rx="40"/><circle cx="400" cy="370" r="62" fill="#E8EEF0"/>',
    "充电设备": '<rect x="305" y="255" width="190" height="240" rx="28"/><path d="M410 300l-45 85h40l-15 65 55-90h-40z" fill="#E8EEF0"/>',
    "数据线与配件": '<path d="M300 280v110a100 100 0 0 0 200 0V280" stroke="#76858A" stroke-width="20" fill="none"/><rect x="275" y="230" width="50" height="85" rx="10"/><rect x="475" y="230" width="50" height="85" rx="10"/>',
    "键盘鼠标": '<rect x="235" y="305" width="330" height="155" rx="20"/><path d="M260 340h280M260 380h280M290 420h220" stroke="#E8EEF0" stroke-width="14"/>',
    "收纳包": '<rect x="260" y="285" width="280" height="215" rx="30"/><path d="M345 285v-35h110v35M280 350h240" fill="none" stroke="#76858A" stroke-width="14"/>',
    "香薰加湿": '<path d="M330 335h140l35 150H295z"/><path d="M380 290q-35-35 0-70M420 290q-35-35 0-70" fill="none" stroke="#76858A" stroke-width="10"/>',
}


def placeholders(rows, images_dir=ROOT / "data/images"):
    written = 0
    for row in rows:
        folder = resolve_path(images_dir) / row["sku_id"]
        # 真图存在时不重新添加占位标记，也不覆盖真图。
        if any((folder / f"main.{ext}").is_file() for ext in ("webp", "png", "jpg", "jpeg")):
            continue
        folder.mkdir(parents=True, exist_ok=True)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" viewBox="0 0 800 800" role="img" aria-label="{escape(row["image_alt"], quote=True)}">'
               f'<rect width="800" height="800" fill="{COLORS[row["category"]]}"/>'
               f'<g fill="#76858A">{SHAPES[row["category"]]}</g><circle cx="540" cy="510" r="8" fill="#E07A3F"/>'
               f'<g text-anchor="middle" font-family="sans-serif" fill="#465359"><text x="400" y="600" font-size="28">{escape(row["name"])}</text>'
               '<text x="400" y="650" font-size="24">占位图</text></g></svg>\n')
        (folder / "main.svg").write_text(svg, encoding="utf-8")
        (folder / "main.placeholder").write_text("占位图：main.svg；真图构建成功后删除此标记。\n", encoding="utf-8")
        written += 1
    return written


def images_build(rows, src_dir=ROOT / "data/images_src", images_dir=ROOT / "data/images"):
    try:
        Image = importlib.import_module("PIL.Image")
        ImageOps = importlib.import_module("PIL.ImageOps")
    except ImportError as exc:
        raise RuntimeError("images-build 需要可选依赖 Pillow，请在当前 Python 环境安装：python -m pip install Pillow；其余商品命令无需 Pillow。") from exc
    missing = []
    for row in rows:
        sku = row["sku_id"]
        folder = resolve_path(src_dir) / sku
        candidates = sorted((path for path in folder.glob("*") if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")),
                            key=lambda path: (path.stem.lower() != "main", path.name))
        if not candidates:
            # 单独执行 images-build 时，也保证缺图 SKU 有占位图。
            placeholders([row], images_dir)
            missing.append(sku)
            continue
        target = resolve_path(images_dir) / sku
        target.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(candidates[0]) as original:
                rgba = ImageOps.exif_transpose(original).convert("RGBA")
                background = Image.new("RGBA", rgba.size, "#F2F2F2")
                image = Image.alpha_composite(background, rgba).convert("RGB")
                for name, width in (("main", 800), ("thumb", 320)):
                    scaled = image.copy()
                    scaled.thumbnail((width, max(1, round(image.height * width / image.width))), Image.Resampling.LANCZOS)
                    temporary = target / f"{name}.webp.tmp"
                    try:
                        scaled.save(temporary, format="WEBP", quality=85, method=6)
                        temporary.replace(target / f"{name}.webp")
                    finally:
                        temporary.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{sku} 原图无法构建：{candidates[0].name}；占位标记保留") from exc
        (target / "main.placeholder").unlink(missing_ok=True)
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="data/products/products.csv")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    generate_parser = commands.add_parser("generate")
    generate_parser.add_argument("--out", default="data/docs_generated")
    commands.add_parser("image-prompts")
    commands.add_parser("placeholders")
    images_parser = commands.add_parser("images-build")
    images_parser.add_argument("--src", default="data/images_src")
    args = parser.parse_args(argv)
    try:
        rows = read_products(args.csv)
        if args.command == "validate":
            print(f"商品校验通过：{len(rows)} 款")
        elif args.command == "generate":
            print(f"已生成 {generate(rows, args.out)} 款商品文档、总览与保修政策；现有 4 款事实一致性校验通过")
        elif args.command == "image-prompts":
            image_prompts(rows)
            print("已生成 data/products/image_prompts.md")
        elif args.command == "placeholders":
            print(f"已生成 {placeholders(rows)} 款占位图与 main.placeholder 标记")
        else:
            missing = images_build(rows, args.src)
            print(f"图片构建完成；缺少原图 {len(missing)} 款：" + ("、".join(missing) or "无"))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
