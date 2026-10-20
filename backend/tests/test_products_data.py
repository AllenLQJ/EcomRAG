"""仅用标准库、真实本地资料与临时目录；也可用 unittest 单独运行。"""
import copy
import csv
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from ingest import products


class ProductsDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.rows = products.read_products()
        self.raw = copy.deepcopy(self.rows)
        for row in self.raw:
            row["specs"] = json.dumps(row["specs"], ensure_ascii=False)

    def write_csv(self, rows=None, fields=products.FIELDS):
        path = self.folder / "products.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.raw if rows is None else rows)
        return path

    def invalid(self, field, value, expected):
        rows = copy.deepcopy(self.raw)
        rows[0][field] = value
        with self.assertRaisesRegex(ValueError, expected):
            products.read_products(self.write_csv(rows))

    def change_sku(self, sku, field, value):
        rows = copy.deepcopy(self.raw)
        row = next(row for row in rows if row["sku_id"] == sku)
        row[field] = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
        return self.write_csv(rows)

    def invalid_sku(self, sku, field, value, expected):
        with self.assertRaisesRegex(ValueError, expected):
            products.read_products(self.change_sku(sku, field, value))

    def test_repository_has_fifty_unique_skus_and_exact_category_counts(self):
        self.assertEqual(len(self.rows), 50)
        self.assertEqual(len({row["name"] for row in self.rows}), 50)
        self.assertEqual(Counter(row["category"] for row in self.rows), {
            "无线耳机": 8, "桌面台灯": 6, "保温杯": 6, "蓝牙音箱": 6, "充电设备": 6,
            "数据线与配件": 5, "键盘鼠标": 6, "收纳包": 4, "香薰加湿": 3,
        })
        self.assertEqual({row["name"] for row in self.rows if row["name"].startswith("听岚 S")},
                         {"听岚 S1 无线耳机", "听岚 S2 无线耳机"})

    def test_header_order_extra_columns_and_empty_csv_rejected(self):
        with self.assertRaisesRegex(ValueError, "表头"):
            products.read_products(self.write_csv(fields=tuple(reversed(products.FIELDS))))
        path = self.write_csv()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("extra,columns\n")
        with self.assertRaisesRegex(ValueError, "列数"):
            products.read_products(path)
        with self.assertRaisesRegex(ValueError, "不能为空"):
            products.read_products(self.write_csv([]))

    def test_duplicate_sku_and_name_rejected(self):
        for field in ("sku_id", "name"):
            with self.subTest(field=field):
                self.invalid(field, self.raw[1][field], field + " 重复")

    def test_sku_format_and_image_paths(self):
        for sku in ("Tinglan-S1", "bad_id", "-s1", "../s1", "a--b", "a" * 65):
            with self.subTest(sku=sku):
                self.invalid("sku_id", sku, "sku_id 格式")
        for image in ("/absolute/main.webp", "../main.webp", "tinglan-s2/main.webp",
                      "tinglan-s1/../main.webp", "tinglan-s1/main.gif", "tinglan-s1\\main.webp"):
            with self.subTest(image=image):
                self.invalid("image_main", image, "image_main")

    def test_category_series_source_and_required_fields(self):
        self.invalid("category", "手机", "白名单")
        self.invalid("series", "其他", "不匹配")
        self.invalid("source", "real", "simulated")
        self.invalid("tagline", "长" * 31, "30 字")
        self.invalid("name", "", "不能为空")
        self.invalid("name", "听岚 S3 无线耳机", "不允许新增")
        self.invalid("usage_notes", "第一行\n第二行", "不允许换行")

    def test_json_shape_values_duplicate_keys_and_required_specs(self):
        for value in ('{bad}', '[]', 'null', '{}', '{"重量": 3}', '{"重量": ""}',
                      '{"重量":"3 克","重量":"4 克"}'):
            with self.subTest(value=value):
                self.invalid("specs", value, "specs")
        for category, keys in products.REQUIRED_SPECS.items():
            row = next(row for row in self.raw if row["category"] == category)
            for key in keys:
                changed = copy.deepcopy(self.raw)
                index = self.raw.index(row)
                specs = json.loads(row["specs"])
                del specs[key]
                changed[index]["specs"] = json.dumps(specs, ensure_ascii=False)
                with self.subTest(category=category, key=key), self.assertRaisesRegex(ValueError, "缺少必填键"):
                    products.read_products(self.write_csv(changed))

    def test_specs_require_chinese_keys_and_units(self):
        specs = copy.deepcopy(self.rows[0]["specs"])
        specs["weight"] = "4.2 克"
        self.invalid("specs", json.dumps(specs, ensure_ascii=False), "中文参数名")
        del specs["weight"]
        specs["单次音乐续航"] = "8"
        self.invalid("specs", json.dumps(specs, ensure_ascii=False), "数字必须带单位")

    def test_related_missing_or_self_rejected(self):
        self.invalid("related_skus", "nonexistent-x1", "引用不存在")
        self.invalid("related_skus", "tinglan-s1", "引用自身")

    def test_price_inventory_and_promotion_rejected_in_all_text_fields(self):
        for field in products.FIELDS:
            if field == "specs":
                continue
            for forbidden in ("19元", "¥19", "￥19", "库存", "现货数量", "价格", "促销"):
                with self.subTest(field=field, forbidden=forbidden):
                    self.invalid(field, forbidden, "禁止字样")

    def test_specs_freight_exception_is_limited_to_exact_key(self):
        specs = copy.deepcopy(self.rows[0]["specs"])
        specs["运费门槛"] = "99 元"
        changed = copy.deepcopy(self.raw)
        changed[0]["specs"] = json.dumps(specs, ensure_ascii=False)
        self.assertEqual(len(products.read_products(self.write_csv(changed))), 50)
        for key in ("售价", "库存", "普通参数", "运费门槛说明"):
            specs = copy.deepcopy(self.rows[0]["specs"])
            specs[key] = "99 元"
            self.invalid("specs", json.dumps(specs, ensure_ascii=False), "禁止字样")

    def test_generate_structure_fields_catalog_and_four_legacy_regressions(self):
        out = self.folder / "generated"
        self.assertEqual(products.generate(self.rows, out), 50)
        self.assertEqual(len(list(out.glob("*.md"))), 52)
        documents = {}
        for row in self.rows:
            text = (out / f"product_{row['sku_id']}.md").read_text(encoding="utf-8")
            documents[row["sku_id"]] = text
            self.assertTrue(text.startswith("# 灵码" + row["name"] + "\n"))
            for heading in ("## 参数表", "### 参数口径与限制", "## 使用与口径", "## 与相关款比较", "### 保养与售后"):
                self.assertIn(heading, text)
            for field in ("positioning", "package_contents", "not_included", "usage_notes", "caveats"):
                self.assertIn(row[field], text)
            self.assertEqual(products.parameter_table(text), row["specs"])
            for related in row["related_skus"].split(";") if row["related_skus"] else ():
                self.assertIn(related, text)
        products.check_legacy(documents)
        catalog = (out / "product_catalog.md").read_text(encoding="utf-8")
        self.assertIn("## 你这里有什么商品？你们卖什么？", catalog)
        self.assertIn("目前资料收录的商品为以上 50 款", catalog)
        self.assertNotIn("4 款", catalog)
        for category in products.REQUIRED_SPECS:
            self.assertIn("## " + category, catalog)
        for row in self.rows:
            self.assertIn(row["sku_id"], catalog)
            self.assertIn(row["positioning"], catalog)

    def test_each_original_parameter_is_protected_including_units_and_negations(self):
        by_id = {row["sku_id"]: row for row in self.rows}
        documents = {sku: products.render_product(row, by_id) for sku, row in by_id.items()}
        for sku, filename in products.LEGACY.items():
            original = (products.ROOT / "data/docs" / filename).read_text(encoding="utf-8")
            for key, value in products.parameter_table(original).items():
                changed = dict(documents)
                changed[sku] = changed[sku].replace(f"| {key} | {value} |", f"| {key} | 错误单位与参数 |")
                with self.subTest(sku=sku, key=key), self.assertRaisesRegex(ValueError, sku + " / " + key):
                    products.check_legacy(changed)

    def test_failed_regression_does_not_write_and_reports_expected_actual(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["specs"]["单次音乐续航"] = "80 分钟"
        out = self.folder / "failed"
        with self.assertRaisesRegex(ValueError, "原值=8 小时；生成值=80 分钟"):
            products.generate(rows, out)
        self.assertFalse(out.exists())
        with self.assertRaisesRegex(ValueError, "缺少原有 SKU"):
            products.generate(rows[1:], out)

    def test_original_negative_statement_cannot_be_removed(self):
        by_id = {row["sku_id"]: row for row in self.rows}
        documents = {sku: products.render_product(row, by_id) for sku, row in by_id.items()}
        documents["tinglan-s1"] = documents["tinglan-s1"].replace("本商品不是医疗器械，不承诺改善听力", "")
        with self.assertRaisesRegex(ValueError, "原有限制缺失"):
            products.check_legacy(documents)

        documents = {sku: products.render_product(row, by_id) for sku, row in by_id.items()}
        documents["muche-c1"] = documents["muche-c1"].replace("# 灵码木澈 C1 保温杯", "# 灵码木澈 C9 保温杯")
        with self.assertRaisesRegex(ValueError, "原商品名称"):
            products.check_legacy(documents)

    def test_cannot_generate_into_original_docs(self):
        for out in (products.ROOT / "data/docs", products.ROOT / "data/docs/nested"):
            with self.subTest(out=out), self.assertRaisesRegex(ValueError, "不得覆盖"):
                products.generate(self.rows, out)

    def test_warranty_is_selected_by_category_and_fixed_policy_occurs_once(self):
        by_id = {row["sku_id"]: row for row in self.rows}
        for row in self.rows:
            text = products.render_product(row, by_id)
            tail = text.split("### 保养与售后", 1)[1]
            with self.subTest(sku=row["sku_id"]):
                self.assertIn(products.WARRANTIES[row["category"]], tail)
                self.assertEqual(text.count(products.RETURN_POLICY), 1)
                if row["category"] in ("保温杯", "收纳包"):
                    self.assertNotIn("12 个月", tail)
                    self.assertIn("制造缺陷保障期为 6 个月", tail)
                    self.assertIn("等正常磨损部件不作永久更换承诺。", tail)
                    parts = ("密封圈",) if row["category"] == "保温杯" else ("拉链", "五金")
                    for part in parts:
                        self.assertIn(part, tail)
                else:
                    self.assertIn("自签收起 12 个月有限保修，针对正常使用下确认的非人为功能故障。", tail)

    def test_old_cup_warranty_migration_still_protects_replacement_and_other_limits(self):
        by_id = {row["sku_id"]: row for row in self.rows}
        documents = {sku: products.render_product(row, by_id) for sku, row in by_id.items()}
        baseline = self.folder / "legacy"
        baseline.mkdir()
        old, new = next(iter(products.LEGACY_WARRANTY_REPLACEMENTS.items()))
        for sku, filename in products.LEGACY.items():
            text = documents[sku].replace(new, old) if sku == "muche-c1" else documents[sku]
            (baseline / filename).write_text(text, encoding="utf-8")
        products.check_legacy(documents, baseline)
        for restriction in (new, "密封圈可拆下清洗并完全晾干后装回，不使用强腐蚀清洁剂"):
            changed = dict(documents)
            changed["muche-c1"] = changed["muche-c1"].replace(restriction, "")
            with self.subTest(restriction=restriction), self.assertRaisesRegex(ValueError, "原有限制缺失"):
                products.check_legacy(changed, baseline)

    def test_image_prompts_cover_all_skus_and_style(self):
        out = self.folder / "prompts.md"
        products.image_prompts(self.rows, out)
        text = out.read_text(encoding="utf-8")
        for phrase in ("干净浅灰背景", "柔和棚拍光", "产品居中", "1:1", "无文字", "无水印", "无品牌 logo", "#E07A3F"):
            self.assertIn(phrase, text)
        for row in self.rows:
            self.assertIn("## " + row["sku_id"], text)
            self.assertIn(row["image_main"], text)
            self.assertIn(row["image_alt"], text)

    def test_placeholders_are_valid_svg_escaped_and_do_not_overwrite_real_images(self):
        images = self.folder / "images"
        rows = copy.deepcopy(self.rows)
        rows[0]["name"] = '耳机 <&> "示意"'
        rows[0]["image_alt"] = '外观 <图> & "描述" 与 \'单引号\''
        self.assertEqual(products.placeholders(rows, images), 50)
        colors = set()
        for row in rows:
            folder = images / row["sku_id"]
            tree = ElementTree.fromstring((folder / "main.svg").read_text(encoding="utf-8"))
            self.assertEqual(tree.attrib["width"], "800")
            self.assertTrue((folder / "main.placeholder").exists())
            self.assertIn(row["name"], "".join(tree.itertext()))
            colors.add(tree[0].attrib["fill"])
        raw_svg = (images / rows[0]["sku_id"] / "main.svg").read_bytes()
        self.assertIn('耳机 &lt;&amp;&gt; &quot;示意&quot;'.encode("utf-8"), raw_svg)
        self.assertIn('aria-label="外观 &lt;图&gt; &amp; &quot;描述&quot; 与 &#x27;单引号&#x27;"'.encode("utf-8"), raw_svg)
        self.assertNotIn(rows[0]["name"].encode("utf-8"), raw_svg)
        self.assertNotIn(rows[0]["image_alt"].encode("utf-8"), raw_svg)
        self.assertEqual(len(colors), 9)
        folder = images / rows[0]["sku_id"]
        (folder / "main.webp").write_bytes(b"existing real image")
        (folder / "main.placeholder").unlink()
        self.assertEqual(products.placeholders(rows, images), 49)
        self.assertEqual((folder / "main.webp").read_bytes(), b"existing real image")
        self.assertFalse((folder / "main.placeholder").exists())

    def test_images_build_has_clear_optional_pillow_error(self):
        with patch.object(products.importlib, "import_module", side_effect=ModuleNotFoundError("PIL")):
            with self.assertRaisesRegex(RuntimeError, "Pillow.*python -m pip install Pillow"):
                products.images_build(self.rows, self.folder / "src", self.folder / "images")

    def test_validation_cli_errors_use_nonzero_status(self):
        path = self.write_csv([])
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(products.main(["--csv", str(path), "validate"]), 1)
        self.assertIn("CSV 不能为空", stderr.getvalue())
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(products.main(["--csv", str(self.write_csv()), "validate"]), 0)
        self.assertIn("50 款", stdout.getvalue())

    def test_no_battery_variants_reject_positive_battery_language(self):
        for description in ("无电池", "不含电池", "无内置电池", "没有内置电池"):
            rows = copy.deepcopy(self.raw)
            row = next(row for row in rows if row["sku_id"] == "qiguang-l2")
            specs = json.loads(row["specs"])
            specs["电池与联网"] = description + "，不支持联网控制"
            row["specs"] = json.dumps(specs, ensure_ascii=False)
            row["caveats"] = "满电后连续照明 8 小时。"
            with self.subTest(description=description), self.assertRaisesRegex(ValueError, "供电/连接规格矛盾"):
                products.read_products(self.write_csv(rows))
        for field in products.CAPABILITY_FIELDS:
            for term in ("电池", "续航", "满电", "充电宝", "充电", "补电"):
                with self.subTest(field=field, term=term):
                    self.invalid_sku("qiguang-l2", field, "本款提供" + term + "功能。", "供电/连接规格矛盾")

    def test_explicit_negations_are_exempt_but_do_not_hide_positive_next_clause(self):
        text = "本款没有内置电池，不支持充电，无续航。"
        for field in products.CAPABILITY_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(len(products.read_products(self.change_sku("qiguang-l2", field, text))), 50)
        self.invalid_sku("qiguang-l2", "usage_notes", "本款不含电池，但续航达到 8 小时。", "续航")
        self.invalid_sku("qiguang-l2", "caveats", "不承诺所有场景达到电池满电续航。", "供电/连接规格矛盾")

    def test_chargers_allow_external_charging_but_reject_storage_talk(self):
        for sku in ("hengdian-a1", "hengdian-a2", "hengdian-a3"):
            with self.subTest(sku=sku):
                self.assertEqual(len(products.read_products(self.change_sku(sku, "usage_notes", "给外设充电前确认输入电压与接口。"))), 50)
                for term in ("电芯", "容量", "续航", "充电宝"):
                    self.invalid_sku(sku, "caveats", "请核对本机" + term + "参数。", "供电/连接规格矛盾")

    def test_wired_keyboards_and_mouse_reject_wireless_or_charging_text(self):
        for sku in ("jianxing-k1", "jianxing-k3", "jianxing-m3"):
            for field in products.CAPABILITY_FIELDS:
                with self.subTest(sku=sku, field=field):
                    self.invalid_sku(sku, field, "使用蓝牙配对与无线接收器并为电池充电。", "供电/连接规格矛盾")

    def test_non_rechargeable_mouse_weight_does_not_mean_battery_free(self):
        row = next(row for row in self.rows if row["sku_id"] == "jianxing-m1")
        self.assertIn("不含电池", row["specs"]["重量"])
        self.assertIn("AA 电池", row["usage_notes"])
        self.assertEqual(products.capability_errors(row), [])
        self.assertNotIn("C 型线", row["package_contents"])
        self.invalid_sku("jianxing-m1", "package_contents", "鼠标、无线接收器、C 型线", "不可充无线款")
        self.invalid_sku("jianxing-m1", "usage_notes", "请用 C 型口充电。", "不可充款")

    def test_unprotected_products_reject_positive_water_claims(self):
        for sku in ("tinglan-m1", "minglan-d1", "minglan-d2", "minglan-a1"):
            for claim in ("防泼溅", "防水", "IPX4"):
                with self.subTest(sku=sku, claim=claim):
                    self.invalid_sku(sku, "caveats", "本款具备" + claim + "能力。", "防护表述与 specs 不一致")
        self.assertEqual(len(products.read_products(self.change_sku("minglan-d1", "caveats",
            "未标注防护等级，不承诺防水，也不具备防泼溅能力。"))), 50)

    def test_water_grade_and_case_claims_must_match_component_specs(self):
        self.invalid_sku("minglan-p2", "caveats", "本款具备 IPX7 防护。", "防护表述与 specs 不一致")
        self.invalid_sku("tinglan-k1", "usage_notes", "充电盒具备 IPX5 防水能力。", "防护表述与 specs 不一致")
        self.assertEqual(len(products.read_products(self.change_sku("tinglan-k1", "caveats",
            "耳机 IPX5，充电盒不防水，擦干后再放回。"))), 50)

    def test_similar_copy_rejected_and_all_category_pairs_are_distinct(self):
        reference = next(row for row in self.rows if row["sku_id"] == "muche-c2")
        for extra in ("", "使用温水清洗。"):
            rows = copy.deepcopy(self.raw)
            row = next(row for row in rows if row["sku_id"] == "muche-c5")
            row["usage_notes"] = reference["usage_notes"] + extra
            row["caveats"] = reference["caveats"]
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "文案相似度.*超过"):
                products.read_products(self.write_csv(rows))
        for left, right, score in products.similar_pairs(self.rows):
            with self.subTest(left=left, right=right):
                self.assertLessEqual(score, 0.85)
        self.assertEqual(products.text_similarity("完全相同的使用说明", "完全相同的使用说明"), 1)
        self.assertLess(products.text_similarity("使用夹座固定桌面台灯", "旋紧保温杯的密封杯盖"), 0.85)

    def test_nonlegacy_spec_keys_are_aligned_and_supplementary_keys_required(self):
        by_category = {}
        for row in self.rows:
            if row["sku_id"] in products.LEGACY:
                continue
            category = row["category"]
            self.assertEqual(set(row["specs"]), set(products.required_specs(row)))
            if category in by_category:
                self.assertEqual(set(row["specs"]), by_category[category])
            by_category[category] = set(row["specs"])
        for category, keys in products.SUPPLEMENTAL_SPECS.items():
            row = next(row for row in self.rows if row["category"] == category and row["sku_id"] not in products.LEGACY)
            for key in keys:
                specs = dict(row["specs"])
                del specs[key]
                with self.subTest(category=category, key=key):
                    self.invalid_sku(row["sku_id"], "specs", specs, "缺少必填键")
        self.assertNotIn("补充规格", next(row for row in self.rows if row["sku_id"] == "qiguang-l1")["specs"])
        self.assertNotIn("杯口内径", next(row for row in self.rows if row["sku_id"] == "muche-c1")["specs"])
        cup = next(row for row in self.rows if row["sku_id"] == "muche-c2")
        self.invalid_sku("muche-c2", "specs", {**cup["specs"], "清洁补充": "软布擦拭"}, "未声明的补充键")

    def test_generated_warranty_changes_only_scope_and_follows_csv_categories(self):
        original = (products.ROOT / "data/docs/after_sales_warranty.md").read_text(encoding="utf-8")
        text = products.render_warranty(self.rows)
        self.assertEqual(text.split("\n\n", 2)[0], original.split("\n\n", 2)[0])
        # 独立冻结提升前正文，避免与已覆盖的线上文件互相验证。
        baseline = (Path(__file__).parent / "fixtures_data/after_sales_warranty_body.md").read_text(encoding="utf-8")
        self.assertEqual(text.split("\n\n", 2)[2], baseline)
        self.assertEqual(original.split("\n\n", 2)[2], baseline)
        scope = text.split("\n\n", 2)[1]
        for category in products.REQUIRED_SPECS:
            self.assertIn(products.POLICY_LABELS.get(category, category), scope)
        self.assertIn("自签收起 12 个月有限保修", scope)
        self.assertIn("保温杯、收纳包按 6 个月的制造缺陷保障", scope)
        self.assertIn("密封圈/拉链/五金等正常磨损部件不作永久更换承诺", scope)
        self.assertIn(products.RETURN_POLICY, text)
        subset = [row for row in self.rows if row["category"] == "收纳包"]
        smaller_scope = products.render_warranty(subset).split("\n\n", 2)[1]
        self.assertIn("收纳包按 6 个月", smaller_scope)
        self.assertNotIn("12 个月", smaller_scope)
        self.assertNotIn("保温杯", smaller_scope)

    def test_empty_related_skus_render_without_empty_string_assertion(self):
        rows = products.read_products(self.change_sku("qiguang-l2", "related_skus", ""))
        by_id = {row["sku_id"]: row for row in rows}
        text = products.render_product(by_id["qiguang-l2"], by_id)
        self.assertNotIn("对照灵码", text)
        self.assertIn("## 与相关款比较", text)


if __name__ == "__main__":
    unittest.main()
