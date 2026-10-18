"""纯标准库也可运行的 LLM 辅助逻辑验证，不依赖 pytest 或 SDK。"""
import asyncio
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
        self.assertEqual(calls[0]["max_tokens"], 600)

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
