import csv
import json
import re
import unittest
from collections import Counter
from pathlib import Path

from ingest import products as product_data
from ingest.eval_expectations import expected_documents
from ingest.loaders import CATEGORIES
from ingest.loaders import load_directory
from ingest.splitter import split_document

ROOT = Path(__file__).resolve().parents[2]


class StoreDataTests(unittest.TestCase):
    def test_documents_have_business_headings_and_size_without_fiction_labels(self):
        docs = list((ROOT / "data/docs").glob("*.md"))
        self.assertEqual(len(docs), 156)
        rows = product_data.read_products()
        by_id = {row["sku_id"]: row for row in rows}
        generated = {f"product_{sku}.md": product_data.render_product(row, by_id)
                     for sku, row in by_id.items()}
        generated["product_catalog.md"] = product_data.render_catalog(rows)
        for path in docs:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                if path.name in generated:
                    # 参数、相关款比较及 50 款目录按商品表逐字核验，不套短政策长度。
                    self.assertEqual(text, generated[path.name])
                    self.assertEqual(text, (ROOT / "data/docs_generated" / path.name).read_text(encoding="utf-8"))
                else:
                    self.assertTrue(300 <= len(text) <= 1500, (path.name, len(text)))
                self.assertGreaterEqual(len(re.findall(r"[\u4e00-\u9fff]", text)), 300)
                self.assertRegex(text, r"^# [^\n]+\n")
                self.assertIn("\n## ", text)
                # “不虚构/不应虚构”是业务边界说明，不是资料标签。
                self.assertNotRegex(text, r"(?<!不)(?<!不应)虚构|样例")
                self.assertNotIn("｜", text.splitlines()[0])
                self.assertNotIn("火星", text)

    def test_faq_exact_count_unique_questions_and_categories(self):
        with (ROOT / "data/faq.csv").open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, ["question", "answer", "category"])
            rows = list(reader)
        self.assertEqual(len(rows), 128)
        self.assertEqual(len({row["question"] for row in rows}), 128)
        self.assertIn("怎么申请退货？", {row["question"] for row in rows})
        self.assertEqual(sum(row["question"] == "怎么申请退货？" for row in rows), 1)
        # 提升后的 FAQ 覆盖全部七个业务类别。
        self.assertEqual(
            {row["category"] for row in rows},
            CATEGORIES,
        )
        for row in rows:
            with self.subTest(question=row["question"]):
                self.assertTrue(row["answer"].strip())
                self.assertNotRegex(row["answer"], "虚构|样例")

    def test_evaluation_sets_do_not_use_fiction_labels(self):
        paths = sorted((ROOT / "data").glob("*.jsonl"))
        self.assertEqual([path.name for path in paths], ["eval.jsonl", "eval_colloquial.jsonl", "eval_multiturn.jsonl", "eval_ragas_gold.jsonl"])
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, "虚构|样例")
                self.assertTrue(all(isinstance(json.loads(line), dict) for line in text.splitlines()))

    def test_shipping_invoice_and_catalog_faq_answers_are_document_excerpts(self):
        with (ROOT / "data/faq.csv").open(encoding="utf-8", newline="") as handle:
            faq = {row["question"]: row for row in csv.DictReader(handle)}
        compact = lambda text: re.sub(r"\s+", "", text)
        for question, category, filename in (
            ("耳机保修多久？", "after_sales", "after_sales_warranty.md"),
            ("满多少免运费？", "logistics", "logistics_shipping.md"),
            ("运费怎么算？", "logistics", "logistics_shipping.md"),
            ("运费多少钱？", "logistics", "logistics_shipping.md"),
            ("发票怎么开？", "payment", "payment_invoices.md"),
            ("怎么开发票？", "payment", "payment_invoices.md"),
            ("你们有什么商品？", "product", "product_catalog.md"),
            ("店里卖哪些东西？", "product", "product_catalog.md"),
            ("有哪些耳机？", "product", "product_catalog.md"),
        ):
            with self.subTest(question=question):
                row = faq[question]
                self.assertEqual(row["category"], category)
                self.assertNotRegex(row["answer"], "虚构|样例")
                document = compact((ROOT / "data/docs" / filename).read_text(encoding="utf-8"))
                for sentence in compact(row["answer"]).split("。"):
                    if sentence:
                        self.assertIn(sentence, document)

    def test_shipping_amount_notation_is_consistent(self):
        for name in ("logistics_shipping.md", "logistics_shipping_threshold_cases.md"):
            text = (ROOT / "data/docs" / name).read_text(encoding="utf-8")
            self.assertIn("满99元", text)
            self.assertIn("收取8元", text)
            self.assertNotRegex(text, r"(?:满|收取)\s+(?:99|8)|(?:99|8)\s+元")

    def test_catalog_product_names_and_key_facts_are_grounded_in_details(self):
        compact = lambda text: re.sub(r"[\s|]", "", text)
        catalog = (ROOT / "data/docs/product_catalog.md").read_text(encoding="utf-8")
        products = {
            "product_tinglan-s1.md": ["日常通勤与语音通话", "单次音乐续航8小时", "含充电盒总续航32小时",
                                     "单只耳机重量4.2克", "通话环境降噪，无主动降噪"],
            "product_tinglan-s2.md": ["支持主动降噪和通话环境降噪", "关闭主动降噪单次续航10小时",
                                     "含盒总续航40小时", "开启主动降噪单次续航6小时", "含盒总续航24小时",
                                     "单只耳机重量4.8克"],
            "product_qiguang-l1.md": ["适合书桌阅读和普通室内照明", "额定功率6瓦", "输入要求5伏直流、2安",
                                     "色温档位3000、4000、5000开尔文", "电池与联网无电池，不支持联网控制"],
            "product_muche-c1.md": ["日常饮水用保温杯", "标称容量450毫升", "食品接触用不锈钢",
                                   "室温20摄氏度、初始水温95摄氏度，6小时后不低于55摄氏度"],
        }
        for filename, facts in products.items():
            detail = (ROOT / "data/docs" / filename).read_text(encoding="utf-8")
            for fact in facts:
                with self.subTest(product=filename, fact=fact):
                    self.assertIn(fact, compact(detail))
                    self.assertIn(fact, compact(catalog))
        rows = product_data.read_products()
        self.assertEqual(len(rows), 50)
        self.assertEqual(len({row["category"] for row in rows}), 9)
        for row in rows:
            detail = (ROOT / "data/docs" / f"product_{row['sku_id']}.md").read_text(encoding="utf-8")
            self.assertEqual(product_data.parameter_table(detail), row["specs"])
            self.assertIn("SKU：" + row["sku_id"], catalog)
            for key, value in row["specs"].items():
                self.assertIn(compact(key + " " + value), compact(catalog))
        self.assertEqual(set(re.findall(r"^### (.+)$", catalog, re.MULTILINE)),
                         {"灵码" + row["name"] for row in rows})
        self.assertIn("目前资料收录的商品为以上 50 款", catalog)
        self.assertNotRegex(catalog, r"只销售|价格|库存|促销")

    def test_catalog_overview_survives_default_chunking_with_all_nine_categories(self):
        document = next(doc for doc in load_directory(ROOT / "data/docs") if doc.source == "product_catalog.md")
        chunks = split_document(document)
        self.assertEqual(document.category, "product")
        self.assertEqual((len({chunk.parent_id for chunk in chunks}), len(chunks)), (19, 75))
        first = chunks[0]
        for phrase in ("你这里有什么商品", "你们卖什么", "50款", "九个品类",
                       *product_data.REQUIRED_SPECS):
            self.assertIn(phrase, first.child_text)
        self.assertTrue(all(chunk.child_text.startswith(document.title + "\n") for chunk in chunks))
        self.assertTrue(all(len(chunk.child_text) <= 300 and len(chunk.parent_text) <= 1200 for chunk in chunks))

    def test_eval_exact_coverage_and_document_references(self):
        rows = [json.loads(line) for line in (ROOT / "data/eval.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({row["question"] for row in rows}), 24)
        self.assertEqual(Counter(row["expected_route"] for row in rows),
                         {"faq": 6, "rag": 8, "general": 5, "fallback": 5})
        with (ROOT / "data/faq.csv").open(encoding="utf-8", newline="") as handle:
            questions = {row["question"] for row in csv.DictReader(handle)}
        for row in rows:
            self.assertEqual(set(row), {"question", "expected_route", "expected_doc"})
            if row["expected_route"] == "rag":
                for source in expected_documents(row["expected_doc"], required=True):
                    self.assertTrue((ROOT / "data/docs" / source).is_file())
                self.assertNotIn(row["question"], questions)
            else:
                self.assertIsNone(row["expected_doc"])
            if row["expected_route"] == "faq":
                self.assertIn(row["question"], questions)
        self.assertTrue(any(row["question"] == "你们支持寄到火星吗？" and row["expected_route"] == "fallback" for row in rows))

    def test_policy_and_distinct_earphone_battery_facts(self):
        for name in ("application", "exchange", "refunds", "returns", "warranty"):
            path = ROOT / "data/docs" / f"after_sales_{name}.md"
            text = path.read_text(encoding="utf-8")
            self.assertIn("签收之日起 7 天内商品存在质量问题可申请退货，运费由店铺承担", text)
            self.assertIn("外观类问题需提供开箱照片或视频，客服 1–2 个工作日内审核", text)
        first = (ROOT / "data/docs/product_tinglan-s1.md").read_text(encoding="utf-8")
        second = (ROOT / "data/docs/product_tinglan-s2.md").read_text(encoding="utf-8")
        self.assertIn("| 单次音乐续航 | 8 小时 |", first)
        self.assertIn("| 关闭主动降噪单次续航 | 10 小时 |", second)

    def test_real_sections_have_meaningful_parent_context(self):
        chunks = [chunk for document in load_directory(ROOT / "data/docs")
                  for chunk in split_document(document)]
        parents = {chunk.parent_id: chunk.parent_text for chunk in chunks}
        counts = Counter(chunk.parent_id for chunk in chunks)
        # 新语料按业务小节拆分；大量 120–400 字的独立小节无需填长。
        # 实测 136/584=23.29% 父块在 400–1000 字，319/584 有多个子块。
        self.assertGreaterEqual(sum(400 <= len(text) <= 1000 for text in parents.values()), len(parents) * 0.23)
        for chunk in chunks:
            self.assertIn(chunk.child_text.removeprefix(chunk.title + "\n"), chunk.parent_text)
        self.assertGreater(sum(count > 1 for count in counts.values()), len(parents) / 2)
        self.assertTrue(all(len(text) <= 1200 for text in parents.values()))
        self.assertTrue(all(len(chunk.child_text) <= 300 for chunk in chunks))

    def test_return_policy_facts_match_documents_and_faq(self):
        compact = lambda text: re.sub(r"\s+", "", text)
        policy = "签收之日起7天内商品存在质量问题可申请退货，运费由店铺承担"
        evidence = "外观类问题需提供开箱照片或视频，客服1–2个工作日内审核"
        for path in (ROOT / "data/docs").glob("*.md"):
            text = compact(path.read_text(encoding="utf-8"))
            if "签收之日起7天" in text:
                # 新流程文档将质量条件前置；两种句序都必须完整保留资格、期限和运费。
                variants = (policy, "商品存在质量问题时，签收之日起7天内可申请退货，运费由店铺承担")
                self.assertTrue(any(value in text for value in variants), path.name)
            if "外观类问题" in text:
                self.assertIn(evidence, text, path.name)
            self.assertTrue(all(days == "7" for days in re.findall(r"签收之日起(\d+)天内", text)))
        with (ROOT / "data/faq.csv").open(encoding="utf-8", newline="") as handle:
            faq = {row["question"]: compact(row["answer"]) for row in csv.DictReader(handle)}
        self.assertIn(policy, faq["怎么申请退货？"])
        self.assertIn(evidence, faq["怎么申请退货？"])
        self.assertIn(policy, faq["退货运费由谁承担？"])
        self.assertIn(evidence, faq["退货时需要上传哪些材料？"])

    def test_each_rag_reference_contains_answer_facts(self):
        # 人工核对后的关键答案事实，防止只校验文件存在却没有可回答内容。
        expected = {
            "听岚S1和S2关闭主动降噪时单次和含盒续航分别差多少？": ["S1 单次 8 小时、含盒 32 小时", "S2 关闭主动降噪单次 10 小时、含盒 40 小时", "分别多 2 小时和 8 小时"],
            "签收后发现屏幕有划痕可以退吗？": [
                "## 屏幕划痕与外观瑕疵的退货条件", "到货即有的外观问题",
                "签收之日起 7 天内商品存在质量问题可申请退货，运费由店铺承担",
                "外观类问题需提供开箱照片或视频，客服 1–2 个工作日内审核",
                "若划痕是签收后使用、碰撞造成，则不属于质量问题退货范围，可咨询付费维修",
            ],
            "木澈C1保温6小时的测试环境和剩余水温是什么？": ["室温 20 摄氏度", "初始水温 95 摄氏度", "6 小时后不低于 55 摄氏度"],
            "栖光L1有哪些色温档位，需要什么输入电源？": ["3000、4000、5000 开尔文", "5 伏直流、2 安"],
            "包裹外箱破损又发现少件，我应该保留哪些证据？": [
                "包裹全貌、面单和破损部位", "连续开箱视频", "装箱清单", "分包",
                "## 运输途中外包装破损与少件核查", "凭包装破损不能直接认定商品有质量缺陷",
            ],
            "开个人抬头和单位抬头电子发票，需要的信息有什么不同？": ["个人抬头填写姓名", "单位抬头填写完整单位名称和纳税人识别号"],
            "会员积分在确认收货后哪天到账，有效期多久？": ["确认收货后的第 8 天", "自到账之日起有效 12 个月"],
            "付款后的订单在什么阶段还能申请修改地址，发货后能保证成功吗？": ["进入仓库拣货前", "开始拣货或已经发出，就不能保证修改成功"],
        }
        rows = [json.loads(line) for line in (ROOT / "data/eval.jsonl").read_text(encoding="utf-8").splitlines()]
        rag_rows = [row for row in rows if row["expected_route"] == "rag"]
        self.assertEqual({row["question"] for row in rag_rows}, set(expected))
        for row in rag_rows:
            # 下列逐字事实断言针对保留的原始规则文档；案例的事实见 test_eval_corpus。
            original = expected_documents(row["expected_doc"], required=True)[0]
            text = (ROOT / "data/docs" / original).read_text(encoding="utf-8")
            for fact in expected[row["question"]]:
                self.assertIn(fact, text, row["question"])


    def test_invoice_facts_and_refund_boundary_do_not_add_ticket_promises(self):
        text = (ROOT / "data/docs/payment_invoices.md").read_text(encoding="utf-8")
        for phrase in ("普通电子发票", "订单完成后 30 天内", "一般在 3 个工作日内开出",
                       "个人抬头填写姓名", "单位抬头填写完整单位名称和纳税人识别号",
                       "优惠券抵扣金额不重复计入实际开票金额", "按红冲与重新开具流程处理",
                       "事实表没有给出退款后票据调整的完整细则", "不承诺先交回发票才受理退款",
                       "不承诺退款完成即已重开"):
            self.assertIn(phrase, text)
        self.assertNotIn("发票金额需要与剩余实际交易金额对应调整", text)

    def test_earphone_faq_covers_all_eight_models_without_price_or_stock(self):
        with (ROOT / "data/faq.csv").open(encoding="utf-8", newline="") as handle:
            faq = {row["question"]: row["answer"] for row in csv.DictReader(handle)}
        rows = [row for row in product_data.read_products() if row["category"] == "无线耳机"]
        self.assertEqual(len(rows), 8)
        self.assertIn("共 8 款", faq["有哪些耳机？"])
        self.assertIn("可按佩戴方式或使用场景选择", faq["有哪些耳机？"])
        self.assertEqual(set(re.findall(r"听岚 ([A-Z]+[0-9]+)", faq["有哪些耳机？"])),
                         {row["name"].split()[1] for row in rows})
        for question in ("你们有什么商品？", "店里卖哪些东西？", "有哪些耳机？"):
            self.assertNotRegex(faq[question], r"价格|库存|现货|[¥￥]|元")
