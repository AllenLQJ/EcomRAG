"""后台刷新与 model_io 容量/关闭约定的集成测试。"""
import asyncio
import threading
from unittest.mock import Mock, patch

from app.config import Settings
from app.executor import BoundedExecutor
from app.faq.cache import FaqCache
from app.faq.repo import FaqRecord
from app.faq.service import FaqService
from app.faq.version import FaqVersion, VERSION_KEY
from app.services import Services
from faq_fakes import FakeBM25, FakeRepo
from test_faq_admin import AdminRedis
from test_faq_refresh import Encoder


def test_single_request_worker_stays_available_while_snapshot_build_is_blocked():
    async def run():
        settings = Settings(_env_file=None, model_io_workers=1, model_io_queue_size=0)
        lock = threading.Lock()
        with patch("app.services._models", return_value=(Encoder(), Mock(), lock)):
            services = Services(settings)
        entered, release = threading.Event(), threading.Event()
        redis = AdminRedis()
        repo = FakeRepo([FaqRecord(1, "问题", "旧答案", "product")])
        services.cache = FaqCache(redis)
        now = [100]
        services.faq = FaqService(repo, services.cache, services.embedder,
                                  tokenizer=list, index_factory=FakeBM25, model_lock=lock,
                                  version_source=FaqVersion(services.cache, clock=lambda: now[0]),
                                  submit_refresh=services.faq_refresh_executor.try_submit)
        def blocked():
            entered.set()
            assert release.wait(3)
            return [FaqRecord(1, "问题", "新答案", "product")]
        repo.load_enabled = blocked
        redis.incr(VERSION_KEY)
        now[0] += 4
        try:
            first = await asyncio.wait_for(services.model_executor.run(services.faq_lookup, "问题"), 1)
            assert first.answer == "旧答案"
            assert await asyncio.to_thread(entered.wait, 1)
            result = await asyncio.wait_for(services.model_executor.run(services.faq_lookup, "问题"), .5)
            assert result.answer == "旧答案"
            assert services.faq_refresh_executor._active == 1
        finally:
            release.set()
            await services.aclose()
        assert services.faq_refresh_executor._closed
        assert services.model_executor._closed
    asyncio.run(run())


def test_background_executor_rejects_extra_work_and_preserves_capacity():
    async def run():
        executor = BoundedExecutor(1, name="faq-capacity-test", queue_size=0)
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            assert release.wait(3)
        try:
            future = executor.try_submit(blocked)
            assert future is not None
            assert await asyncio.to_thread(entered.wait, 1)
            assert executor.try_submit(lambda: None) is None
            assert executor._admitted == 1
            assert executor._active == 1
        finally:
            release.set()
            await executor.aclose()
        assert executor.try_submit(lambda: None) is None
    asyncio.run(run())
