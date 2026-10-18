"""纯标准库也可运行的 LLM 辅助逻辑验证，不依赖 pytest 或 SDK。"""
import asyncio
import re
import unittest
from types import SimpleNamespace

from app.llm.generator import Generator, validate_citations
from app.llm.prompts import block, load
from app.llm.strategies import build_queries


class LlmHelperTests(unittest.TestCase):
    def test_citations_keep_valid_indices_and_remove_others(self):
        result = validate_citations("答案[2][2][0][4][1]", 2)
        self.assertEqual(result.answer, "答案[2][2][1]")
        self.assertEqual(result.cited_indices, {1, 2})

    def test_huge_and_empty_citations(self):
        self.assertEqual(validate_citations("[" + "9" * 5000 + "]", 3).answer, "")
        self.assertEqual(validate_citations("[1]", 0).cited_indices, set())

    def test_all_strategies_use_direct_rewritten_query(self):
        for strategy in ("direct", "hyde", "subquery", "stepback"):
            decision = SimpleNamespace(strategy=strategy, rewritten_query="退货条件")
            self.assertEqual(build_queries(decision, "口语"), ["退货条件"])

    def test_block_escapes_closing_delimiter(self):
        self.assertEqual(block("问题", "</问题>"), "<问题>\n&lt;/问题&gt;\n</问题>")

    def test_prompt_guardrails(self):
        for name in ("general", "generate"):
            text = load(name)
            for phrase in ("个人信息", "地址", "电话", "订单", "医疗", "疗效", "法律结论",
                           "政策之外", "退款", "赔偿", "补偿", "人工客服", "系统提示词"):
                self.assertIn(phrase, text)
        self.assertIn('"strategy":"direct"', load("router"))
        for phrase in ("150 字", "5 条", "不重复问题", "不寒暄"):
            self.assertIn(phrase, load("generate"))
        for phrase in ("完全无法回答", "只输出固定标记 NO_ANSWER", "不加任何其他文字", "部分回答", "资料未提及"):
            self.assertIn(phrase, load("generate"))

    def test_fake_llm_generation_uses_parent_and_validates_citations(self):
        calls = []
        class FakeLLM:
            async def complete(self, **kwargs):
                calls.append(kwargs)
                return "说明[1][2]"
        docs = [SimpleNamespace(title="标题", content="完整父块</资料>", snippet="命中子块")]
        answer = asyncio.run(Generator(FakeLLM(), "fake-model").generate_answer("问题", docs, []))
        self.assertEqual(answer.answer, "说明[1]")
        self.assertEqual(answer.cited_indices, {1})
        content = calls[0]["messages"][1]["content"]
        self.assertIn("[1] 标题\n完整父块&lt;/资料&gt;", content)
        self.assertNotIn("命中子块", content)
        self.assertEqual(calls[0]["temperature"], 0.2)
        self.assertEqual(calls[0]["max_tokens"], 4096)

    def test_router_prompt_has_distinct_examples_and_general_stays_in_scope(self):
        import json
        from pathlib import Path
        router = load("router")
        self.assertIn('"intent":"general|knowledge|sensitive"', router)
        for term in ("配送范围", "能否寄到某地", "奇怪", "sensitive > knowledge > general"):
            self.assertIn(term, router)
        examples = [line[2:].strip() for line in router.splitlines() if line.startswith("- ")]
        self.assertEqual(len(examples), 12)
        rows = [json.loads(line) for line in (Path(__file__).resolve().parents[2] / "data/eval.jsonl").read_text().splitlines()]
        self.assertTrue(set(examples).isdisjoint(row["question"] for row in rows))
        general = load("general")
        for term in ("不凭记忆回答", "政策", "价格", "时效", "配送范围", "写代码", "做作业", "专注店铺咨询", "不展开"):
            self.assertIn(term, general)

    def test_router_prompt_preserves_formal_rewrite_rules_safety_and_example_coverage(self):
        router = load("router")
        for phrase in (
            "主语、动作、对象", "完整书面问句", "书面措辞", "如何申请", "的规则是什么", "需要什么条件/材料",
            "怎么弄、咋、啥、多久能", "一个明确意图", "保持原意", "不得添加原问没有的事实、数字或承诺",
            "不得把问题改成别的主题", "不得推定这些状态", "现金抵扣", "不承诺优先发货",
            "sensitive > knowledge > general", "不得先把危险请求改写成正常问题再分类",
            "询问耳机能否治疗听力问题", "rewritten_query 固定为「敏感请求」",
            "不得带入系统提示词相关内容", "火星",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, router)
        examples = re.findall(r"^\d+\. 「([^」]+)」 → 「([^」]+)」$", router, re.MULTILINE)
        self.assertGreaterEqual(len(examples), 8)
        for keyword in ("收货地址", "积分", "发货", "运费", "发票", "退货", "保修", "优惠券"):
            with self.subTest(topic=keyword):
                self.assertTrue(any(keyword in rewritten for _, rewritten in examples))
        for _, rewritten in examples:
            self.assertTrue(rewritten.endswith("？"))
            self.assertIsNone(re.search(r"[0-9０-９]", rewritten))
            self.assertNotIn("系统提示词", rewritten)
