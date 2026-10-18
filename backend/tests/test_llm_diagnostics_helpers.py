"""无需 httpx 或 pytest 的脱敏诊断测试。"""
import unittest
from types import SimpleNamespace
from urllib.parse import quote

from app.llm.diagnostics import configuration, diagnostic_info, redact, target_host

KEY = "FAKE_KEY_DO_NOT_PRINT_71"
QUESTION = "PRIVATE_CUSTOMER_28"


class DiagnosticHelperTests(unittest.TestCase):
    def test_host_never_includes_credentials_path_or_query(self):
        self.assertEqual(target_host(f"https://user:{KEY}@vendor.example/v1/{KEY}?key={KEY}", KEY), "vendor.example")
        self.assertIsNone(target_host("https://[invalid", KEY))

    def test_redact_before_truncation_and_encoded_key(self):
        secret = "secret/" + "K" * 300
        self.assertEqual(redact(secret + "/message", (secret,)), "***/message")
        self.assertEqual(redact(quote(secret, safe=""), (secret,)), "***")
        self.assertEqual(len(redact("长" * 300)), 200)

    def test_extract_only_allowed_error_fields_and_hide_prompts(self):
        response = SimpleNamespace(status_code=401, json=lambda: {"error": {
            "code": "InvalidApiKey", "message": KEY + QUESTION + "PRIVATE_PROMPT_31",
            "request": "DO_NOT_INCLUDE_OTHER_FIELDS"}, "choices": [{"message": {"content": "PRIVATE_ANSWER"}}]})
        messages = [{"role": "system", "content": "PRIVATE_PROMPT_31"},
                    {"role": "user", "content": f"<问题>\n{QUESTION}\n</问题>"}]
        info = diagnostic_info("http_status", base_url="https://vendor.example/v1", model="llm-test",
                               key=KEY, messages=messages, response=response, exception_type="HTTPStatusError")
        self.assertEqual(info["error_message"], "*********")
        self.assertEqual(info["error_code"], "InvalidApiKey")
        self.assertEqual(info["http_status"], 401)
        self.assertEqual(info["exception_type"], "HTTPStatusError")
        for secret in (KEY, QUESTION, "PRIVATE_PROMPT_31", "PRIVATE_ANSWER", "DO_NOT_INCLUDE_OTHER_FIELDS"):
            self.assertNotIn(secret, str(info))

    def test_invalid_json_does_not_log_response_body(self):
        def invalid():
            raise ValueError(KEY + QUESTION)
        info = diagnostic_info("bad_response", base_url="https://vendor.example", model="test", key=KEY,
                               messages=[], response=SimpleNamespace(status_code=200, json=invalid))
        self.assertIsNone(info["error_message"])
        self.assertNotIn(KEY, str(info))

    def test_configuration_contains_only_key_presence_and_length(self):
        settings = SimpleNamespace(llm_api_key=SimpleNamespace(get_secret_value=lambda: KEY),
                                   llm_base_url=f"https://vendor.example/v1?key={KEY}",
                                   router_model="router", generate_model="generate")
        result = configuration(settings)
        self.assertEqual(result, dict(host="vendor.example", router_model="router", generate_model="generate",
                                      key_configured=True, key_length=len(KEY)))
        self.assertNotIn(KEY, str(result))

    def test_extra_body_diagnostic_logs_only_top_level_keys(self):
        extra = {"thinking": {"type": "PRIVATE_EXTRA_VALUE"}}
        response = SimpleNamespace(status_code=400, json=lambda: {"error": {
            "code": "BadOption", "message": '{"type":"PRIVATE_EXTRA_VALUE"}'}})
        info = diagnostic_info("http_status", base_url="https://vendor.example", model="test", key=KEY,
                               messages=[], response=response, extra_body=extra)
        self.assertEqual(info["extra_body_keys"], ["thinking"])
        self.assertNotIn("PRIVATE_EXTRA_VALUE", str(info))
        self.assertEqual(info["error_message"], "***")

    def test_bad_response_structure_never_includes_provider_or_reasoning_text(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"error": {
            "message": "PRIVATE_ANSWER"}, "choices": [{"finish_reason": "length", "message": {
                "content": None, "reasoning_content": "PRIVATE_REASONING"}}]})
        info = diagnostic_info("bad_response", base_url="https://vendor.example", model="test", key=KEY,
                               messages=[], response=response)
        self.assertIsNone(info["error_message"])
        self.assertTrue(info["response_structure"]["has_reasoning_content"])
        self.assertNotIn("PRIVATE_ANSWER", str(info))
        self.assertNotIn("PRIVATE_REASONING", str(info))
