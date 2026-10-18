import asyncio
from unittest.mock import Mock, patch

from app.config import Settings
from app.services import Services, _models


def test_assembly_is_lazy_singleton_and_warmup_initializes_once():
    settings = Settings(_env_file=None, bge_m3_path="models/offline-test-unique",
                        reranker_path="models/offline-test-reranker-unique")
    embedder, reranker, faq, retriever = Mock(), Mock(), Mock(), Mock(client=None)
    with patch("app.services.BgeM3Embedder", return_value=embedder) as encoder_factory, \
         patch("app.services.BgeReranker", return_value=reranker) as reranker_factory, \
         patch("app.services.FaqService", return_value=faq) as faq_factory, \
         patch("app.services.Retriever", return_value=retriever):
        first, second = Services(settings), Services(settings)
        assert first.router.max_tokens == settings.router_max_tokens
        assert first.generator.max_tokens == settings.generate_max_tokens
        encoder_factory.assert_called_once()
        reranker_factory.assert_called_once()
        assert reranker_factory.call_args.kwargs == {"max_length": 512, "batch_size": 16, "fp16": False}
        embedder.encode.assert_not_called()
        faq_factory.assert_not_called()
        assert first.embedder is second.embedder
        async def run():
            try:
                await first.warmup()
                await first.warmup()
                faq_factory.assert_called_once()
                assert embedder.encode.call_count == 2
                assert reranker.rerank.call_count == 2
                assert retriever._connect.call_count == 2
                await asyncio.to_thread(first.faq_lookup, "问题")
                faq.faq_lookup.assert_called_once_with("问题")
                await asyncio.to_thread(first.retrieve_candidates, ["问题"])
                retriever.retrieve_candidates.assert_called_once_with(["问题"])
                await asyncio.to_thread(first.rerank_candidates, "原问题", [], 5)
                retriever.rerank_candidates.assert_called_once_with("原问题", [], 5)
            finally:
                await first.aclose()
                await second.aclose()
        asyncio.run(run())
    _models.cache_clear()
