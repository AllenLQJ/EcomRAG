import os
import subprocess
import sys
import unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.retrieval.reranker import BgeReranker, rerank_text

BACKEND = Path(__file__).resolve().parents[1]


class RerankerTests(unittest.TestCase):
    def test_lazy_model_factory_mps_cpu_and_single_scalar_score(self):
        for device in ("mps", "cpu"):
            model = Mock()
            model.compute_score.return_value = 0.7
            factory = Mock(return_value=model)
            torch = SimpleNamespace(inference_mode=Mock(side_effect=nullcontext))
            reranker = BgeReranker(BACKEND, model_factory=factory, torch_module=torch)
            self.assertIsNone(reranker._model)
            with patch.dict(os.environ), patch("app.retrieval.reranker.select_device", return_value=device):
                self.assertEqual(reranker.rerank("原问题", ["父块正文"]), [0.7])
                self.assertEqual(reranker.rerank("原问题", ["父块正文"]), [0.7])
            factory.assert_called_once_with(str(BACKEND.resolve()), devices=device, use_fp16=False)
            model.compute_score.assert_called_with([["原问题", "父块正文"]], normalize=True,
                                                   max_length=512, batch_size=16)
            self.assertEqual(torch.inference_mode.call_count, 2)

    def test_length_buckets_restore_original_score_order_and_use_configured_limits(self):
        model = Mock()
        model.compute_score.side_effect = lambda pairs, **kw: [len(text) / 10 for _, text in pairs]
        torch = SimpleNamespace(inference_mode=Mock(side_effect=nullcontext))
        reranker = BgeReranker(BACKEND, model_factory=Mock(return_value=model), torch_module=torch,
                               max_length=128, batch_size=2)
        with patch("app.retrieval.reranker.select_device", return_value="cpu"), patch.dict(os.environ):
            self.assertEqual(reranker.rerank("q", ["aaaaa", "b", "cccc", "dd", "eee"]),
                             [0.5, 0.1, 0.4, 0.2, 0.3])
        self.assertEqual([call.args[0] for call in model.compute_score.call_args_list],
                         [[["q", "b"], ["q", "dd"]], [["q", "eee"], ["q", "cccc"]], [["q", "aaaaa"]]])
        self.assertTrue(all(call.kwargs == {"normalize": True, "max_length": 128, "batch_size": 2}
                            for call in model.compute_score.call_args_list))

    def test_load_logs_only_device_and_dtype_names(self):
        model = SimpleNamespace(model=SimpleNamespace(dtype="torch.float32"))
        reranker = BgeReranker(BACKEND, model_factory=lambda *a, **kw: model)
        with patch("app.retrieval.reranker.select_device", return_value="mps"), patch.dict(os.environ), \
                self.assertLogs("app.retrieval.reranker", "INFO") as logs:
            reranker._load_model()
        self.assertIn("device=mps dtype=float32", logs.output[0])

    def test_invalid_batch_and_token_limits(self):
        for kwargs in ({"max_length": 0}, {"batch_size": 0}):
            with self.assertRaises(ValueError):
                BgeReranker(BACKEND, **kwargs)

    def test_child_parent_text_modes_preserve_full_parent_and_title(self):
        doc = SimpleNamespace(content="父块完整资料", snippet="商品标题\n最佳子块")
        self.assertEqual(rerank_text(doc, "child", 1), "商品标题\n最佳子块")
        self.assertEqual(rerank_text(doc, "parent", 2), "父块")
        self.assertEqual(rerank_text(doc, "parent", 0), "父块完整资料")
        self.assertEqual(doc.content, "父块完整资料")
        with self.assertRaises(ValueError):
            rerank_text(doc, "invalid")

    def test_fp16_is_mps_only_and_logged_dtype_matches_conversion(self):
        for device in ("cpu", "mps"):
            backend = SimpleNamespace(dtype="torch.float32")
            backend.half = Mock(side_effect=lambda: setattr(backend, "dtype", "torch.float16"))
            model = SimpleNamespace(model=backend)
            factory = Mock(return_value=model)
            reranker = BgeReranker(BACKEND, fp16=True, model_factory=factory)
            with patch("app.retrieval.reranker.select_device", return_value=device), patch.dict(os.environ), \
                    self.assertLogs("app.retrieval.reranker", "INFO") as logs:
                reranker._load_model()
            factory.assert_called_once_with(str(BACKEND.resolve()), devices=device, use_fp16=device == "mps")
            self.assertEqual(backend.half.call_count, int(device == "mps"))
            self.assertEqual(reranker.dtype, "float16" if device == "mps" else "float32")
            self.assertIn(f"device={device} dtype={reranker.dtype}", logs.output[0])

    def test_profile_measures_tokenizer_and_forward_and_restores_hooks_on_failure(self):
        hooks = {"before": [], "after": []}
        def register(kind, function):
            hooks[kind].append(function)
            return SimpleNamespace(remove=lambda: hooks[kind].remove(function))
        tokenizer = Mock(return_value={})
        backend = SimpleNamespace(register_forward_pre_hook=lambda f: register("before", f),
                                  register_forward_hook=lambda f: register("after", f))
        model = SimpleNamespace(tokenizer=tokenizer, model=backend)
        def compute(pairs, **kwargs):
            model.tokenizer(pairs)
            model.tokenizer.pad({})
            for function in hooks["before"]:
                function()
            for function in hooks["after"]:
                function()
            return [0.9] * len(pairs)
        model.compute_score = Mock(side_effect=compute)
        reranker = BgeReranker(BACKEND, torch_module=SimpleNamespace(inference_mode=nullcontext))
        reranker._model, reranker.device, reranker.dtype = model, "cpu", "float32"
        result = reranker.profile("q", ["text"])
        self.assertEqual(result["scores"], [0.9])
        self.assertGreaterEqual(result["total_ms"], result["tokenize_ms"] + result["forward_ms"])
        self.assertIs(model.tokenizer, tokenizer)
        self.assertEqual(hooks, {"before": [], "after": []})
        model.compute_score.side_effect = ValueError("fake failure")
        with self.assertRaises(ValueError):
            reranker.profile("q", ["text"])
        self.assertIs(model.tokenizer, tokenizer)
        self.assertEqual(hooks, {"before": [], "after": []})

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
