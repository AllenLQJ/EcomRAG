"""响应结构与分段解析的纯标准库测试，不加载配置或 SDK。"""
import unittest

from app.llm.responses import extract_text, response_structure


class LlmResponseHelperTests(unittest.TestCase):
    def test_string_and_text_parts_preserve_only_final_answer(self):
        self.assertEqual(extract_text("  最终答案  "), "  最终答案  ")
        self.assertEqual(extract_text([
            {"type": "reasoning", "text": "PRIVATE_REASONING"},
            {"type": "text", "text": "第一段"}, {"type": "output_text", "text": "第二段"},
            {"type": "image", "url": "PRIVATE_URL"}, {"text": "第三段"},
        ]), "第一段第二段第三段")

    def test_empty_and_unsupported_content(self):
        self.assertEqual(extract_text(None), "")
        self.assertEqual(extract_text([]), "")
        with self.assertRaises(TypeError):
            extract_text({"text": "not a supported segment list"})

    def test_structure_has_only_metadata_not_text(self):
        raw = {"choices": [{"finish_reason": "length", "message": {
            "content": None, "reasoning_content": "PRIVATE_REASONING", "reasoning": "PRIVATE_REASONING"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 64,
                      "completion_tokens_details": {"reasoning_tokens": 63}}}
        info = response_structure(raw)
        self.assertEqual(info["choices_count"], 1)
        self.assertEqual(info["finish_reason"], "length")
        self.assertEqual(info["content_type"], "None")
        self.assertTrue(info["content_empty"])
        self.assertTrue(info["has_reasoning_content"] and info["has_reasoning"])
        self.assertEqual(info["usage"], {"prompt_tokens": 20, "completion_tokens": 64, "reasoning_tokens": 63})
        self.assertNotIn("PRIVATE_REASONING", str(info))

    def test_structure_handles_missing_and_malformed_choices(self):
        for raw, count in ((None, None), ({}, None), ({"choices": []}, 0),
                           ({"choices": [None]}, 1), ({"choices": [{"message": None}]}, 1)):
            with self.subTest(raw=raw):
                info = response_structure(raw)
                self.assertEqual(info["choices_count"], count)
                self.assertTrue(info["content_empty"])

    def test_untrusted_finish_and_usage_strings_are_never_logged(self):
        raw = {"choices": [{"finish_reason": "PRIVATE_ANSWER", "message": {"content": "PRIVATE_ANSWER"}}],
               "usage": {"prompt_tokens": "PRIVATE_KEY", "completion_tokens": True, "reasoning_tokens": -1}}
        info = response_structure(raw)
        self.assertEqual(info["finish_reason"], "unknown")
        self.assertEqual(info["content_type"], "str")
        self.assertFalse(info["content_empty"])
        self.assertEqual(info["usage"], {"prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None})
        self.assertNotIn("PRIVATE", str(info))

    def test_top_level_reasoning_usage_and_list_content_shape(self):
        raw = {"choices": [{"message": {"content": [{"type": "text", "text": "ANSWER"}]}}],
               "usage": {"reasoning_tokens": 0}}
        info = response_structure(raw)
        self.assertEqual(info["content_type"], "list")
        self.assertFalse(info["content_empty"])
        self.assertEqual(info["usage"]["reasoning_tokens"], 0)
        self.assertNotIn("ANSWER", str(info))
