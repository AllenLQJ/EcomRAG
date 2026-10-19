"""独立同步 IO 线程池；取消等待不提前释放仍在运行的任务名额。"""
import asyncio
import logging
import threading
from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from dataclasses import dataclass
from time import monotonic

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, SERVICE_BUSY, TIMEOUT

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    ready: asyncio.Future
    state: str = "waiting"


class BoundedExecutor:
    def __init__(self, workers, max_in_flight=None, *, name, queue_size=None, queue_timeout_s=20):
        # rate-limit 保留旧的总在飞容量；模型使用显式异步队列，不向无界线程队列堆任务。
        if workers <= 0 or (queue_size is None and (max_in_flight is None or max_in_flight <= 0)):
            raise ValueError("线程数和在飞容量必须大于零")
        if queue_size is not None and (queue_size < 0 or queue_timeout_s <= 0 or max_in_flight is not None):
            raise ValueError("异步排队需非负队列容量、正超时，且不能同时指定 max_in_flight")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._queued_mode = queue_size is not None
        self._slots = None if self._queued_mode else threading.BoundedSemaphore(max_in_flight)
        self._workers = workers
        self._capacity = workers + queue_size if self._queued_mode else max_in_flight
        self._queue_timeout_s = queue_timeout_s
        self._waiting = deque()
        self._active = self._admitted = 0
        self._lock = threading.RLock()
        self._futures = set()
        self._closed = False
        self._close_future = None
        self._close_deadline = None
        self._close_abandoned = threading.Event()
        self._close_warned = False

    async def run(self, function, *args, **kwargs):
        if self._queued_mode:
            return await self._run_queued(function, *args, **kwargs)
        return await self._run_immediate(function, *args, **kwargs)

    def try_submit(self, function):
        """后台任务只用空闲名额，不阻塞提交者或挤占等待者。"""
        with self._lock:
            if self._closed or self._waiting or (self._queued_mode and self._active >= self._workers):
                return None
            if self._queued_mode:
                self._active += 1
                self._admitted += 1
            elif not self._slots.acquire(blocking=False):
                return None
            try:
                future = self._executor.submit(copy_context().run, function)
            except BaseException:
                if self._queued_mode:
                    self._active -= 1
                    self._admitted -= 1
                else:
                    self._slots.release()
                raise
            self._futures.add(future)
            future.add_done_callback(self._done)
            return future

    async def _run_immediate(self, function, *args, **kwargs):
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

    def _notify(self, waiter):
        def wake():
            if not waiter.ready.done():
                waiter.ready.set_result(None)
        try:
            waiter.loop.call_soon_threadsafe(wake)
        except RuntimeError:
            # 所属循环已经关闭；没有协程会再消费这个预留名额。
            self._abandon(waiter)

    def _dispatch(self):
        # 调用方持有 _lock；线程完成回调也能安全唤醒其他事件循环的等待者。
        while not self._closed and self._waiting and self._active < self._workers:
            waiter = self._waiting.popleft()
            waiter.state = "reserved"
            self._active += 1
            self._notify(waiter)

    def _abandon(self, waiter):
        with self._lock:
            if waiter.state == "waiting":
                self._waiting.remove(waiter)
            elif waiter.state == "reserved":
                self._active -= 1
            else:
                return
            waiter.state = "released"
            self._admitted -= 1
            self._dispatch()

    def _reject_waiters(self):
        while self._waiting:
            waiter = self._waiting.popleft()
            waiter.state = "released"
            self._admitted -= 1
            self._notify(waiter)

    async def _run_queued(self, function, *args, **kwargs):
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop, loop.create_future())
        abandoned = threading.Event()
        def invoke():
            if not abandoned.is_set():
                return function(*args, **kwargs)
        with self._lock:
            if self._closed:
                raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试")
            if self._admitted >= self._capacity:
                raise AppError(SERVICE_BUSY, 503, "服务有点忙，请稍后再试")
            self._admitted += 1
            self._waiting.append(waiter)
            self._dispatch()
        try:
            try:
                async with asyncio.timeout(self._queue_timeout_s):
                    await asyncio.shield(waiter.ready)
            except TimeoutError:
                raise AppError(TIMEOUT, 504, "请求超时，请稍后再试") from None
            with self._lock:
                if self._closed:
                    raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试")
                # 成功提交后由底层 Future 释放执行名额；取消 await 不会提前释放它。
                future = self._executor.submit(copy_context().run, invoke)
                waiter.state = "submitted"
                self._futures.add(future)
                future.add_done_callback(self._done)
            wrapped = asyncio.wrap_future(future)
            wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            abandoned.set()
            raise
        finally:
            self._abandon(waiter)

    def _done(self, future):
        with self._lock:
            self._futures.discard(future)
            if self._queued_mode:
                self._active -= 1
                self._admitted -= 1
                self._dispatch()
            else:
                self._slots.release()

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._reject_waiters()
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self, cleanup=None, *, timeout=5):
        with self._lock:
            if self._close_future is None:
                self._closed = True
                self._reject_waiters()
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
