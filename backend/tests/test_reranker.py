import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.retrieval.reranker import BgeReranker

BACKEND = Path(__file__).resolve().parents[1]


class RerankerTests(unittest.TestCase):
    def test_lazy_model_factory_mps_cpu_and_single_scalar_score(self):
        for device in ("mps", "cpu"):
            model = Mock()
            model.compute_score.return_value = 0.7
            factory = Mock(return_value=model)
            reranker = BgeReranker(BACKEND, model_factory=factory)
            self.assertIsNone(reranker._model)
            with patch.dict(os.environ), patch("app.retrieval.reranker.select_device", return_value=device):
                self.assertEqual(reranker.rerank("原问题", ["父块正文"]), [0.7])
                self.assertEqual(reranker.rerank("原问题", ["父块正文"]), [0.7])
            factory.assert_called_once_with(str(BACKEND.resolve()), devices=device, use_fp16=False)
            model.compute_score.assert_called_with([["原问题", "父块正文"]], normalize=True)

    def test_empty_input_and_missing_local_model_never_load_or_download(self):
        reranker = BgeReranker(BACKEND / "missing-reranker-model")
        self.assertEqual(reranker.rerank("问题", []), [])
        with self.assertRaisesRegex(FileNotFoundError, "不会下载模型"):
            reranker.rerank("问题", ["正文"])

    def test_model_independent_modules_import_without_optional_dependencies(self):
        code = '''import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"torch", "FlagEmbedding", "pymilvus", "pymysql", "redis", "rank_bm25", "jieba"}:
            raise AssertionError("禁止导入可选依赖：" + fullname)
sys.meta_path.insert(0, Block())
import app.faq.repo, app.faq.cache, app.faq.bm25
import app.retrieval.embedder, app.retrieval.store, app.retrieval.reranker
print("imports OK")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("imports OK", result.stdout)
