import contextlib
import io
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from eval.profile_rerank import main, profile, print_report, compare_precision, print_precision
from eval.metrics import percentile


class FakeProfiler:
    def __init__(self):
        self.calls = []

    def profile(self, question, texts, *, batch_size):
        self.calls.append((question, list(texts), batch_size))
        return dict(scores=[len(text) for text in texts], device="cpu", dtype="float32",
                    tokenize_ms=1, forward_ms=2, total_ms=len(self.calls))


class ProfileRerankTests(unittest.TestCase):
    def test_grid_warmup_lengths_and_top1_baseline(self):
        candidates = [SimpleNamespace(doc_id=f"d{i}", content="x" * (40 * (i + 1))) for i in range(20)]
        fake = FakeProfiler()
        report = profile("问题", candidates, fake)
        self.assertEqual(len(report["rows"]), 27)
        self.assertEqual(len(fake.calls), 54)
        self.assertEqual(report["baseline_top1_doc_id"], "d19")
        self.assertEqual([row["total_ms"] for row in report["rows"]], list(range(2, 55, 2)))
        first = report["rows"][0]
        self.assertEqual((first["length_min"], first["length_median"], first["length_max"]), (42, 422, 802))
        self.assertEqual((first["tokenize_ms"], first["forward_ms"]), (1, 2))
        self.assertTrue(all(row["top1_matches_baseline"] for row in report["rows"][:3]))
        self.assertTrue(all(not row["top1_matches_baseline"] for row in report["rows"][9:]))
        for row, call in zip(report["rows"], fake.calls[1::2]):
            self.assertEqual(len(call[1]), row["candidates"])
            self.assertEqual(call[2], row["batch_size"])
            self.assertTrue(all(not row["max_chars"] or len(text) <= row["max_chars"] for text in call[1]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_report(report)
        self.assertIn("tokenize_ms forward_ms total_ms", output.getvalue())
        self.assertIn("丢弃首次预热", output.getvalue())

    def test_cli_loads_runtime_settings_with_injected_offline_dependencies(self):
        settings = SimpleNamespace(rerank_max_length=256, rerank_candidates=10, rerank_text_mode="child",
                                   rerank_max_chars=600, rerank_batch_size=16)
        getter = Mock(return_value=settings)
        retriever = Mock()
        retriever.retrieve_candidates.return_value = [SimpleNamespace(doc_id="d", content="正文", snippet="标题\n子块")]
        output = io.StringIO()
        with patch.dict(sys.modules, {"app.config": SimpleNamespace(get_settings=getter)}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["问题"], retriever=retriever, reranker=FakeProfiler()), 0)
        getter.assert_called_once_with()
        retriever.retrieve_candidates.assert_called_once_with(["问题"])
        retriever.client.close.assert_not_called()
        self.assertIn("RERANK_MAX_LENGTH=256", output.getvalue())

    def test_no_candidates_and_invalid_scores(self):
        retriever = Mock()
        retriever.retrieve_candidates.return_value = []
        fake = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["问题"], settings=SimpleNamespace(), retriever=retriever, reranker=fake), 1)
        fake.profile.assert_not_called()
        fake.profile.return_value = {"scores": []}
        with self.assertRaises(ValueError):
            profile("q", [SimpleNamespace(content="text", doc_id="d")], fake)

    def test_percentiles_interpolate_and_handle_single_or_empty_samples(self):
        self.assertEqual(percentile([10, 0, 20], 50), 10)
        self.assertEqual(percentile([0, 10, 20], 90), 18)
        self.assertEqual(percentile([7], 90), 7)
        self.assertIsNone(percentile([], 50))
        with self.assertRaises(ValueError):
            percentile([1], 101)

    def test_precision_comparison_uses_identical_child_inputs_and_reports_score_differences(self):
        docs = [SimpleNamespace(doc_id="a", content="父块a", snippet="标题a\n子块a"),
                SimpleNamespace(doc_id="b", content="父块b", snippet="标题b\n子块b")]
        def fake(dtype, scores, forward):
            profiler = Mock()
            profiler.profile.return_value = dict(device="mps", dtype=dtype, scores=scores, forward_ms=forward)
            return profiler
        full, half = fake("float32", [0.9, 0.7], 10), fake("float16", [0.85, 0.72], 5)
        report = compare_precision("q", docs, full, lambda: half, max_chars=1, batch_size=8)
        self.assertFalse(report["skipped"])
        self.assertTrue(report["top1_matches"])
        self.assertAlmostEqual(report["score_mean_abs_diff"], 0.035)
        self.assertAlmostEqual(report["score_max_abs_diff"], 0.05)
        self.assertEqual((report["fp32_forward_ms"], report["fp16_forward_ms"]), (10, 5))
        for profiler in (full, half):
            self.assertEqual(profiler.profile.call_count, 2)
            profiler.profile.assert_called_with("q", ["标题a\n子块a", "标题b\n子块b"], batch_size=8)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_precision(report)
        self.assertIn("Top1一致=是", output.getvalue())
        self.assertIn("score_mean_abs_diff=0.035000", output.getvalue())

    def test_precision_comparison_skips_cpu_and_fp16_load_or_forward_failure_safely(self):
        docs = [SimpleNamespace(doc_id="a", content="父块", snippet="子块")]
        factory = Mock()
        report = compare_precision("q", docs, FakeProfiler(), factory)
        self.assertTrue(report["skipped"])
        self.assertIn("CPU 始终使用 fp32", report["reason"])
        factory.assert_not_called()
        full = Mock()
        full.profile.return_value = dict(device="mps", dtype="float32", scores=[0.9], forward_ms=10)
        for fails_on_load in (False, True):
            half = Mock()
            half.profile.side_effect = RuntimeError("PRIVATE_QUESTION_KEY")
            factory = Mock(side_effect=RuntimeError("PRIVATE_QUESTION_KEY")) if fails_on_load else Mock(return_value=half)
            report = compare_precision("q", docs, full, factory, text_mode="parent", max_chars=1)
            self.assertTrue(report["skipped"])
            self.assertIn("RuntimeError", report["reason"])
            self.assertNotIn("PRIVATE_QUESTION_KEY", str(report))
            full.profile.assert_called_with("q", ["父"], batch_size=16)
