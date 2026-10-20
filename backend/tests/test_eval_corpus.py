"""提升后评测语料的离线契约；不加载应用、模型或服务。"""
import csv
import json
import re
import unittest
from pathlib import Path
from ingest.eval_expectations import expected_documents

ROOT = Path(__file__).resolve().parents[2]


def read_cases(name):
    return [json.loads(line) for line in (ROOT / "data" / name).read_text(encoding="utf-8").splitlines()]


class EvalCorpusTests(unittest.TestCase):
    def test_added_cases_contain_reviewed_answer_facts(self):
        # 每篇新增来源的事实依据；规范空白后比较，不能按标题/主题相似自动扩充来源。
        evidence = {
            "payment_invoice_correction_case.md": ["个人抬头填姓名", "单位抬头填完整单位名称和纳税人识别号",
                "订单完成后30天内", "一般3个工作日内开出", "不提供纸质发票"],
            "payment_address_change_case.md": ["进入仓库拣货前", "开始拣货或已发出不保证成功"],
            "promotion_points_arrival_timeline.md": ["确认收货后的第8天发放", "每满1元计1积分", "自到账之日起12个月"],
            "promotion_points_expiry_exchange.md": ["自到账之日起12个月", "积分不支持直接抵现", "会员页兑换"],
            "logistics_damage_shortage_evidence_case.md": ["包裹全貌、面单和破损部位", "连续开箱视频、外箱和填充物",
                "核对分包与装箱清单", "不含供电适配器"],
            "logistics_shipping_threshold_cases.md": ["实付满99元", "未满99元收取8元", "偏远可配送地区可能另收附加运费"],
            "logistics_tracking_status_guide.md": ["超过24小时仍无揽收记录", "连续48小时没有更新"],
            "after_sales_refusal_return_boundary.md": ["签收之日起7天内商品存在质量问题可申请退货，运费由店铺承担"],
            "after_sales_refund_arrival_selfcheck.md": ["一般2个工作日内验收", "1–3个工作日发起原路退款", "1–5个工作日"],
            "after_sales_damage_cause_checklist.md": ["人为跌落、进液", "不按质量退货规则直接处理", "充电盒不防水"],
            "after_sales_repair_cost_comparison.md": ["自签收起12个月有限保修", "正常使用下确认的非人为功能故障",
                "保温杯、收纳包是制造缺陷保障6个月", "保修期外或人为损坏按付费维修处理", "不能理解为整个保障期都能退货"],
            "payment_cancel_timing_case.md": ["30分钟内未完成支付", "已支付未拣货的订单可申请取消", "批准后原渠道退款", "不承诺立即撤单"],
            "promotion_coupon_usage_case.md": ["最多使用一张店铺优惠券", "运费不参与凑券门槛", "以结算页实际可选结果为准"],
            "promotion_price_protection_case.md": ["没有统一的全店价保承诺", "个别活动如有单独价保规则"],
        }
        used = set()
        for name in ("eval.jsonl", "eval_multiturn.jsonl", "eval_ragas_gold.jsonl", "eval_colloquial.jsonl"):
            for row in read_cases(name):
                value = row.get("expected_doc")
                if isinstance(value, list):
                    self.assertGreaterEqual(len(value), 2)
                    self.assertEqual(len(value), len(set(value)))
                    for source in value[1:]:
                        self.assertIn(source, evidence)
                        used.add(source)
        self.assertEqual(used, set(evidence))
        for source, phrases in evidence.items():
            text = re.sub(r"\s+", "", (ROOT / "data/docs" / source).read_text(encoding="utf-8"))
            for phrase in phrases:
                self.assertIn(phrase, text, (source, phrase))

    def test_all_expected_documents_resolve_to_promoted_corpus(self):
        for name, count in (("eval.jsonl", 24), ("eval_multiturn.jsonl", 12),
                            ("eval_ragas_gold.jsonl", 62), ("eval_colloquial.jsonl", 44)):
            rows = read_cases(name)
            self.assertEqual(len(rows), count)
            for row in rows:
                for source in expected_documents(row.get("expected_doc")):
                    self.assertTrue((ROOT / "data/docs" / source).is_file(), (name, source))
                    self.assertNotRegex(source, r"product_(?:tinglan_s[12]|qiguang_l1|muche_c1)\.md")

    def test_expanded_catalog_references_keep_baseline_trace(self):
        gold = {row["id"]: row for row in read_cases("eval_ragas_gold.jsonl")}
        with (ROOT / "data/products/products.csv").open(encoding="utf-8", newline="") as handle:
            products = list(csv.DictReader(handle))
        for case_id in ("catalog-001", "catalog-002", "catalog-003"):
            row = gold[case_id]
            self.assertEqual(row["reference"], row["reference_after_expansion"])
            self.assertEqual(row["source_version"], row["source_version_after_expansion"])
            self.assertEqual(row["source_version"], "kb-expanded-v1")
            self.assertEqual(row["source_version_before_expansion"], "kb-baseline-v1")
            self.assertIn("4 款", row["reference_before_expansion"])
            self.assertIn(f"{len(products)}款", row["reference"])
            for category in {product["category"] for product in products}:
                self.assertIn(category, row["reference"])
        self.assertIn("50 款", gold["refuse-005"]["reference"])
        self.assertIn("没有听岚 S3", gold["refuse-005"]["reference"])
        self.assertIn("不得编造发售时间", gold["refuse-005"]["reference"])
        self.assertNotIn("tinglan-s3", {product["sku_id"] for product in products})

    def test_warranty_reference_matches_faq_and_war01(self):
        row = next(row for row in read_cases("eval_ragas_gold.jsonl") if row["id"] == "aftersales-007")
        with (ROOT / "data/faq.csv").open(encoding="utf-8", newline="") as handle:
            faq = {row["question"]: row["answer"] for row in csv.DictReader(handle)}
        self.assertEqual(row["reference"], faq["耳机保修多久？"])
        for phrase in ("电子类商品——无线耳机", "自签收起 12 个月有限保修", "正常使用下确认的非人为功能故障",
                       "不能理解成整个保障期都能退货"):
            self.assertIn(phrase, row["reference"])
        warranty = (ROOT / "data/docs/after_sales_warranty.md").read_text(encoding="utf-8")
        self.assertIn("无线耳机", warranty)
        self.assertIn("自签收起 12 个月有限保修，针对正常使用下确认的非人为功能故障", warranty)
        self.assertIn("不能理解成整个保障期都能退货", warranty)

    def test_warranty_history_and_cup_reference_are_document_excerpts(self):
        history = read_cases("eval_multiturn.jsonl")[0]["history"][1]["content"]
        gold = next(row for row in read_cases("eval_ragas_gold.jsonl") if row["id"] == "product-014")
        warranty = (ROOT / "data/docs/after_sales_warranty.md").read_text(encoding="utf-8")
        for sentence in history.rstrip("。").split("。"):
            self.assertIn(sentence + "。", warranty)
        for phrase in ("自签收起 12 个月有限保修", "正常使用下确认的非人为功能故障",
                       "不能理解成整个保障期都能退货"):
            self.assertIn(phrase, history)
        docs = gold["expected_doc"]
        docs = [docs] if isinstance(docs, str) else docs
        self.assertIn("after_sales_warranty.md", docs)
        self.assertIn(gold["reference"], warranty)
        self.assertIn("6 个月的制造缺陷保障", gold["reference"])

    def test_multiturn_two_selected_earphones_do_not_imply_entire_catalog(self):
        row = next(row for row in read_cases("eval_multiturn.jsonl") if row["question"] == "第二款多少克")
        self.assertEqual(row["expected_doc"], "product_tinglan-s2.md")
        self.assertIn("按顺序介绍听岚 S1 和 S2", row["history"][0]["content"])
        self.assertIn("八款中的两款", row["history"][1]["content"])
        self.assertNotIn("哪两款耳机", row["history"][0]["content"])
