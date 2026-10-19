import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.executor import BoundedExecutor
from app.faq.cache import FaqCache
from app.faq.repo import FaqRecord
from app.faq.service import FaqService
from app.faq.version import FaqVersion, VERSION_KEY
from faq_fakes import FakeBM25, FakeRepo
from test_faq_admin import AdminRedis


class Encoder:
    def encode(self, texts):
        return SimpleNamespace(dense=[[1.0, 0.0] for _ in texts])


class FaqRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.jobs = {}
        self.completed = {}

    def build(self, *, redis=None, repo=None, submit=None, lock=None, embedder=None):
        redis = redis if redis is not None else AdminRedis()
        cache = FaqCache(redis)
        version = FaqVersion(cache, interval_s=3, clock=lambda: self.now)
        if submit is None:
            executor = BoundedExecutor(1, name="faq-refresh-test", queue_size=0)
            self.addCleanup(executor.shutdown)
            submit = executor.try_submit
        jobs = []
        def tracked_submit(task):
            future = submit(task)
            if future is not None:
                self.assertIsInstance(future, Future)
                completed = threading.Event()
                self.completed[future] = completed
                # try_submit 先注册容量释放回调；等待这里的回调可确保名额已释放。
                future.add_done_callback(lambda done: completed.set())
                jobs.append(future)
            return future
        service = FaqService(repo if repo is not None else FakeRepo([FaqRecord(1, "问题", "旧答案", "product")]),
                             cache, embedder if embedder is not None else Encoder(),
                             tokenizer=list, index_factory=FakeBM25, version_source=version,
                             submit_refresh=tracked_submit, model_lock=lock)
        self.jobs[service] = jobs
        self.addCleanup(service.close)
        return service, redis

    def drain(self, service):
        for future in self.jobs[service]:
            future.result(timeout=3)
            self.assertTrue(self.completed[future].wait(1))
        self.assertFalse(service._refreshing)

    def block_reload(self, service):
        entered, release = threading.Event(), threading.Event()
        original = service.repo.load_enabled
        def blocked():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
            return original()
        service.repo.load_enabled = blocked
        self.addCleanup(release.set)
        return entered, release

    def test_version_change_refreshes_each_worker_once_and_is_ttl_cached(self):
        redis = AdminRedis()
        services = [self.build(redis=redis)[0] for _ in range(2)]
        gates = [self.block_reload(service) for service in services]
        redis.get = Mock(wraps=redis.get)
        for service in services:
            self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
        reads = lambda: sum(call.args == (VERSION_KEY,) for call in redis.get.call_args_list)
        self.assertEqual(reads(), 0)  # 构造时已读取版本，后续 GET 受 TTL 合并。
        redis.incr(VERSION_KEY)
        for service in services:
            service.repo.records = [FaqRecord(1, "问题", "新答案", "product")]
            service.faq_lookup("问题")
            self.assertEqual(self.jobs[service], [])
        self.now += 4
        try:
            for service, (entered, _) in zip(services, gates):
                self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
                self.assertTrue(entered.wait(1))
                service.faq_lookup("问题")
                self.assertEqual(len(self.jobs[service]), 1)
            self.assertEqual(reads(), 2)
        finally:
            for _, release in gates:
                release.set()
            for service in services:
                self.drain(service)
        for service in services:
            self.assertEqual(service.faq_lookup("问题").answer, "新答案")
            self.assertEqual(service._version, 1)
            self.assertEqual(service.repo.loads, 2)

    def test_failed_rebuild_keeps_snapshot_and_retries_without_leaking_error(self):
        service, redis = self.build()
        original = service._snapshot
        service.repo.load_enabled = Mock(side_effect=RuntimeError("PRIVATE SQL password"))
        redis.incr(VERSION_KEY)
        self.now += 4
        with self.assertLogs("app.faq.service", "WARNING") as logs:
            service.faq_lookup("问题")
            self.drain(service)
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.assertIs(service._snapshot, original)
        self.assertEqual(service._version, 0)
        self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
        self.assertEqual(len(self.jobs[service]), 1)
        service.repo.load_enabled = lambda: [FaqRecord(1, "问题", "恢复后", "product")]
        with patch("app.faq.service.monotonic", return_value=service._next_retry + 1):
            service.faq_lookup("问题")
            self.drain(service)
        self.assertEqual(len(self.jobs[service]), 2)
        self.assertEqual(service.faq_lookup("问题").answer, "恢复后")

    def test_requests_do_not_wait_for_background_database_build(self):
        service, redis = self.build()
        entered, release = self.block_reload(service)
        service.repo.records = [FaqRecord(1, "问题", "新答案", "product")]
        redis.incr(VERSION_KEY)
        self.now += 4
        try:
            self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
            self.assertTrue(entered.wait(1))
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(service.faq_lookup, "另一个问法")
                self.assertEqual(result.result(timeout=.5).answer, "旧答案")
        finally:
            release.set()
            self.drain(service)
        self.assertEqual(service.faq_lookup("问题").answer, "新答案")

    def test_background_encoding_respects_model_lock_without_blocking_lookup(self):
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        owner = self
        class BlockingEncoder(Encoder):
            block = False
            def encode(self, texts):
                if self.block and texts == ["新问题"]:
                    owner.assertTrue(lock.locked())
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test timeout")
                return super().encode(texts)
        encoder = BlockingEncoder()
        service, redis = self.build(lock=lock, embedder=encoder)
        encoder.block = True
        service.repo.records = [FaqRecord(2, "新问题", "新答案", "product")]
        redis.incr(VERSION_KEY)
        self.now += 4
        try:
            service._check_version()
            self.assertTrue(entered.wait(1))
            with ThreadPoolExecutor(1) as pool:
                self.assertEqual(pool.submit(service.faq_lookup, "问题").result(timeout=.5).answer, "旧答案")
                self.assertIsNone(pool.submit(service.faq_lookup, "未编码的新语义问题").result(timeout=.5))
        finally:
            release.set()
            self.drain(service)

    def test_refresh_starting_between_query_decision_and_lock_attempt_never_blocks(self):
        query_attempt, encoding_entered, release = threading.Event(), threading.Event(), threading.Event()
        attempts = []
        class RacingLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.armed = False
            def acquire(self, blocking=True):
                if self.armed:
                    self.armed = False
                    attempts.append(blocking)
                    query_attempt.set()
                    if not encoding_entered.wait(3):
                        raise RuntimeError("test timeout")
                return self.lock.acquire(blocking=blocking)
            def release(self):
                self.lock.release()
            def __enter__(self):
                self.acquire()
                return self
            def __exit__(self, *args):
                self.release()
        class BlockingEncoder(Encoder):
            def encode(self, texts):
                if texts == ["新问题"]:
                    encoding_entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test timeout")
                return super().encode(texts)
        lock = RacingLock()
        service, redis = self.build(lock=lock, embedder=BlockingEncoder())
        lock.armed = True
        queries = ThreadPoolExecutor(1)
        try:
            pending = queries.submit(service.faq_lookup, "未编码的新问法", use_cache=False)
            self.assertTrue(query_attempt.wait(1))
            redis.incr(VERSION_KEY)
            self.now += 4
            service.repo.records = [FaqRecord(2, "新问题", "新答案", "product")]
            service._check_version()
            self.assertTrue(encoding_entered.wait(1))
            self.assertIsNone(pending.result(timeout=.5))
            self.assertEqual(attempts, [False])
        finally:
            release.set()
            encoding_entered.set()
            queries.shutdown(wait=True)
            self.drain(service)

    def test_old_inflight_write_cannot_poison_new_snapshot_cache(self):
        entered, release = threading.Event(), threading.Event()
        service, redis = self.build()
        old_snapshot = service._snapshot
        original_set = service.cache.set
        def delayed_set(question, hit, **kwargs):
            if hit.answer == "旧答案":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            return original_set(question, hit, **kwargs)
        service.cache.set = delayed_set
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(service.faq_lookup, "问题")
            try:
                self.assertTrue(entered.wait(1))
                service.repo.records = [FaqRecord(1, "问题", "新答案", "product")]
                service.reload()
                self.assertNotEqual(old_snapshot[0].cache_namespace, service._snapshot[0].cache_namespace)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=1).answer, "旧答案")
        self.assertEqual(service.faq_lookup("问题").answer, "新答案")
        self.assertEqual(service.faq_lookup("问题").answer, "新答案")

    def test_outage_keeps_snapshot_and_rate_limits_sanitized_warnings(self):
        service, redis = self.build()
        redis.get = Mock(side_effect=ConnectionError("PRIVATE REDIS URL"))
        redis.scan = Mock(side_effect=ConnectionError("PRIVATE"))
        snapshot = service._snapshot
        self.now += 4
        with self.assertLogs("app.faq.version", "WARNING") as logs:
            for _ in range(5):
                self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
                self.now += 4
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.assertEqual(redis.get.call_count, 5)
        self.assertIs(service._snapshot, snapshot)
        self.assertEqual(service.repo.loads, 1)

    def test_version_arriving_during_build_is_not_lost(self):
        service, redis = self.build()
        entered, release = self.block_reload(service)
        redis.incr(VERSION_KEY)
        self.now += 4
        service._check_version()
        try:
            self.assertTrue(entered.wait(1))
            redis.incr(VERSION_KEY)
        finally:
            release.set()
            self.drain(service)
        self.assertEqual(service._version, 1)
        self.now += 4
        service._check_version()
        self.drain(service)
        self.assertEqual(len(self.jobs[service]), 2)
        self.assertEqual(service._version, 2)

    def test_no_executor_capacity_retries_without_waiting(self):
        executor = BoundedExecutor(1, name="faq-full-test", queue_size=0)
        self.addCleanup(executor.shutdown)
        entered, release = threading.Event(), threading.Event()
        def occupied():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
        occupied_future = executor.try_submit(occupied)
        capacity_released = threading.Event()
        occupied_future.add_done_callback(lambda done: capacity_released.set())
        submit = Mock(wraps=executor.try_submit)
        service, redis = self.build(submit=submit)
        redis.incr(VERSION_KEY)
        self.now += 4
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
            self.assertFalse(service._refreshing)
            service.faq_lookup("问题")
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(self.jobs[service], [])
        finally:
            release.set()
            occupied_future.result(timeout=3)
            self.assertTrue(capacity_released.wait(1))
        service._check_version()
        self.drain(service)
        self.assertEqual(service._version, 1)

    def test_existing_startup_version_does_not_reembed_snapshot(self):
        redis = AdminRedis()
        redis.values[VERSION_KEY] = 17
        encoder = Encoder()
        encoder.encode = Mock(wraps=encoder.encode)
        service, _ = self.build(redis=redis, embedder=encoder)
        self.assertEqual(service._version, 17)
        self.now += 4
        service.faq_lookup("问题")
        service.faq_lookup("问题")
        self.assertEqual(service.repo.loads, 1)
        self.assertEqual(encoder.encode.call_count, 2)  # 初始化一次，查询一次。
        self.assertEqual(self.jobs[service], [])

    def test_publish_during_initial_build_is_not_acknowledged_as_loaded(self):
        redis = AdminRedis()
        redis.values[VERSION_KEY] = 17
        class PublishingRepo(FakeRepo):
            def load_enabled(self):
                rows = super().load_enabled()
                if self.loads == 1:
                    redis.incr(VERSION_KEY)
                    self.records = [FaqRecord(1, "问题", "新答案", "product")]
                return rows
        service, _ = self.build(redis=redis, repo=PublishingRepo([FaqRecord(1, "问题", "旧答案", "product")]))
        self.assertEqual(service._version, 17)
        service._check_version()
        self.drain(service)
        self.assertEqual(service._version, 18)
        self.assertEqual(service.faq_lookup("问题").answer, "新答案")

    def test_startup_outage_keeps_snapshot_until_version_can_be_reconciled(self):
        redis = AdminRedis()
        redis.values[VERSION_KEY] = 17
        redis.get = Mock(side_effect=ConnectionError("PRIVATE"))
        with self.assertLogs("app.faq.version", "WARNING") as logs:
            service, _ = self.build(redis=redis)
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.assertIsNone(service._version)
        self.assertEqual(service.faq_lookup("问题").answer, "旧答案")
        self.assertEqual(self.jobs[service], [])
        redis.get = lambda key: redis.values.get(key)
        self.now += 4
        service._check_version()
        self.drain(service)
        self.assertEqual(service._version, 17)

    def test_cancelled_before_start_resets_refresh_flag_and_can_retry(self):
        pool = ThreadPoolExecutor(1)
        entered, release = threading.Event(), threading.Event()
        def occupied():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
        occupying = pool.submit(occupied)
        service, redis = self.build(submit=pool.submit)
        redis.incr(VERSION_KEY)
        self.now += 4
        try:
            self.assertTrue(entered.wait(1))
            service._check_version()
            self.assertTrue(service._refreshing)
            self.assertTrue(self.jobs[service].pop().cancel())
            self.assertFalse(service._refreshing)
            self.assertEqual(service._version, 0)
            release.set()
            occupying.result(timeout=3)
            service._check_version()
            self.drain(service)
            self.assertEqual(service._version, 1)
        finally:
            release.set()
            pool.shutdown(wait=True)
