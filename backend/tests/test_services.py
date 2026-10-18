import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.config import Settings
from app.budget import request_budget
from app.contracts import AppError, RouteDecision
from app.pipeline import run_chat
from app.rate_limit import RateLimiter
from app.schemas import ChatRequest
from app.services import Services, _models
from deadline_helpers import ControlledDeadlines


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


def test_disabled_rerank_startup_warns_about_rrf_units(caplog):
    settings = Settings(_env_file=None, rerank_enabled=False)
    with patch("app.services._models", return_value=(Mock(), Mock(), threading.Lock())):
        services = Services(settings)
        asyncio.run(services.llm.aclose())
    assert "RRF" in caplog.text and "REFUSE_THRESHOLD" in caplog.text


@pytest.mark.parametrize("top_k,expected", [(None, 3), (1, 1)])
def test_retrieval_and_split_rerank_use_configured_top_k_unless_overridden(top_k, expected):
    settings = Settings(_env_file=None, rerank_top_k=3)
    with patch("app.services._models", return_value=(Mock(), Mock(), threading.Lock())):
        services = Services(settings)
        services.retriever = Mock(client=None)
        try:
            kwargs = {} if top_k is None else {"top_k": top_k}
            services.retrieve(["问题"], "原问题", **kwargs)
            services.retriever.retrieve.assert_called_once_with(["问题"], "原问题", top_k=expected)
            services.rerank_candidates("原问题", [], **kwargs)
            services.retriever.rerank_candidates.assert_called_once_with("原问题", [], expected)
        finally:
            asyncio.run(services.aclose())


def test_timed_out_model_work_is_bounded_and_does_not_block_rate_limit_or_default_pool(monkeypatch, caplog):
    async def run():
        settings = Settings(_env_file=None, speculative_retrieval=False, request_timeout_s=60,
                            model_io_workers=1, model_io_max_in_flight=1)
        deadlines = ControlledDeadlines(monkeypatch, asyncio.Semaphore(1))
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        default_entered, model_entered = asyncio.Event(), asyncio.Event()
        release_default, release_model = threading.Event(), threading.Event()
        def blocked(entered, release):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        default_work = loop.run_in_executor(None, blocked, default_entered, release_default)
        with patch("app.services._models", return_value=(Mock(), Mock(), threading.Lock())):
            services = Services(settings)
        services.faq = Mock()
        services.faq.faq_lookup.return_value = None
        services.route = AsyncMock(return_value=RouteDecision(intent="knowledge", rewritten_query="检索问题"))
        services.retriever = Mock(client=None)
        services.retriever.retrieve.side_effect = lambda *a, **kw: blocked(model_entered, release_model)
        limiter = RateLimiter(settings)
        limiter.cache._client = lambda: None
        request = ChatRequest(session_id="bounded", message="PRIVATE_QUESTION")
        task = None
        try:
            await asyncio.wait_for(default_entered.wait(), 1)
            task = asyncio.create_task(run_chat(request, settings=settings, services=services))
            await asyncio.wait_for(model_entered.wait(), 1)
            deadlines.expire(task)
            with pytest.raises(AppError) as caught:
                await task
            assert (caught.value.code, caught.value.http_status) == (50401, 504)
            for _ in range(5):
                with pytest.raises(AppError) as caught:
                    await asyncio.wait_for(run_chat(request, settings=settings, services=services), 0.5)
                assert (caught.value.code, caught.value.http_status) == (50302, 503)
            services.retriever.retrieve.assert_called_once()
            # 默认池和模型池仍各有一个阻塞任务；限流、收尾均无需借用它们。
            await asyncio.wait_for(limiter.check_async("ip", "session"), 1)
            await asyncio.wait_for(limiter.aclose(), 1)
            assert not default_work.done()
        finally:
            release_model.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(services.aclose(), 1)
            await limiter.aclose()
            release_default.set()
            await default_work
    asyncio.run(run())
    assert "PRIVATE_QUESTION" not in caplog.text


def test_admitted_task_waits_for_model_lock_held_by_timed_out_work(monkeypatch, caplog):
    async def run():
        settings = Settings(_env_file=None, model_io_workers=2, model_io_max_in_flight=2)
        loop = asyncio.get_running_loop()
        entered, waiting = asyncio.Event(), asyncio.Event()
        release, lock = threading.Event(), threading.Lock()
        deadlines = ControlledDeadlines(monkeypatch, asyncio.Semaphore(1))
        class ObservedLock:
            def __enter__(self):
                if lock.locked():
                    loop.call_soon_threadsafe(waiting.set)
                lock.acquire()

            def __exit__(self, *args):
                lock.release()
        with patch("app.services._models", return_value=(Mock(), Mock(), ObservedLock())):
            services = Services(settings)
        services.retriever = Mock(client=None)
        def model(queries, question, **kwargs):
            if question == "first":
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
                raise ValueError("PRIVATE_QUESTION PRIVATE_ANSWER PRIVATE_KEY")
            return []
        services.retriever.retrieve.side_effect = model
        async def request(question):
            async with request_budget(60):
                return await services.model_executor.run(services.retrieve, [question], question)
        first = asyncio.create_task(request("first"))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            deadlines.expire(first)
            with pytest.raises(TimeoutError):
                await first
            assert lock.locked()
            second = asyncio.create_task(request("second"))
            await asyncio.wait_for(waiting.wait(), 1)
            # 第二个线程已准入，但尚未进入模型函数；任务名额不等于模型锁可用。
            assert not second.done()
            services.retriever.retrieve.assert_called_once()
            deadlines.expire(second)
            with pytest.raises(TimeoutError):
                await second
            assert lock.locked()
            services.retriever.retrieve.assert_called_once()
            with pytest.raises(AppError) as caught:
                await services.model_executor.run(lambda: None)
            assert caught.value.code == 50302
        finally:
            release.set()
            await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
            await asyncio.wait_for(services.aclose(), 1)
        assert services.retriever.retrieve.call_count == 2
        assert not lock.locked()
    asyncio.run(run())
    assert "PRIVATE_" not in caplog.text
