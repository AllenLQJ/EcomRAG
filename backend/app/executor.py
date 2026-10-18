"""独立同步 IO 线程池；取消等待不提前释放仍在运行的任务名额。"""
import asyncio
import logging
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from time import monotonic

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, SERVICE_BUSY

logger = logging.getLogger(__name__)


class BoundedExecutor:
    def __init__(self, workers, max_in_flight, *, name):
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._lock = threading.RLock()
        self._futures = set()
        self._closed = False
        self._close_future = None
        self._close_deadline = None
        self._close_abandoned = threading.Event()
        self._close_warned = False

    async def run(self, function, *args, **kwargs):
        abandoned = threading.Event()
        def invoke():
            if not abandoned.is_set():
                return function(*args, **kwargs)
        with self._lock:
            if self._closed:
                raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试")
            if not self._slots.acquire(blocking=False):
                raise AppError(SERVICE_BUSY, 503, "服务有点忙，请稍后再试")
            try:
                future = self._executor.submit(copy_context().run, invoke)
            except BaseException:
                self._slots.release()
                raise
            self._futures.add(future)
            # 回调绑定底层 Future，而非会在超时/取消时立即结束的 asyncio Future。
            future.add_done_callback(self._done)
        wrapped = asyncio.wrap_future(future)
        # 即使调用方已取消，也消费迟到异常，避免事件循环把异常中的原文写入日志。
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            # 不取消底层 Future：否则取消的排队任务仍留在执行器队列中，却提前释放名额。
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            abandoned.set()
            raise

    def _done(self, future):
        with self._lock:
            self._futures.discard(future)
            self._slots.release()

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self, cleanup=None, *, timeout=5):
        with self._lock:
            if self._close_future is None:
                self._closed = True
                self._close_deadline = monotonic() + max(0, timeout)
                pending = tuple(self._futures)
                def close():
                    # 多工作线程时，排在队尾的收尾任务仍须等其它线程真正结束。
                    for task in pending:
                        try:
                            task.exception(timeout=max(0, self._close_deadline - monotonic()))
                        except FutureTimeout:
                            self._close_abandoned.set()
                            return
                        except CancelledError:
                            pass
                    if self._close_abandoned.is_set() or monotonic() >= self._close_deadline:
                        self._close_abandoned.set()
                        return
                    if cleanup is not None:
                        cleanup()
                # 收尾排在已接纳的工作之后，不借用默认线程池，也不占业务名额。
                self._close_future = self._executor.submit(close)
                self._executor.shutdown(wait=False)
            future = self._close_future
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        _, pending = await asyncio.wait((wrapped,), timeout=max(0, self._close_deadline - monotonic()))
        if pending:
            self._close_abandoned.set()
        elif not wrapped.cancelled():
            wrapped.result()
        if self._close_abandoned.is_set():
            self.shutdown()
            with self._lock:
                if not self._close_warned:
                    logger.warning("同步 IO 线程池关闭超时，放弃未完成任务")
                    self._close_warned = True
