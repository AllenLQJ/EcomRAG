import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from app import config
from app.contracts import FaqHit, RetrievedDoc
from eval import check_retrieval, faq_demo, search_demo
from ingest.loaders import resolve_data_path


class EvalDemoTests(unittest.TestCase):
    def settings(self):
        return config.Settings(
            _env_file=None, bge_m3_path="models/fake-bge", reranker_path="models/fake-reranker",
            embed_batch_size=7, milvus_uri="http://127.0.0.1:19531", milvus_timeout_s=3.5,
            rerank_candidates=12,
            redis_url="redis://127.0.0.1:6380/1", faq_cache_ttl_s=60, faq_threshold=0.85,
        )

    def test_check_retrieval_loads_config_and_forces_normalized_rerank(self):
        self.assertEqual(config.Settings.model_fields["refuse_threshold"].default, 0.5)
        self.assertEqual(config.Settings.model_fields["milvus_timeout_s"].default, 10)
        settings = self.settings().model_copy(update={"refuse_threshold": 0.6, "rerank_enabled": False})
        from app.retrieval import embedder, reranker, retriever

        for injected in (False, True):
            getter = Mock(side_effect=AssertionError("不得读 .env")) if injected else Mock(return_value=settings)
            with self.subTest(injected=injected), patch.object(config, "get_settings", getter), \
                    patch.object(embedder, "BgeM3Embedder") as encoder, \
                    patch.object(reranker, "BgeReranker") as ranker, \
                    patch.object(retriever, "Retriever") as factory, \
                    patch.object(check_retrieval, "evaluate", return_value={}) as evaluate, \
                    patch.object(check_retrieval, "print_report") as report, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_retrieval.main(["--rerank-text-mode", "parent"],
                                                      **({"settings": settings} if injected else {})), 0)
            encoder.assert_called_once_with(resolve_data_path(settings.bge_m3_path), 7)
            ranker.assert_called_once_with(resolve_data_path(settings.reranker_path), max_length=512, batch_size=16, fp16=False)
            factory.assert_called_once_with(None, encoder.return_value, ranker.return_value,
                                            uri=settings.milvus_uri, timeout_s=3.5,
                                            rerank_enabled=True, rerank_candidates=12, rerank_max_chars=600,
                                            rerank_text_mode="parent")
            self.assertEqual(len(evaluate.call_args.args[0]), 24)
            self.assertIs(evaluate.call_args.args[1], factory.return_value)
            self.assertEqual(evaluate.call_args.kwargs, {"refuse_threshold": 0.6})
            report.assert_called_once_with({})
            factory.return_value.client.close.assert_called_once_with()
            getter.assert_not_called() if injected else getter.assert_called_once_with()

    def test_search_demo_loads_settings_and_displays_both_paths_without_real_models(self):
        settings = self.settings()
        for injected in (False, True):
            with self.subTest(injected=injected):
                getter = Mock(side_effect=AssertionError("不得读 .env")) if injected else Mock(return_value=settings)
                retriever = Mock()
                doc = RetrievedDoc(parent_id="p", doc_id="d", title="文档标题", source="demo.md",
                                   content="父块", snippet="片" * 80, score=0.7)
                seen_modes = []

                def retrieve(*args, **kwargs):
                    seen_modes.append(retriever.rerank_enabled)
                    return [doc]

                retriever.retrieve.side_effect = retrieve
                output = io.StringIO()
                with patch.object(config, "get_settings", getter), \
                        patch.object(search_demo, "BgeM3Embedder") as encoder, \
                        patch.object(search_demo, "BgeReranker") as reranker, \
                        patch.object(search_demo, "Retriever", return_value=retriever) as factory, \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(search_demo.main(["问题"], **({"settings": settings} if injected else {})), 0)
                encoder.assert_called_once_with(resolve_data_path(settings.bge_m3_path), 7)
                reranker.assert_called_once_with(resolve_data_path(settings.reranker_path), max_length=512, batch_size=16, fp16=False)
                factory.assert_called_once_with(None, encoder.return_value, reranker.return_value,
                                                uri=settings.milvus_uri, timeout_s=3.5, rerank_candidates=12,
                                                rerank_max_chars=600, rerank_text_mode="child")
                self.assertEqual(seen_modes, [False, True])
                self.assertEqual(retriever.retrieve.call_count, 2)
                retriever.retrieve.assert_called_with(["问题"], "问题", top_k=5)
                retriever.client.close.assert_called_once_with()
                self.assertIn("rerank=off", output.getvalue())
                self.assertIn("rerank=on", output.getvalue())
                self.assertIn("片" * 60, output.getvalue())
                self.assertNotIn("片" * 61, output.getvalue())
                getter.assert_not_called() if injected else getter.assert_called_once_with()

    def test_check_retrieval_defaults_to_both_modes_and_cli_overrides_candidates(self):
        settings = self.settings()
        with patch("app.retrieval.embedder.BgeM3Embedder"), patch("app.retrieval.reranker.BgeReranker"), \
                patch("app.retrieval.retriever.Retriever") as factory, \
                patch.object(check_retrieval, "evaluate", return_value={}) as evaluate, \
                patch.object(check_retrieval, "print_report"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_retrieval.main(["--candidates", "6"], settings=settings), 0)
        self.assertEqual([call.kwargs["rerank_text_mode"] for call in factory.call_args_list], ["child", "parent"])
        self.assertEqual([call.kwargs["rerank_candidates"] for call in factory.call_args_list], [6, 6])
        self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(factory.return_value.client.close.call_count, 2)

    def test_check_retrieval_cli_rejects_invalid_candidates_or_mode_without_loading_settings(self):
        for args in (["--candidates", "0"], ["--candidates", "-1"], ["--rerank-text-mode", "invalid"]):
            with patch.object(config, "get_settings", side_effect=AssertionError("不得读取配置")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                check_retrieval.main(args)
            self.assertEqual(caught.exception.code, 2)

    def test_faq_demo_uses_runtime_config_and_reports_hit_or_miss(self):
        settings = self.settings()
        for hit in (None, FaqHit(faq_id=1, question="问题", answer="答案", score=0.9, source="cache")):
            for injected in (False, True):
                with self.subTest(hit=hit, injected=injected):
                    getter = Mock(side_effect=AssertionError("不得读 .env")) if injected else Mock(return_value=settings)
                    service = Mock(top_score=0.9 if hit else 0.5)
                    service.faq_lookup.return_value = hit
                    output = io.StringIO()
                    with patch.object(config, "get_settings", getter), \
                            patch.object(faq_demo, "FaqRepo") as repo, \
                            patch.object(faq_demo, "FaqCache") as cache, \
                            patch.object(faq_demo, "BgeM3Embedder") as encoder, \
                            patch.object(faq_demo, "FaqService", return_value=service) as factory, \
                            contextlib.redirect_stdout(output):
                        self.assertEqual(faq_demo.main(["问题"], **({"settings": settings} if injected else {})), 0)
                    repo.assert_called_once_with(settings=settings)
                    cache.assert_called_once_with(url=settings.redis_url, ttl_s=60)
                    encoder.assert_called_once_with(resolve_data_path(settings.bge_m3_path), 7)
                    factory.assert_called_once_with(repo.return_value, cache.return_value,
                                                    encoder.return_value, threshold=0.85)
                    cache.return_value.client.close.assert_called_once_with()
                    result = json.loads(output.getvalue())
                    self.assertEqual(result["result"], hit.model_dump() if hit else None)
                    self.assertEqual(result["source"], "cache" if hit else None)
                    self.assertEqual(result["top_score"], service.top_score)
                    getter.assert_not_called() if injected else getter.assert_called_once_with()
