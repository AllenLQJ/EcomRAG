import asyncio
import threading
from unittest.mock import Mock

import pytest

from app.contracts import AppError
from app.executor import BoundedExecutor


@pytest.mark.parametrize("cancel", [False, True])
def test_running_job_keeps_capacity_after_timeout_or_cancellation(cancel, caplog):
    async def run():
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        pool = BoundedExecutor(1, 1, name="test-model")
        unexpected = Mock()
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            raise ValueError("PRIVATE_QUESTION PRIVATE_ANSWER PRIVATE_KEY")
        task = asyncio.create_task(pool.run(blocked))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(task, 0.01)
            for _ in range(5):
                with pytest.raises(AppError) as caught:
                    await asyncio.wait_for(pool.run(unexpected), 0.5)
                assert (caught.value.code, caught.value.http_status, caught.value.message) == (
                    50302, 503, "服务有点忙，请稍后再试")
            unexpected.assert_not_called()
            release.set()
            # 工作真正结束后才能重新接纳任务；不依赖线程与事件循环的调度顺序。
            async with asyncio.timeout(1):
                while True:
                    try:
                        assert await pool.run(lambda: "recovered") == "recovered"
                        break
                    except AppError:
                        await asyncio.sleep(0.001)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await pool.aclose()
    asyncio.run(run())
    assert "PRIVATE_" not in caplog.text


def test_queue_is_bounded_and_cancelled_queued_job_never_runs():
    async def run():
        loop = asyncio.get_running_loop()
        entered, queued, release = asyncio.Event(), asyncio.Event(), threading.Event()
        pool = BoundedExecutor(1, 2, name="test-model")
        cancelled_job = Mock()
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
        async def queue():
            queued.set()
            await pool.run(cancelled_job)
        holder = asyncio.create_task(pool.run(blocked))
        waiter = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            waiter = asyncio.create_task(queue())
            await queued.wait()
            with pytest.raises(AppError) as caught:
                await pool.run(lambda: None)
            assert (caught.value.code, caught.value.http_status) == (50302, 503)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            # 已取消的排队项被线程取出前仍占名额，避免取消风暴堆积无限队列项。
            for _ in range(5):
                with pytest.raises(AppError):
                    await pool.run(lambda: None)
            release.set()
            await holder
            await pool.aclose()
            cancelled_job.assert_not_called()
        finally:
            release.set()
            await asyncio.gather(*[task for task in (holder, waiter) if task is not None], return_exceptions=True)
            await pool.aclose()
    asyncio.run(run())


def test_close_waits_for_all_workers_before_cleanup_and_rejects_new_work():
    async def run():
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        pool = BoundedExecutor(2, 2, name="test-model")
        cleanup = Mock()
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
        holder = asyncio.create_task(pool.run(blocked))
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder
            closing = asyncio.create_task(pool.aclose(cleanup))
            await asyncio.sleep(0)
            with pytest.raises(AppError) as caught:
                await pool.run(lambda: None)
            assert (caught.value.code, caught.value.http_status) == (50301, 503)
            assert not closing.done()
            cleanup.assert_not_called()
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 1)
            await pool.aclose(cleanup)
        cleanup.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize("workers", [1, 2])
def test_close_timeout_abandons_hung_worker_without_late_cleanup(workers, caplog):
    async def run():
        loop = asyncio.get_running_loop()
        entered, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
        pool = BoundedExecutor(workers, workers, name="test-model")
        cleanup = Mock()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5)
                raise ValueError("PRIVATE_QUESTION PRIVATE_ANSWER PRIVATE_KEY")
            finally:
                loop.call_soon_threadsafe(finished.set)
        task = asyncio.create_task(pool.run(blocked))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(pool.aclose(cleanup, timeout=0.01), 1)
            assert not finished.is_set()
            cleanup.assert_not_called()
            with pytest.raises(AppError) as caught:
                await pool.run(lambda: None)
            assert (caught.value.code, caught.value.http_status) == (50301, 503)
            await asyncio.wait_for(pool.aclose(cleanup), 1)
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            await asyncio.gather(task, return_exceptions=True)
            await pool.aclose(cleanup)
            await asyncio.sleep(0)
        cleanup.assert_not_called()
        assert not unhandled
    asyncio.run(run())
    assert [record.getMessage() for record in caplog.records] == ["同步 IO 线程池关闭超时，放弃未完成任务"]
    assert "PRIVATE_" not in caplog.text
