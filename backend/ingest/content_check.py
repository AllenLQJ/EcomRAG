"""离线内容审计；只读 CSV/Markdown，不加载应用设置或连接服务。"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from app.faq.cache import normalize
from ingest import products
from ingest.load_faq import validate_faq_values
from ingest.loaders import CATEGORIES, PROJECT_ROOT, filename_category

GROUPS = ("guides", "service", "order_promo", "account_store")
MANIFEST_FIELDS = ("file", "title", "category", "facts", "skus")
FAQ_FIELDS = ("question", "answer", "category")
FAQ_MANIFEST_FIELDS = ("question", "source_file", "facts")
FACT_FIELDS = ("id", "domain", "rule", "value", "status", "source", "note")
DOC_MIN_CHARS, DOC_MAX_CHARS = 900, 1500
SECTION_MIN_CHARS, SECTION_MAX_CHARS = 120, 400
LENGTH_ERROR_MARGIN = 0.20
SIMILARITY_LIMIT, FAQ_SIMILARITY_LIMIT = 0.6, 0.8
REAL_BRANDS = ("苹果", "华为", "小米", "三星", "索尼", "戴森", "飞利浦", "罗技",
               "Apple", "Huawei", "Xiaomi", "Samsung", "Sony", "Bose", "Dyson", "Logitech")
UNSUPPORTED_FEATURES = ("短信验证码", "邮箱找回", "第三方登录", "实名认证", "会员等级")
MEDICAL_CLAIMS = ("治疗", "治愈", "诊断", "医疗功效", "改善听力", "听力康复", "改善睡眠", "杀菌", "消毒")
ARABIC_NUMBER = r"\d+(?:\.\d+)?"
CHINESE_NUMBER = r"[零〇一二两三四五六七八九十百千万]+"
NUMBER = rf"(?:{ARABIC_NUMBER}|{CHINESE_NUMBER})"
UNITS = ("个工作日", "工作日", "个月", "小时", "分钟", "毫升", "毫安时", "千克", "厘米", "毫米",
         "英寸", "摄氏度", "自然日", "秒", "天", "日", "月", "年", "周", "时", "点",
         "元", "%", "次", "条", "篇", "款", "克", "升", "瓦", "伏", "安", "个", "档",
         "mAh", "Gbps", "Mbps", "kHz", "Hz", "mm", "cm", "kg", "ml", "mL", "°C", "℃", "W", "V", "A", "g", "m")
# 中文量词常用于指代而非事实断言；阿拉伯数字的计数仍需核查。
CHINESE_UNITS = tuple(unit for unit in UNITS if unit not in {"个", "次", "条", "篇", "款", "档", "时", "点"})
UNIT_PATTERN = "|".join(re.escape(unit) for unit in sorted(UNITS, key=len, reverse=True))
CHINESE_UNIT_PATTERN = "|".join(re.escape(unit) for unit in sorted(CHINESE_UNITS, key=len, reverse=True))
NUMBER_RE = re.compile(
    rf"(?<![\d.])(?:[¥￥]\s*{ARABIC_NUMBER}|百分之{CHINESE_NUMBER}|"
    rf"{ARABIC_NUMBER}(?:\s*[–—~～至到-]\s*{ARABIC_NUMBER})?\s*(?:{UNIT_PATTERN})|"
    rf"{CHINESE_NUMBER}(?:\s*[–—~～至到-]\s*{CHINESE_NUMBER})?\s*(?:{CHINESE_UNIT_PATTERN}))(?![A-Za-z])"
)
PRODUCT_GROUPS = {"guides", "docs_generated"}


def safe_file(path):
    path = Path(path)
    if any(part.startswith(".env") for part in path.parts):
        raise ValueError("禁止访问 .env* 文件")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"不读取符号链接：{path}")
    return path


def csv_rows(path, fields):
    """严格表头、列数，保留重复行供后续审计。"""
    with safe_file(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(fields):
            raise ValueError("表头必须为 " + ",".join(fields))
        rows = []
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"第 {line} 行列数不正确")
            rows.append({key: value.strip() for key, value in row.items()})
        return rows


def identifiers(value):
    return [item for item in re.split(r"[;,，；|\s]+", value.strip()) if item]


def number_key(value):
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(rf"百分之({CHINESE_NUMBER})", lambda match: str(chinese_integer(match[1])) + "%", value)
    value = re.sub(rf"({CHINESE_NUMBER})(?=\s*(?:[–—~～至到-]|{CHINESE_UNIT_PATTERN}))",
                   lambda match: str(chinese_integer(match[1])), value)
    value = re.sub(rf"[¥￥]\s*({ARABIC_NUMBER})", r"\1元", value)
    return re.sub(r"\s+", "", re.sub(r"[—~～至到-]", "–", value))


def chinese_integer(value):
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if not re.search("[十百千万]", value):
        return int("".join(str(digits[char]) for char in value))
    total = section = digit = 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char == "万":
            total += (section + digit or 1) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return total + section + digit


def numbers(text):
    text = unicodedata.normalize("NFKC", text)
    return {number_key(match.group()) for match in NUMBER_RE.finditer(text)}


def snippet(text, start, end):
    return text[max(0, start - 28):min(len(text), end + 28)].replace("\n", " ")


class Report:
    def __init__(self):
        self.issues = []
        self.references = defaultdict(set)
        self.affected = defaultdict(set)
        self.documents = []
        self.faqs = []

    def add(self, level, code, file, message, **details):
        self.issues.append(dict(level=level, code=code, file=str(file), message=message, **details))

    def read_csv(self, path, fields):
        try:
            return csv_rows(path, fields)
        except (OSError, ValueError, csv.Error) as exc:
            self.add("error", "csv", path, str(exc))
            return []

    def result(self):
        levels = Counter(issue["level"] for issue in self.issues)
        document_results = []
        groups = {}
        for doc in self.documents:
            file, group = str(doc["path"]), str(doc["group"])
            counts = Counter(issue["level"] for issue in self.issues if issue["file"] == file)
            document_results.append(dict(file=file, category=doc["category"], errors=counts["error"], warnings=counts["warning"]))
            groups.setdefault(group, dict(documents=0, errors=0, warnings=0))["documents"] += 1
        for group, stats in groups.items():
            counts = Counter(issue["level"] for issue in self.issues if issue["file"].startswith(group + "/"))
            stats.update(errors=counts["error"], warnings=counts["warning"])
        return dict(summary=dict(documents=len(self.documents), faqs=len(self.faqs),
                                 errors=levels["error"], warnings=levels["warning"], info=levels["info"],
                                 categories=dict(Counter(doc["category"] for doc in self.documents))),
                    groups=groups, documents=document_results, issues=self.issues,
                    rule_references={key: sorted(value) for key, value in sorted(self.references.items())},
                    affected_documents={key: sorted(value) for key, value in sorted(self.affected.items())})


def trace_rules(report, value, file, facts, *, document=None):
    for fact in identifiers(value):
        if fact not in facts:
            report.add("error", "unknown_fact", file, f"规则编号不存在：{fact}")
        else:
            reference = str(document or file)
            report.references[fact].add(reference)
            if facts[fact]["status"] == "new":
                report.affected[fact].add(reference)


def check_structure(report, path, text):
    if not re.search(r"(?m)^#\s+\S", text):
        report.add("error", "title", path, "缺少一级标题")
    sections = re.split(r"(?m)^##\s+[^\n]+\n?", text)[1:]
    if len(sections) < 3:
        report.add("error", "sections", path, "至少需要 3 个二级小节", actual=len(sections))

    def length_check(code, value, low, high, **details):
        if not low <= value <= high:
            level = "error" if value < low * (1 - LENGTH_ERROR_MARGIN) or value > high * (1 + LENGTH_ERROR_MARGIN) else "warning"
            report.add(level, code, path, f"字数 {value}，要求 {low}–{high}", actual=value, **details)

    # 不计空白及 Markdown 标记；正文、标题、数字与标点均按 Unicode 字符计。
    count = lambda value: len(re.sub(r"[\s#*`|]+", "", value))
    length_check("document_length", count(text), DOC_MIN_CHARS, DOC_MAX_CHARS)
    for index, section in enumerate(sections, 1):
        length_check("section_length", count(section), SECTION_MIN_CHARS, SECTION_MAX_CHARS, section=index)


def negated(text, start, end=None):
    # 保留顿号并列项，但逗号、句号、换行和转折会终止前面的否定作用域。
    boundary = r"[，,。；;！!？?\n]"
    contrast = r"但是|但|然而|不过|却|而是|反而|实际上|而(?!且)"
    prefix = re.split(contrast, re.split(boundary, text[:start])[-1])[-1].rstrip()
    suffix = re.split(contrast, re.split(boundary, text[end if end is not None else start:])[0])[0]
    double_negative = r"(?:并非|不是|并不是|不能说|没有|不可能)\s*(?:不能(?:承诺|把)|不(?:通过|按|以|依据|根据)|不支持|不提供|不具备|不应|不得|不要|没有|无)"
    if re.search(double_negative, prefix) or re.match(r"(?:功能|服务|能力|效果|的)*" + double_negative, suffix):
        return False
    # “不能把 X 改写成 Y”否定的是 Y 中的整项宣称，包括 Y 内的“可/有”。
    # 此模式在已见到改写动作、进入 Y 后生效，不扩展到转折、分句或新肯定谓语。
    rewrite = re.search(r"(?:不能|不得|不要|不应(?:当)?)把.+?(?:改写成|改写为|写成|当作|理解为)", prefix)
    if rewrite:
        rewritten_object = prefix[rewrite.end():]
        if not re.search(r"并且|而且|同时|且|另外|此外|并(?:提供|支持|承诺|保证|宣称)", rewritten_object):
            return True
    if products._negated(prefix, len(prefix), water=True):
        return True
    negatives = list(re.finditer(
        r"不(?:通过|按|以|依据|根据)(?:(?!承诺|但|而|并|且|仍|就|还|可以|能够|能|可|支持|提供|具备|具有|拥有)[^，,。；;\n]){1,40}承诺|"
        r"不能承诺|不承诺(?:提供)?|不增加|(?:不应(?:当)?|不得|不要)(?:承诺|保证|宣称|声称|宣传|有|具备|具有)?|"
        r"不构成|不代表(?:具备|具有|可以|能够)?|不能用于|不用于|不提供|不支持(?:通过)?|不具备|不具有|不保证|不能保证|"
        r"不作(?:任何)?承诺|不作|不是|并非|禁止|未开放|未提供|未说明|未标注|没有(?:提供|支持|开通|开放|设置)?|无", prefix))
    if negatives:
        predicate = negatives[-1]
        tail = prefix[predicate.end():]
        # “没有说明表明能够消毒”是否定证据，不能被内嵌的“能够”当成新承诺。
        absent_evidence = predicate[0].startswith("没有") and re.search(
            r"(?:说明|证据|资料|文档|依据|信息)(?:表明|证明|显示|写明|说明|能够|可以|支持|具有|具备|能|可)", tail)
        # 资料没有 A、B 或 C：缺的是资料中的事实/流程，并列末项不必紧邻“没有”。
        document_absence = predicate[0] == "没有" and re.search(
            r"(?:资料|文档|说明书|说明|手册|知识库)(?:中|里|内)?(?:目前|暂时|尚)?$", prefix[:predicate.start()])
        positive = re.search(r"就|仍|可以|能够|支持|提供|具备|具有|承诺|保证|拥有|(?<!不)[可能有]$", tail)
        # “不支持会员等级且第三方登录可以使用”含新的后置肯定谓语。
        if re.search(r"并且|而且|同时|且|另外|此外", tail) and re.match(
                r"(?:的|功能|服务|能力|效果|作用|承诺|入口|方式)*"
                r"(?:可以|能够|支持|提供|具备|具有|有保障|显著|有效|现已开放|可用|可使用)", suffix):
            return False
        # 单独的“无”只修饰明确限制/功能项，避免将“无线耳机治疗…”放行。
        limited_absence = not (predicate[0] == "无" or predicate[0].startswith("没有")) or not re.sub(
            r"任何|相关|相应|此类|上述|明确|有效|关于|本品|本系统|的|需|须|服务|功能|能力|效果|作用|承诺|说明|[、和或与及\s]|"
            + "|".join(MEDICAL_CLAIMS + UNSUPPORTED_FEATURES), "", tail)
        if (limited_absence or absent_evidence or document_absence) and (not positive or absent_evidence and not re.search(r"就|仍", tail)):
            return True
    # 兼容“杀菌效果不作承诺”“消毒能力没有相关说明”等后置否定，不能跨转折。
    suffix = re.sub(r"^(?:的|近视|等|功能|服务|能力|效果|作用|承诺|[、和或与及\s]|"
                    + "|".join(MEDICAL_CLAIMS + UNSUPPORTED_FEATURES) + r")*", "", suffix)
    return bool(re.match(r"(?:均|也|都)?(?:不承诺|不能承诺|不保证|不[作做](?:出)?(?:任何)?承诺|"
                         r"(?:不应(?:当)?|不得|不要)(?:作出|作|做出|做)?(?:任何)?(?:承诺|保证|宣称|声称|宣传)|"
                         r"不提供|不支持|不具备|未说明|未标注|"
                         r"未开放|没有(?:提供|支持|开放|开通)|没有(?:任何|相关|相应|明确)?(?:说明|依据|证据)|无(?:相关|相应)?说明)", suffix))


def product_content(group, category):
    if Path(group).name in PRODUCT_GROUPS:
        return True
    if Path(group).name in GROUPS:
        return False
    return category == "product"


def check_forbidden(report, path, text, *, product_rules=True):
    patterns = [("brand", re.compile("|".join(re.escape(term) for term in REAL_BRANDS), re.I)),
                ("medical", re.compile("|".join(MEDICAL_CLAIMS))),
                ("unsupported_feature", re.compile("|".join(UNSUPPORTED_FEATURES)))]
    if product_rules:
        patterns.insert(0, ("forbidden", products.FORBIDDEN))
    for code, pattern in patterns:
        for match in pattern.finditer(text):
            # promotion 内容允许“促销”；元件、单元等不是金额。
            if code == "forbidden" and (match.group() == "促销" or
                    match.group() == "元" and not re.search(NUMBER + r"\s*$", text[:match.start()])):
                continue
            start = match.start()
            if code == "forbidden" and match.group() == "元":
                amount = re.search(NUMBER + r"\s*$", text[:start])
                if amount:
                    start = amount.start()
            if not negated(text, start, match.end()):
                report.add("error", code, path, f"禁用内容：{match.group()}",
                           context=snippet(text, match.start(), match.end()))


def check_numbers(report, path, text, allowed, *, code="unsourced_number", money_allowed=None, money_only=False):
    normalized = unicodedata.normalize("NFKC", text)
    for match in NUMBER_RE.finditer(normalized):
        key = number_key(match.group())
        monetary = key.endswith("元")
        if money_only and not monetary:
            continue
        sources = money_allowed if monetary and money_allowed is not None else allowed
        if key not in sources:
            # 仅显式案例中的时间位置可降级；金额、规则期限及 FAQ 来源仍严格报错。
            prefix = re.split(r"[。；;！？!?\n]", normalized[:match.start()])[-1]
            example = code == "unsourced_number" and not monetary and prefix.rstrip().endswith("第") and bool(
                re.search(r"例如|比如|举例|示例|假设|假如|案例|例[:：]", prefix)) and bool(
                re.search(r"(?:天|日|个月|月|年|周|小时|分钟|秒)$", key)) and not re.search(
                r"规则|规定|必须|应当|要求|承诺|一律|统一|期限|时限|请在", prefix)
            details = dict(number=key, context=snippet(normalized, match.start(), match.end()))
            if example:
                details["reason"] = "显式案例中的时间位置，仍需人工复核；不能用作规则出处"
            if monetary and money_allowed is not None:
                details["required_source"] = "policy_facts.value"
            report.add("warning" if example else "error", code, path, f"无出处数字：{match.group()}", **details)


def check_skus(report, path, text, rows):
    """名称/SKU 建立局部主体；参数表核对键值，正文核对该主体的数值集合。"""
    aliases = {}
    if not rows:
        return
    by_id = {row["sku_id"]: row for row in rows}
    title = re.search(r"(?m)^#\s+(.+)$", text)
    for row in rows:
        if Path(path).stem in {"product_" + row["sku_id"], "product_" + row["sku_id"].replace("-", "_")}:
            if not title or row["name"] not in title[1]:
                report.add("error", "sku_name", path, f"文件 SKU {row['sku_id']} 的标题名称与商品表不符")
    models = defaultdict(list)
    for row in rows:
        for alias in (row["name"], row["sku_id"], " ".join(row["name"].split()[:2])):
            aliases[alias] = row["sku_id"]
        model = re.search(r"\b[A-Z]+\d+\b", row["name"])
        if model:
            models[model.group()].append(row["sku_id"])
    series_pattern = "|".join(re.escape(series) for series in sorted({row["series"] for row in rows}))
    for match in re.finditer(r"(" + series_pattern + r")\s*([A-Za-z]+\d+)\b", text):
        candidates = models.get(match[2], [])
        if not any(by_id[sku]["series"] == match[1] for sku in candidates):
            report.add("error", "unknown_sku", path, f"商品系列/型号不存在：{match[0]}")
    if not aliases:
        return
    pattern = re.compile("|".join(re.escape(key) + (r"(?![A-Za-z0-9])" if key[-1:].isascii() else "")
                                  for key in sorted(set(aliases) | models.keys(), key=len, reverse=True)))

    def check_parameters(sku, clause, line):
        for key, expected in by_id[sku]["specs"].items():
            for parameter in re.finditer(re.escape(key) + r"\s*[:：为是]?\s*([^；;。|，,]+)", clause):
                unexpected = numbers(parameter[1]) - numbers(expected)
                if unexpected:
                    report.add("error", "sku_parameter", path,
                               f"{sku} 参数 {key} 与 specs 不同：{','.join(sorted(unexpected))}", context=line[:240])

    main = None
    for line in text.splitlines():
        mentions = []
        for mention in pattern.finditer(line):
            sku = aliases.get(mention.group())
            if sku is None:
                candidates = models[mention.group()]
                if main:
                    candidates = [sku for sku in candidates if by_id[sku]["series"] == by_id[main]["series"]]
                sku = candidates[0] if len(candidates) == 1 else None
            if sku:
                mentions.append((mention, sku))
        if line.startswith("#") and mentions:
            main = mentions[0][1]
        table = re.match(r"\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
        if table and main and table.group(1) in by_id[main]["specs"]:
            expected = by_id[main]["specs"][table.group(1)]
            if number_key(table.group(2)) != number_key(expected):
                report.add("error", "sku_parameter", path, f"{main} 参数 {table.group(1)} 与 specs 不同", context=line)
        if main and not mentions and re.search(r"本款|本商品|该款|该商品", line):
            check_parameters(main, line, line)
        for index, (mention, sku) in enumerate(mentions):
            end = mentions[index + 1][0].start() if index + 1 < len(mentions) else len(line)
            clause = line[mention.end():end]
            check_parameters(sku, re.split(r"[。！？!?]|售后|保修|退货|审核", clause)[0], line)
            # 避免把同一行末尾的售后时间当成商品参数。
            clause = re.split(r"售后|保修|退货|审核|[，,。；;]", clause)[0]
            allowed = numbers(" ".join(by_id[sku]["specs"].values()))
            for value in sorted(numbers(clause) - allowed):
                if re.search(r"(?:个|次|条|篇|款)$", value):
                    continue
                report.add("error", "sku_number", path, f"{sku} 的数值与 specs 不符：{value}", context=line[:240])
    for sku in re.findall(r"(?i)\bSKU\s*[:：]\s*([a-z0-9-]+)", text):
        if sku not in by_id:
            report.add("error", "unknown_sku", path, f"未知 SKU：{sku}")


def compare_documents(report, documents, baselines, limit):
    cached = {str(doc["path"]): products.text_ngrams(doc["text"]) for doc in documents + baselines}
    pairs = list(combinations(documents, 2))
    pairs.extend((left, right) for left in documents for right in baselines
                 if left["category"] == right["category"] and left["path"].resolve() != right["path"].resolve())
    for left, right in pairs:
        a, b = cached[str(left["path"])], cached[str(right["path"])]
        score = len(a & b) / len(a | b) if a or b else 1.0
        if score > limit:
            report.add("error", "similarity", left["path"], f"跨文档相似度 {score:.3f} 超过 {limit}",
                       other=str(right["path"]), score=round(score, 6))


def check_faqs(report, group, facts, documents):
    rows = report.read_csv(group / "faq.csv", FAQ_FIELDS)
    links = report.read_csv(group / "faq_manifest.csv", FAQ_MANIFEST_FIELDS)
    policy_numbers = numbers(" ".join(row["value"] for row in facts.values()))
    by_question = defaultdict(list)
    for row in links:
        by_question[row["question"]].append(row)
    for question, linked in by_question.items():
        if len(linked) != 1 or question not in {row["question"] for row in rows}:
            report.add("error", "faq_manifest", group / "faq_manifest.csv", f"问题映射重复或无对应 FAQ：{question}")
        for link in linked:
            source = documents.get(link["source_file"])
            if source is None:
                report.add("error", "faq_source", group / "faq_manifest.csv", f"来源文件不存在：{link['source_file']}")
            trace_rules(report, link["facts"], group / "faq_manifest.csv", facts,
                        document=source["path"] if source else None)
    for index, row in enumerate(rows, 2):
        file = f"{group / 'faq.csv'}:{index}"
        try:
            validate_faq_values(row, index)
        except ValueError as exc:
            report.add("error", "faq_validation", file, str(exc))
        report.faqs.append(dict(row, file=file))
        product_rules = product_content(group, row["category"])
        check_forbidden(report, file, row["answer"], product_rules=product_rules)
        if not product_rules:
            check_numbers(report, file, row["answer"], set(), money_allowed=policy_numbers, money_only=True)
        linked = by_question.get(row["question"], [])
        if len(linked) != 1:
            report.add("error", "faq_manifest", file, "FAQ 必须有且仅有一条来源映射")
        elif linked[0]["source_file"] in documents:
            check_numbers(report, file, row["answer"], numbers(documents[linked[0]["source_file"]]["text"]), code="faq_number")


def check_content(root, facts_path, *, data_dir=None, similarity_threshold=SIMILARITY_LIMIT):
    root, facts_path = Path(root), Path(facts_path)
    data = Path(data_dir) if data_dir is not None else PROJECT_ROOT / "data"
    report = Report()
    fact_rows = report.read_csv(facts_path, FACT_FIELDS) if facts_path.exists() else []
    if not facts_path.exists():
        report.add("info", "skipped", facts_path, "事实表不存在，跳过规则来源；数字仍核对商品规格")
    facts = {}
    for row in fact_rows:
        if not row["id"] or row["id"] in facts:
            report.add("error", "fact_id", facts_path, f"规则编号为空或重复：{row['id']}")
        facts[row["id"]] = row
        report.references[row["id"]]  # 含零引用规则
    product_rows = []
    if (data / "products/products.csv").exists():
        try:
            product_rows = products.read_products(safe_file(data / "products/products.csv"))
        except (OSError, ValueError, csv.Error) as exc:
            report.add("error", "products", data / "products/products.csv", str(exc))
    else:
        report.add("info", "skipped", data / "products/products.csv", "商品表不存在，跳过 SKU 检查")
    policy_numbers = numbers(" ".join(row["value"] for row in fact_rows))
    allowed = policy_numbers | numbers(" ".join(value for row in product_rows for value in row["specs"].values()))
    flat = root.is_dir() and (root.name in GROUPS + ("docs", "docs_generated") or
                             any(root.glob("*.md")) or (root / "manifest.csv").exists())
    groups = [root] if flat else [root / name for name in GROUPS]
    standalone_mode = flat and not (root / "manifest.csv").exists() and root.name not in GROUPS + ("docs_new",)
    if not flat and root.is_dir():
        groups.extend(folder for folder in sorted(root.iterdir()) if folder.is_dir() and folder.name not in GROUPS)
    for group in groups:
        if not group.is_dir():
            report.add("info", "skipped", group, "目录不存在，跳过")
            continue
        standalone = standalone_mode
        manifests = [] if standalone else report.read_csv(group / "manifest.csv", MANIFEST_FIELDS)
        paths = sorted(group.rglob("*.md"))
        actual = {path.relative_to(group).as_posix() for path in paths}
        counts = Counter(row["file"] for row in manifests)
        if standalone:
            report.add("info", "not_applicable", group, "独立文档目录：跳过 manifest/FAQ/规则引用检查")
        else:
            for file in sorted(actual | counts.keys()):
                if file not in actual or counts[file] != 1:
                    report.add("error", "manifest_files", group / "manifest.csv", f"文件与清单不是一一对应：{file}")
        by_file = {row["file"]: row for row in manifests}
        docs = {}
        for path in paths:
            try:
                text = safe_file(path).read_text(encoding="utf-8-sig")
            except (OSError, ValueError) as exc:
                report.add("error", "document", path, str(exc))
                continue
            relative = path.relative_to(group).as_posix()
            doc = dict(path=path, group=group, text=text, category=filename_category(path))
            docs[relative] = doc
            report.documents.append(doc)
            metadata = by_file.get(relative)
            if metadata:
                if metadata["category"] not in CATEGORIES or metadata["category"] != doc["category"]:
                    report.add("error", "manifest_category", path, "manifest 类别与文件名前缀不一致")
                title = re.search(r"(?m)^#\s+(.+?)\s*$", text)
                if not metadata["title"] or not title or title.group(1) != metadata["title"]:
                    report.add("error", "manifest_title", path, "manifest 标题与文档不一致")
                trace_rules(report, metadata["facts"], path, facts)
                for sku in identifiers(metadata["skus"]):
                    if sku not in {row["sku_id"] for row in product_rows}:
                        report.add("error", "unknown_sku", path, f"manifest 未知 SKU：{sku}")
            check_structure(report, path, text)
            product_rules = product_content(group, doc["category"])
            check_numbers(report, path, text, allowed, money_allowed=None if product_rules else policy_numbers)
            check_forbidden(report, path, text, product_rules=product_rules)
            check_skus(report, path, text, product_rows)
        if not standalone:
            check_faqs(report, group, facts, docs)
    baselines = []
    for folder in (data / "docs", data / "docs_generated"):
        if not folder.is_dir():
            report.add("info", "skipped", folder, "比对目录不存在，跳过")
            continue
        for path in sorted(folder.rglob("*.md")):
            if any(path.resolve() == doc["path"].resolve() for doc in report.documents):
                continue
            try:
                baselines.append(dict(path=path, text=safe_file(path).read_text(encoding="utf-8-sig"), category=filename_category(path)))
            except (OSError, ValueError) as exc:
                report.add("error", "document", path, str(exc))
    compare_documents(report, report.documents, baselines, similarity_threshold)
    existing = data / "faq.csv"
    if existing.exists() and not standalone_mode:
        for index, row in enumerate(report.read_csv(existing, FAQ_FIELDS), 2):
            try:
                validate_faq_values(row, index)
            except ValueError as exc:
                report.add("error", "faq_validation", existing, str(exc))
            report.faqs.append(dict(row, file=f"{existing}:{index}"))
    elif not existing.exists():
        report.add("info", "skipped", existing, "现有 FAQ 不存在，跳过")
    for left, right in combinations(report.faqs, 2):
        score = products.text_similarity(normalize(left["question"]), normalize(right["question"]))
        if normalize(left["question"]) == normalize(right["question"]) or score > FAQ_SIMILARITY_LIMIT:
            report.add("error", "faq_duplicate", left["file"], "FAQ 问题重复或近似", other=right["file"],
                       question=left["question"], other_question=right["question"], score=round(score, 6))
    return report.result()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/docs_new")
    parser.add_argument("--facts", default="data/policy/policy_facts.csv")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--similarity-threshold", type=float, default=SIMILARITY_LIMIT)
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.similarity_threshold <= 1:
            raise ValueError("相似度阈值必须在 0–1 之间")
        result = check_content(products.resolve_path(args.root), products.resolve_path(args.facts),
                               similarity_threshold=args.similarity_threshold)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.json_path:
            safe_file(args.json_path).write_text(output + "\n", encoding="utf-8")
        print(output)
        return int(bool(result["summary"]["errors"] or result["summary"]["warnings"]))
    except (OSError, ValueError, csv.Error) as exc:
        print(f"内容检查失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
