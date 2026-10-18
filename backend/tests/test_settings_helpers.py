"""进程环境隔离的标准库回归测试，不导入运行时配置或环境文件。"""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from settings_helpers import APP_PREFIXES, clear_app_environment


class SettingsEnvironmentTests(unittest.TestCase):
    def test_clear_all_prefixes_and_exact_field_names_case_insensitively(self):
        polluted = {prefix.lower() + "test_option": "PRIVATE_ENV_VALUE" for prefix in APP_PREFIXES}
        polluted.update(debug="true", ALLOW_TRACE="true", CORS_ORIGINS="INVALID_ENV_VALUE")
        with patch.dict(os.environ, polluted, clear=True):
            cleanup = SimpleNamespace(delenv=lambda name, raising: os.environ.pop(name, None))
            clear_app_environment(cleanup, ("debug", "allow_trace", "cors_origins"))
            self.assertEqual(dict(os.environ), {})

    def test_preserve_unrelated_process_environment(self):
        values = {"PATH": "/test/bin", "LANG": "zh_CN.UTF-8", "UNRELATED_FLAG": "keep", "LLM_API_KEY": "PRIVATE"}
        with patch.dict(os.environ, values, clear=True):
            cleanup = SimpleNamespace(delenv=lambda name, raising: os.environ.pop(name, None))
            clear_app_environment(cleanup, ())
            self.assertEqual(dict(os.environ), {name: value for name, value in values.items() if name != "LLM_API_KEY"})
