"""引用对齐的纯标准库测试，可独立运行，不加载配置、模型或网络。"""
import unittest

from app.citations import align_citations, best_source, source_snippet
from app.llm.generator import validate_citations


class CitationAlignmentTests(unittest.TestCase):
    def test_repeated_first_source_only_survives_where_it_supports_the_claim(self):
        contents = ["签收后七天内可申请退货。", "退款会在三个工作日内原路到账。", "台灯支持三档色温。"]
        result, claims = align_citations(
            "签收后七天内可申请退货[1]。\n退款在三个工作日内原路到账[1, 2]。\n台灯支持三档色温[1, 3]。",
            contents)
        self.assertEqual(result.answer,
                         "签收后七天内可申请退货[1]。\n退款在三个工作日内原路到账[2]。\n台灯支持三档色温[3]。")
        self.assertEqual(result.cited_indices, {1, 2, 3})
        self.assertEqual(len(claims[1]), 1)

    def test_unrelated_valid_number_removed_without_triggering_fallback(self):
        result, _ = align_citations("退款在三个工作日内到账[1][2]。", ["耳机采用蓝牙连接。", "退款在三个工作日内到账。"])
        self.assertEqual(result.answer, "退款在三个工作日内到账[2]。")
        self.assertEqual(result.cited_indices, {2})

    def test_repeated_supporting_source_is_kept_for_multiple_sentences(self):
        result, _ = align_citations("签收后七天内可退货[1]。请上传包装照片[1]。",
                                    ["签收后七天内可退货。请上传包装照片。"])
        self.assertEqual(result.answer, "签收后七天内可退货[1]。请上传包装照片[1]。")

    def test_citation_groups_belong_to_the_preceding_sentence_or_clause(self):
        for text in ("耳机支持降噪。[1][2]\n台灯支持调光。[1][2]",
                     "耳机支持降噪[1][2]，台灯支持调光[1][2]"):
            with self.subTest(text=text):
                result, _ = align_citations(text, ["耳机支持降噪。", "台灯支持调光。"])
                self.assertEqual(result.answer.count("[1]"), 1)
                self.assertEqual(result.answer.count("[2]"), 1)
                self.assertIn("降噪" + ("。" if "。" in text else "") + "[1]", result.answer)

    def test_models_and_specification_numbers_must_match(self):
        for claim in ("栖光 L2 额定功率为 6 瓦[1]", "栖光 L1 额定功率为 8 瓦[1]",
                      "栖光 L1 额定功率为 1 瓦[1]"):
            with self.subTest(claim=claim):
                result, _ = align_citations(claim, ["栖光 L1 额定功率为 6 瓦。"])
                self.assertEqual(result.cited_indices, set())
                self.assertNotIn("[1]", result.answer)

    def test_unique_model_in_heading_supports_specification_on_next_line(self):
        result, _ = align_citations("栖光 L1 额定功率为 6 瓦[1]", ["# 栖光 L1\n额定功率为 6 瓦。"])
        self.assertEqual(result.cited_indices, {1})

    def test_exact_model_or_numeric_short_answers_have_evidence(self):
        for claim, content in [("L1[1]", "栖光 L1 台灯。"), ("4.2[1]", "重量为 4.2 克。")]:
            with self.subTest(claim=claim):
                result, _ = align_citations(claim, [content])
                self.assertEqual(result.answer, claim)
                self.assertEqual(result.cited_indices, {1})

    def test_decimal_and_width_normalization_and_list_numbers(self):
        result, _ = align_citations("1. 栖光 Ｌ１ 重量为 4.2 克［１］。", ["栖光 L1 重量为 4.2 克。"])
        self.assertEqual(result.answer, "1. 栖光 Ｌ１ 重量为 4.2 克[1]。")

    def test_code_markers_stay_literal_and_do_not_support_citations(self):
        code = "`[1]`\n```text\n耳机支持降噪[2]\n```\n"
        result, _ = align_citations(code + "台灯支持调光[1][2]。", ["耳机支持降噪。", "台灯支持调光。"])
        self.assertEqual(result.answer, code + "台灯支持调光[2]。")
        self.assertEqual(result.cited_indices, {2})

    def test_grouped_full_width_and_out_of_range_markers(self):
        result = validate_citations("事实【１，２、３】和［２, ０, ９９］及 `[1, 2]`", 2)
        self.assertEqual(result.answer, "事实[1][2]和[2]及 `[1, 2]`")
        self.assertEqual(result.cited_indices, {1, 2})
        self.assertEqual(validate_citations("[" + "9" * 5000 + ", 2]", 2).answer, "[2]")

    def test_fallback_uses_answer_overlap_after_all_markers_removed(self):
        contents = ["台灯支持三档色温。", "退款会在三个工作日内原路到账。"]
        for answer in ("退款会在三个工作日内原路到账", "退款会在三个工作日内原路到账[1][99]"):
            with self.subTest(answer=answer):
                result, _ = align_citations(answer, contents)
                self.assertEqual(result.cited_indices, set())
                self.assertEqual(best_source(result.answer, contents), 2)
                self.assertNotIn("[", result.answer)

    def test_fallback_ties_preserve_original_order(self):
        self.assertEqual(best_source("未知信息", ["耳机", "台灯"]), 1)

    def test_snippet_selects_fact_sentence_deep_in_parent(self):
        fact = "退款会在三个工作日内原路到账。"
        content = "欢迎咨询店铺服务。" * 30 + fact + "台灯支持三档色温。" * 20
        snippet = source_snippet(content, "退款在三个工作日内原路到账", "退款什么时候到账")
        self.assertEqual(snippet, fact)
        self.assertLessEqual(len(snippet), 120)

    def test_snippet_checks_tail_of_long_sentence(self):
        fact = "退款会在三个工作日内原路到账"
        snippet = source_snippet("无关描述" * 100 + fact, fact, "退款到账")
        self.assertIn(fact, snippet)
        self.assertLessEqual(len(snippet), 120)

    def test_snippet_uses_supported_claim_over_other_answer_or_question_facts(self):
        content = "签收后七天内可申请退货。退款会在三个工作日内原路到账。"
        result, claims = align_citations("签收后七天内可申请退货[1]。退款会在三个工作日内原路到账[2]。",
                                        [content, "退款会在三个工作日内原路到账。"])
        self.assertEqual(result.cited_indices, {1, 2})
        self.assertEqual(source_snippet(content, "\n".join(claims[1]), "退款何时原路到账"), "签收后七天内可申请退货。")

    def test_empty_body_has_no_evidence_or_snippet(self):
        result, _ = align_citations("退款原路到账[1]", [""])
        self.assertEqual(result.answer, "退款原路到账")
        self.assertEqual(source_snippet("", result.answer, "退款"), "")


if __name__ == "__main__":
    unittest.main()
