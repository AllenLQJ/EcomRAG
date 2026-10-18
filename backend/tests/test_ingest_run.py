import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ingest.run import main

BACKEND = Path(__file__).resolve().parents[1]
SPLIT_SETTINGS = dict(
    embed_batch_size=16, parent_max_chars=1200,
    child_chunk_size=300, child_chunk_overlap=50,
)


class DryRunTests(unittest.TestCase):
    def test_dry_run_without_optional_packages_or_network(self):
        blocked = {name: None for name in (
            "FlagEmbedding", "torch", "pymilvus", "pymysql", "pymupdf", "docx",
            "langchain_text_splitters", "pydantic", "pydantic_settings",
        )}
        output = io.StringIO()
        with patch.dict(sys.modules, blocked), patch("socket.socket", side_effect=AssertionError("禁止网络")):
            with contextlib.redirect_stdout(output):
                result = main(["--dir", str(BACKEND.parent / "data/docs"), "--dry-run"],
                              settings=SimpleNamespace(**SPLIT_SETTINGS))
        self.assertEqual(result, 0)
        stats = json.loads(output.getvalue())
        self.assertEqual(stats["documents"], 18)
        # 新增屏幕划痕小节后，ingest_directory(data/docs, dry_run=True) 实测 37/76；
        # 默认参数：父块 1200、子块 300、重叠 50，按 ## 整节切分。
        self.assertEqual(stats["parent_chunks"], 37)
        self.assertEqual(stats["child_chunks"], 76)
        self.assertGreaterEqual(stats["elapsed_s"], 0)
        self.assertTrue(stats["dry_run"])

    def test_cli_relative_path_with_mocked_settings_and_no_dotenv_reads(self):
        # 默认 CLI 调用配置加载器，但在测试中替换它，审计钩子禁止访问真实 .env。
        code = '''import os, runpy, sys, types
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = os.path.basename(os.fsdecode(args[0]))
        if name == ".env" or name.startswith(".env."):
            raise AssertionError("禁止读取环境文件")
sys.addaudithook(audit)
config = types.ModuleType("app.config")
calls = []
def get_settings():
    calls.append(True)
    return types.SimpleNamespace(embed_batch_size=16, parent_max_chars=1200, child_chunk_size=300, child_chunk_overlap=50)
config.get_settings = get_settings
sys.modules["app.config"] = config
sys.argv = ["ingest.run", "--dir", "data/docs", "--dry-run"]
try:
    runpy.run_module("ingest.run", run_name="__main__")
finally:
    assert len(calls) == 1, "CLI 必须通过 get_settings 加载配置"
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(result.stdout)
        # 同上：真实样例与默认切块参数的基准统计。
        self.assertEqual((stats["documents"], stats["parent_chunks"], stats["child_chunks"]), (18, 37, 76))

    def test_missing_directory_returns_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = main(["--dir", "missing-ingest-documents", "--dry-run"],
                          settings=SimpleNamespace(**SPLIT_SETTINGS))
        self.assertEqual(result, 1)
        self.assertIn("目录不存在", output.getvalue())
