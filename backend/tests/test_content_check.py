"""纯离线临时假数据；可直接用 unittest 运行，无需应用依赖。"""
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ingest import content_check as check, products


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fake_product():
    row = dict.fromkeys(products.FIELDS, "标准说明")
    row.update(sku_id="tinglan-s1", name="听岚 S1 无线耳机", category="无线耳机", series="听岚",
               source="simulated", image_main="tinglan-s1/main.webp", related_skus="", not_included="")
    specs = dict.fromkeys(products.REQUIRED_SPECS["无线耳机"], "标准说明")
    specs.update(单次音乐续航="8 小时", 单只耳机重量="4.2 克")
    row["specs"] = json.dumps(specs, ensure_ascii=False)
    return row


class ContentCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve() / "data"
        self.root = self.data / "docs_new"
        self.group = self.root / "account_store"
        self.group.mkdir(parents=True)
        self.path = self.group / "account_security_guide.md"
        body = "请核对当前流程并保留页面信息，遇到异常时联系人工协助确认。" * 11
        self.text = "# 安全指引\n\n" + "\n\n".join(f"## {title}\n\n{body}" for title in ("操作入口", "使用步骤", "异常处理"))
        self.path.write_text(self.text, encoding="utf-8")
        self.facts_path = self.data / "policy/policy_facts.csv"
        self.fact = dict(id="RULE-1", domain="账号", rule="处理时限", value="7天内", status="new", source="示例", note="")
        write_csv(self.facts_path, check.FACT_FIELDS, [self.fact])
        write_csv(self.data / "products/products.csv", products.FIELDS, [fake_product()])
        self.manifest = dict(file=self.path.name, title="安全指引", category="account", facts="RULE-1", skus="")
        self.faq = dict(question="账号如何使用？", answer="请核对页面并联系人工。", category="account")
        self.link = dict(question=self.faq["question"], source_file=self.path.name, facts="RULE-1")
        self.save()
        write_csv(self.data / "faq.csv", check.FAQ_FIELDS,
                  [dict(question="配送信息在哪里查看？", answer="请查看订单。", category="logistics")])

    def save(self):
        write_csv(self.group / "manifest.csv", check.MANIFEST_FIELDS, [self.manifest])
        write_csv(self.group / "faq.csv", check.FAQ_FIELDS, [self.faq])
        write_csv(self.group / "faq_manifest.csv", check.FAQ_MANIFEST_FIELDS, [self.link])

    def run_check(self):
        return check.check_content(self.root, self.facts_path, data_dir=self.data)

    def codes(self):
        return {issue["code"] for issue in self.run_check()["issues"] if issue["level"] != "info"}

    def test_pass_missing_groups_and_rule_reverse_index(self):
        with patch("socket.socket", side_effect=AssertionError("禁止网络")):
            result = self.run_check()
        self.assertEqual(result["summary"]["errors"], 0, result)
        self.assertEqual(result["summary"]["warnings"], 0, result)
        self.assertEqual(result["summary"]["documents"], 1)
        self.assertEqual(result["rule_references"]["RULE-1"], [str(self.path)])
        self.assertEqual(result["affected_documents"]["RULE-1"], [str(self.path)])
        self.assertTrue(any(issue["code"] == "skipped" for issue in result["issues"]))

    def test_sourced_numbers_from_facts_and_specs_and_unsourced_context(self):
        self.path.write_text(self.text + "\n7 天内答复，8小时续航，4.2克重量。", encoding="utf-8")
        self.assertNotIn("unsourced_number", self.codes())
        self.path.write_text(self.text + "\n999分钟完成。", encoding="utf-8")
        issue = next(issue for issue in self.run_check()["issues"] if issue["code"] == "unsourced_number")
        self.assertEqual(issue["file"], str(self.path))
        self.assertIn("999分钟", issue["context"])

    def test_ranges_percent_and_time_units(self):
        for value in ("1–2 个工作日", "48小时内", "5%", "三个月", "20 秒", "50款", "2条", "3篇", "5次"):
            with self.subTest(value=value):
                self.assertTrue(check.numbers(value))
        self.assertEqual(check.numbers("1 - 2 个工作日"), check.numbers("1–2个工作日"))
        self.assertNotEqual(check.numbers("2小时"), check.numbers("12小时"))

    def test_chinese_counters_and_idioms_are_not_numeric_claims(self):
        text = "两款、一款、一个、两个、一条、一次、一时、一点、三款、一定、一种、一份、一张、两档。"
        self.assertEqual(check.numbers(text), set())
        for code in ("unsourced_number", "faq_number"):
            report = check.Report()
            check.check_numbers(report, self.path, text, set(), code=code)
            self.assertEqual(report.issues, [])
        self.assertEqual(check.numbers("2款、1个、3条、4次、10天、99元"),
                         {"2款", "1个", "3条", "4次", "10天", "99元"})

    def test_chinese_quantitative_units_require_numeric_sources(self):
        cases = (("三个工作日", "3个工作日"), ("十二个月", "12个月"), ("七天", "7天"),
                 ("两小时", "2小时"), ("九十九元", "99元"), ("百分之十五", "15%"),
                 ("一至三个工作日", "1–3个工作日"), ("¥99", "99元"), ("￥99", "99元"))
        for text, key in cases:
            with self.subTest(text=text):
                self.assertEqual(check.numbers(text), {key})
                report = check.Report()
                check.check_numbers(report, self.path, text, {key})
                self.assertEqual(report.issues, [])
                check.check_numbers(report, self.path, text, set())
                self.assertEqual(report.issues[0]["level"], "error")

    def test_only_explicit_example_time_positions_are_warnings(self):
        for text in ("例如，顾客在签收第10天联系客服。", "案例：下单第12天发现异常。", "假设支付第3个工作日还未完成。"):
            report = check.Report()
            check.check_numbers(report, self.path, text, set())
            self.assertEqual([issue["level"] for issue in report.issues], ["warning"], text)
            self.assertIn("人工复核", report.issues[0]["reason"])
        for text in ("签收第10天可申请。", "例如退款必须10天内到账。", "例如规定签收第10天可退货。",
                     "例如满999元免运费。", "例如三天内全部受理。", "例如签收第10天。退款期限999天。"):
            report = check.Report()
            check.check_numbers(report, self.path, text, set())
            self.assertTrue(any(issue["level"] == "error" for issue in report.issues), text)
        report = check.Report()
        check.check_numbers(report, self.path, "例如签收第10天。", set(), code="faq_number")
        self.assertEqual(report.issues[0]["level"], "error")

    def test_price_topics_depend_on_product_group_but_other_claims_do_not(self):
        text = "价格保护、库存说明、现货发货，满99元免运费，售价与¥99仅作说明。"
        for group, category, expected in (("guides", "logistics", True), ("docs_generated", "after_sales", True),
                                          ("service", "after_sales", False), ("order_promo", "payment", False),
                                          ("account_store", "store", False), ("docs", "product", True),
                                          ("docs", "logistics", False), ("docs", "promotion", False),
                                          ("account", "account", False), ("payment", "payment", False)):
            with self.subTest(group=group, category=category):
                product_rules = check.product_content(Path(group), category)
                self.assertEqual(product_rules, expected)
                report = check.Report()
                check.check_forbidden(report, self.path, text, product_rules=product_rules)
                self.assertEqual(bool(report.issues), expected)
                check.check_forbidden(report, self.path, "支持第三方登录，承诺杀菌，推荐Apple。", product_rules=product_rules)
                self.assertTrue({"unsupported_feature", "medical", "brand"} <= {i["code"] for i in report.issues})

    def test_nonproduct_amounts_need_policy_even_if_in_specs_and_faq_source(self):
        row = fake_product()
        specs = json.loads(row["specs"])
        specs["运费门槛"] = "99 元"
        row["specs"] = json.dumps(specs, ensure_ascii=False)
        write_csv(self.data / "products/products.csv", products.FIELDS, [row])
        self.path.write_text(self.text + "\n价格保护说明：满99元免运费，现货库存按页面查看。", encoding="utf-8")
        self.faq["answer"] = "价格保护说明：满99元免运费。"
        self.save()
        result = self.run_check()
        self.assertNotIn("forbidden", {i["code"] for i in result["issues"]})
        self.assertNotIn("faq_number", {i["code"] for i in result["issues"]})
        amounts = [i for i in result["issues"] if i["code"] == "unsourced_number"]
        self.assertEqual(len(amounts), 2, result)
        self.assertTrue(all(i["level"] == "error" and i["required_source"] == "policy_facts.value" for i in amounts))
        self.fact["value"] = "7天，满99元免运费"
        write_csv(self.facts_path, check.FACT_FIELDS, [self.fact])
        self.assertNotIn("unsourced_number", self.codes())
        self.faq["answer"] = "满999元免运费。"
        self.save()
        self.assertIn("faq_number", self.codes())
        self.assertIn("unsourced_number", self.codes())

    def test_guides_document_and_faq_still_reject_sourced_product_prices(self):
        group = self.root / "guides"
        group.mkdir()
        (group / self.path.name).write_text(self.text + "\n满99元免运费，价格和库存按页面查看。", encoding="utf-8")
        write_csv(group / "manifest.csv", check.MANIFEST_FIELDS, [self.manifest])
        write_csv(group / "faq.csv", check.FAQ_FIELDS, [dict(self.faq, answer="价格和库存请查看页面，满99元免运费。")])
        write_csv(group / "faq_manifest.csv", check.FAQ_MANIFEST_FIELDS, [self.link])
        self.fact["value"] = "满99元免运费"
        write_csv(self.facts_path, check.FACT_FIELDS, [self.fact])
        result = check.check_content(group, self.facts_path, data_dir=self.data)
        forbidden = [i for i in result["issues"] if i["code"] == "forbidden"]
        self.assertTrue(any(i["file"].endswith(".md") for i in forbidden))
        self.assertTrue(any("faq.csv:" in i["file"] for i in forbidden))

    def test_medical_negative_phrases_and_postposed_denials(self):
        negative = ("不承诺消毒效果。", "不提供杀菌承诺。", "不构成治疗。", "本品不代表具备治疗作用。",
                    "没有相关的消毒说明。", "没有任何说明表明本品能够消毒。", "没有关于治疗作用的说明。",
                    "没有说明可以消毒。", "消毒效果不作承诺。", "杀菌、消毒效果均不作承诺。",
                    "不承诺消毒、杀菌或治疗。", "本品无杀菌效果。", "本品不能用于治疗。")
        for text in negative:
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text, product_rules=False)
                self.assertEqual(report.issues, [])
        positive = ("承诺消毒效果。", "提供杀菌承诺。", "本品可以治疗。", "本品不但可以杀菌还能消毒。",
                    "无线耳机治疗疾病。", "不是没有消毒能力。", "并非不具备治疗作用。",
                    "没有说明就提供杀菌服务。", "没有缺点的消毒设备。")
        for text in positive:
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text, product_rules=False)
                self.assertTrue(any(i["code"] == "medical" and i["level"] == "error" for i in report.issues))

    def test_unsupported_negative_scopes_and_positive_claims(self):
        negative = ("本系统不支持实名认证。", "不提供会员等级服务。", "无第三方登录。", "没有提供实名认证入口。",
                    "不支持实名认证、会员等级和第三方登录。", "实名认证没有相关说明。",
                    "本系统没有相关的会员等级说明。", "实名认证服务未开放。", "实名认证服务没有提供。")
        for text in negative:
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text, product_rules=False)
                self.assertEqual(report.issues, [])
        positive = ("提供实名认证。", "支持第三方登录。", "会员等级现已开放。", "并非不支持实名认证。",
                    "不是没有会员等级。", "无会员等级但可以实名认证。", "不支持会员等级且提供第三方登录。")
        for text in positive:
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text, product_rules=False)
                self.assertTrue(any(i["code"] == "unsupported_feature" and i["level"] == "error" for i in report.issues))

    def test_negation_never_hides_positive_next_clause_or_contrast(self):
        for text, term in (("不承诺消毒效果，但提供杀菌功能。", "杀菌"),
                           ("没有相关消毒说明；可以治疗疾病。", "治疗"),
                           ("不支持实名认证，但可以第三方登录。", "第三方登录"),
                           ("无会员等级，提供实名认证。", "实名认证"),
                           ("不提供杀菌承诺但能够消毒。", "消毒"),
                           ("不支持会员等级且第三方登录可以使用。", "第三方登录"),
                           ("不承诺杀菌且治疗效果有保障。", "治疗")):
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text, product_rules=False)
                self.assertEqual(len(report.issues), 1, report.issues)
                self.assertEqual(report.issues[0]["message"], "禁用内容：" + term)
                self.assertEqual(report.issues[0]["level"], "error")

    def test_reported_maintenance_negations_cover_parallel_items(self):
        # 用户提供的真实原文片段；本工作树尚无对应 data/docs_new 源文件。
        cases = (
            "维护不能承诺出雾覆盖面积、杀菌或助眠效果",
            "资料没有统一消毒、浸泡或电路修复流程",
            "本文只说明资料已有的维护边界，不增加消毒温度或清洁周期",
            "资料没有统一浸泡时长、清洁液比例或杀菌效果说明",
            "不通过这些方法承诺消毒",
            "资料没有清洁液配比、浸泡时间或消毒效果，不据此编造流程",
        )
        for text in cases:
            for product_rules in (True, False):
                with self.subTest(text=text, product_rules=product_rules):
                    report = check.Report()
                    check.check_forbidden(report, self.path, text, product_rules=product_rules)
                    self.assertEqual(report.issues, [])

    def test_limiting_predicates_before_and_after_medical_objects(self):
        # 对缺少源文件的第 7/8 项，仅构造作用域回归句，不冒称复制完整原句。
        cases = (
            "不承诺对治疗近视等效果作出保证。",
            "不应对治疗近视等效果作出承诺。",
            "对治疗近视等效果不作任何承诺。",
            "对治疗近视等效果不应作出任何承诺。",
            "不得把显色参数改写成治疗近视或适合所有人。",
            "不要把显色参数改写成治疗近视或适合所有人。",
            "不应把显色参数改写成治疗近视或适合所有人。",
            "不得承诺治疗近视。",
            "不应有消毒功效的承诺。",
        )
        for text in cases:
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text)
                self.assertEqual(report.issues, [])

    def test_extended_negative_lists_keep_positive_claims_and_boundaries(self):
        for text in ("本产品可杀菌。", "有消毒功能。", "可治疗近视。",
                     "资料没有清洁步骤和本品可杀菌。", "不通过这些方法可以承诺消毒。",
                     "不应忽视本产品可治疗近视。", "不得否认本产品能杀菌。",
                     "不是不能承诺杀菌。", "并非不得宣称治疗近视。"):
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text)
                self.assertTrue(any(i["code"] == "medical" and i["level"] == "error" for i in report.issues))
        for boundary in ("。", "；", "，", "\n", "但", "然而", "而"):
            with self.subTest(boundary=boundary):
                report = check.Report()
                check.check_forbidden(report, self.path,
                                      "维护不能承诺出雾覆盖面积、杀菌或助眠效果" + boundary + "本产品有消毒功能。")
                self.assertEqual(len(report.issues), 1, report.issues)
                self.assertEqual(report.issues[0]["message"], "禁用内容：消毒")
        report = check.Report()
        check.check_forbidden(report, self.path, "不通过这些方法而承诺消毒。")
        self.assertEqual(len(report.issues), 1)

    def test_remaining_reported_method_and_rewrite_fragments(self):
        # 复制用户给出的原文片段，不补写省略的前文/后文；本工作树仍无源文件。
        cases = (
            ("product_care_insulated_cups.md", "因此不按这些方法承诺消毒。清洗目的是"),
            ("product_scene_home_reading.md", "不能把显色参数改写成治疗近视或适合所有人的结"),
        )
        for filename, text in cases:
            with self.subTest(filename=filename):
                report = check.Report()
                check.check_forbidden(report, self.group / filename, text)
                self.assertEqual(report.issues, [])

    def test_restricted_rewrite_owns_the_resulting_claim(self):
        for negative in ("不能", "不得", "不要", "不应", "不应当"):
            for operation in ("改写成", "改写为", "写成", "当作", "理解为"):
                for claim in ("治疗近视", "可治疗近视", "有消毒功能", "杀菌或消毒的保证"):
                    text = negative + "把显色参数" + operation + claim + "。"
                    with self.subTest(text=text):
                        report = check.Report()
                        check.check_forbidden(report, self.path, text)
                        self.assertEqual(report.issues, [])

    def test_new_scopes_keep_positive_claims_boundaries_and_example_warning(self):
        positive = (
            "本杯可承诺消毒。", "可把显色参数改写成治疗近视。",
            "不按这些方法而承诺消毒。", "不按这些方法可以承诺消毒。",
            "并非不按这些方法承诺消毒。", "不是不能把显色参数改写成治疗近视。",
            "不得把显色参数改写成普通照明且本品可治疗近视。",
            "不能把显色参数当作普通照明并提供杀菌保证。",
        )
        for text in positive:
            with self.subTest(text=text):
                report = check.Report()
                check.check_forbidden(report, self.path, text)
                self.assertTrue(any(i["code"] == "medical" and i["level"] == "error" for i in report.issues))
        for boundary in ("。", "；", "，", "\n", "但", "然而", "而"):
            for negative in ("因此不按这些方法承诺消毒", "不能把显色参数改写成治疗近视"):
                with self.subTest(boundary=boundary, negative=negative):
                    report = check.Report()
                    check.check_forbidden(report, self.path, negative + boundary + "本产品可杀菌。")
                    self.assertEqual([i["message"] for i in report.issues], ["禁用内容：杀菌"])
        report = check.Report()
        check.check_numbers(report, self.group / "after_sales_rejected_application_next.md",
                            "例如，顾客在签收第10天提出申请。", set())
        self.assertEqual([(i["code"], i["level"]) for i in report.issues], [("unsourced_number", "warning")])

    def test_unknown_rule_and_zero_reference_existing_rule(self):
        self.manifest["facts"] = "MISSING"
        self.link["facts"] = "MISSING"
        self.save()
        result = self.run_check()
        self.assertIn("unknown_fact", {issue["code"] for issue in result["issues"]})
        self.assertEqual(result["rule_references"]["RULE-1"], [])
        self.assertEqual(result["affected_documents"], {})

    def test_existing_rule_not_in_affected_documents(self):
        self.fact["status"] = "existing"
        write_csv(self.facts_path, check.FACT_FIELDS, [self.fact])
        self.assertEqual(self.run_check()["affected_documents"], {})

    def test_manifest_missing_extra_duplicate_category_and_title(self):
        for change, code in ((dict(file="absent.md"), "manifest_files"),
                             (dict(category="store"), "manifest_category"),
                             (dict(category="invalid"), "manifest_category"),
                             (dict(title="不一致"), "manifest_title")):
            original = self.manifest.copy()
            self.manifest.update(change)
            self.save()
            self.assertIn(code, self.codes())
            self.manifest = original
        write_csv(self.group / "manifest.csv", check.MANIFEST_FIELDS, [self.manifest] * 2)
        self.assertIn("manifest_files", self.codes())

    def test_missing_metadata_and_strict_headers_and_column_counts(self):
        for filename, fields in (("manifest.csv", check.MANIFEST_FIELDS), ("faq.csv", check.FAQ_FIELDS),
                                 ("faq_manifest.csv", check.FAQ_MANIFEST_FIELDS)):
            for text in (",".join(reversed(fields)) + "\n", ",".join(fields) + "\nonly-one-column\n"):
                self.save()
                (self.group / filename).write_text(text, encoding="utf-8")
                self.assertIn("csv", self.codes())
            self.save()
            (self.group / filename).unlink()
            self.assertIn("csv", self.codes())

    def test_structure_and_length_grading(self):
        report = check.Report()
        check.check_structure(report, self.path, "# 标题\n\n" + "\n".join("## 小节\n" + "文" * 265 for _ in range(3)))
        issue = next(issue for issue in report.issues if issue["code"] == "document_length")
        self.assertEqual(issue["level"], "warning")
        for text, expected in (("无标题", "title"), ("# 标题\n## 单节\n正文", "sections"),
                               ("# 标题\n## 一\n" + "文" * 2000, "document_length")):
            self.path.write_text(text, encoding="utf-8")
            self.assertIn(expected, self.codes())
        report = check.Report()
        check.check_structure(report, self.path, "# 标题\n" + "\n".join("## 小节\n" + "文" * 500 for _ in range(3)))
        self.assertTrue(any(issue["code"] == "section_length" and issue["level"] == "error" for issue in report.issues))

    def test_forbidden_prices_brands_medical_and_features_with_local_negation(self):
        terms = ("价格", "库存", "¥99", "99元", "华为", "Apple", "治疗", "改善听力") + check.UNSUPPORTED_FEATURES
        for term in terms:
            with self.subTest(term=term):
                report = check.Report()
                check.check_forbidden(report, self.path, "本系统支持" + term + "。")
                self.assertTrue(report.issues)
                report = check.Report()
                check.check_forbidden(report, self.path, "不支持" + term + "。")
                self.assertEqual(report.issues, [])
        report = check.Report()
        check.check_forbidden(report, self.path, "不支持短信验证码，但支持第三方登录。")
        self.assertEqual(len(report.issues), 1)
        self.assertIn("第三方登录", report.issues[0]["message"])
        report = check.Report()
        check.check_forbidden(report, self.path, "不承诺治疗或消毒，不承诺提供眼部疾病治疗效果。")
        self.assertEqual(report.issues, [])
        check.check_forbidden(report, self.path, "不支持短信验证码但支持第三方登录。")
        self.assertEqual(len(report.issues), 1)

    def test_new_pair_similarity_and_same_category_baselines(self):
        (self.group / "store_copy.md").write_text(self.text, encoding="utf-8")
        self.assertIn("similarity", self.codes())  # 新文档跨类别也比较
        (self.group / "store_copy.md").unlink()
        for folder in ("docs", "docs_generated"):
            baseline = self.data / folder
            baseline.mkdir()
            path = baseline / "account_old.md"
            path.write_text(self.text, encoding="utf-8")
            self.assertIn("similarity", self.codes())
            path.rename(baseline / "product_other.md")
        self.assertNotIn("similarity", self.codes())

    def test_faq_validation_nonempty_lengths_and_categories(self):
        for key, value in (("question", ""), ("answer", ""), ("question", "问" * 501),
                           ("answer", "答" * 10001), ("category", "invalid")):
            old = self.faq[key]
            self.faq[key] = value
            self.save()
            self.assertIn("faq_validation", self.codes())
            self.faq[key] = old

    def test_faq_duplicates_with_existing_and_new_and_near_questions(self):
        for question in (self.faq["question"], " 账号如何使用! ", "账号如何使用的方法？"):
            # 近义通过 3-gram 阈值用长公共问句验证。
            if "方法" in question:
                self.faq["question"] = "请问我应该在哪里查看账号安全设置页面的详细说明"
                self.link["question"] = self.faq["question"]
                question = self.faq["question"] + "呢"
                self.save()
            write_csv(self.data / "faq.csv", check.FAQ_FIELDS, [dict(self.faq, question=question)])
            self.assertIn("faq_duplicate", self.codes())
        write_csv(self.group / "faq.csv", check.FAQ_FIELDS, [self.faq, self.faq])
        self.assertIn("faq_duplicate", self.codes())

    def test_faq_sources_rule_links_and_answer_numbers(self):
        self.link["source_file"] = "../account_security_guide.md"
        self.save()
        self.assertIn("faq_source", self.codes())
        self.link["source_file"] = self.path.name
        self.faq["answer"] = "7天内完成。"
        self.save()
        self.assertIn("faq_number", self.codes())  # 在事实表中也必须出现在来源文档
        self.path.write_text(self.text + "\n7 天内完成。", encoding="utf-8")
        self.assertNotIn("faq_number", self.codes())
        write_csv(self.group / "faq_manifest.csv", check.FAQ_MANIFEST_FIELDS, [self.link] * 2)
        self.assertIn("faq_manifest", self.codes())

    def test_sku_name_numeric_binding_even_if_other_source_has_number(self):
        for text, bad in (("听岚 S1 无线耳机续航为8小时。", False),
                          ("听岚 S1 无线耳机续航为7天。", True),
                          ("# 听岚 S1 无线耳机\n| 单次音乐续航 | 7小时 |", True),
                          ("# 听岚 S1 无线耳机\n| 单次音乐续航 | 8小时 |", False)):
            report = check.Report()
            rows = products.read_products(self.data / "products/products.csv")
            check.check_skus(report, self.path, text, rows)
            self.assertEqual(bool(report.issues), bad, report.issues)
        self.manifest["skus"] = "unknown-x1"
        self.save()
        self.assertIn("unknown_sku", self.codes())

    def test_sku_filename_name_unknown_model_and_wrong_parameter_binding(self):
        rows = products.read_products(self.data / "products/products.csv")
        for path, text, code in (("product_tinglan-s1.md", "# 名称写错", "sku_name"),
                                 ("product_note.md", "听岚 S99 无线耳机", "unknown_sku"),
                                 ("product_note.md", "SKU：missing-x1", "unknown_sku"),
                                 ("product_note.md", "听岚 S1 单次音乐续航：4.2克。", "sku_parameter"),
                                 ("product_note.md", "听岚 S1 单次音乐续航8小时；单只耳机重量8小时。", "sku_parameter"),
                                 ("product_note.md", "# 听岚 S1 无线耳机\n本款单次音乐续航7天。", "sku_parameter")):
            report = check.Report()
            check.check_skus(report, Path(path), text, rows)
            self.assertIn(code, {issue["code"] for issue in report.issues}, report.issues)

    def test_standalone_generated_only_applicable_checks(self):
        folder = self.data / "docs_generated"
        folder.mkdir()
        (folder / "account_single.md").write_text(self.text, encoding="utf-8")
        result = check.check_content(folder, self.facts_path, data_dir=self.data)
        self.assertEqual(result["summary"]["faqs"], 0)
        self.assertNotIn("csv", {issue["code"] for issue in result["issues"]})
        self.assertNotIn("similarity", {issue["code"] for issue in result["issues"]})

    def test_cli_json_status_warning_status_and_env_protection(self):
        output = self.data / "report.json"
        with patch.object(check, "PROJECT_ROOT", self.data.parent), redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(["--root", str(self.root), "--facts", str(self.facts_path), "--json", str(output)]), 0)
            self.path.write_text("# 缺少正文", encoding="utf-8")
            self.assertEqual(check.main(["--root", str(self.root), "--facts", str(self.facts_path)]), 1)
        self.assertEqual(json.loads(output.read_text())["summary"]["errors"], 0)
        for path in (".env", ".env.local"):
            with self.assertRaises(ValueError):
                check.safe_file(self.data / path)


if __name__ == "__main__":
    unittest.main()
