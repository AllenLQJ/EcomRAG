"""提升后评测语料的离线契约；不加载应用、模型或服务。"""
import csv
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read_cases(name):
    return [json.loads(line) for line in (ROOT / "data" / name).read_text(encoding="utf-8").splitlines()]


class EvalCorpusTests(unittest.TestCase):
    def test_all_expected_documents_resolve_to_promoted_corpus(self):
        for name, count in (("eval.jsonl", 24), ("eval_multiturn.jsonl", 12),
                            ("eval_ragas_gold.jsonl", 62), ("eval_colloquial.jsonl", 44)):
            rows = read_cases(name)
            self.assertEqual(len(rows), count)
            for row in rows:
                source = row.get("expected_doc")
                if source:
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
        for phrase in ("无线耳机属电子类", "自签收起 12 个月有限保修", "正常使用下确认的非人为功能故障",
                       "不等于 12 个月内都能退货"):
            self.assertIn(phrase, row["reference"])
        warranty = (ROOT / "data/docs/after_sales_warranty.md").read_text(encoding="utf-8")
        self.assertIn("无线耳机", warranty)
        self.assertIn("自签收起 12 个月有限保修，针对正常使用下确认的非人为功能故障", warranty)
        self.assertIn("不能理解成整个保障期都能退货", warranty)

    def test_multiturn_two_selected_earphones_do_not_imply_entire_catalog(self):
        row = next(row for row in read_cases("eval_multiturn.jsonl") if row["question"] == "第二款多少克")
        self.assertEqual(row["expected_doc"], "product_tinglan-s2.md")
        self.assertIn("按顺序介绍听岚 S1 和 S2", row["history"][0]["content"])
        self.assertIn("八款中的两款", row["history"][1]["content"])
        self.assertNotIn("哪两款耳机", row["history"][0]["content"])
