"""驱动真实 asyncio deadline，避免用很短的 sleep 猜测排队时序。"""
import asyncio


class ControlledDeadlines:
    def __init__(self, monkeypatch, semaphore):
        self.scopes = {}
        self.waiters = asyncio.Queue()
        timeout_at = asyncio.timeout_at
        acquire = semaphore.acquire

        def capture(when):
            scope = timeout_at(when)
            self.scopes.setdefault(asyncio.current_task(), []).append(scope)
            return scope

        async def observe_acquire():
            if semaphore.locked():
                # 回调执行时 acquire 已挂起；持有者由 Event 控制，不会提前释放。
                asyncio.get_running_loop().call_soon(self.waiters.put_nowait, asyncio.current_task())
            return await acquire()

        monkeypatch.setattr(asyncio, "timeout_at", capture)
        # 只观察真实共享信号量，不替换其排队、取消或释放逻辑。
        monkeypatch.setattr(semaphore, "acquire", observe_acquire)

    def expire(self, task):
        scopes = self.scopes[task]
        assert len(scopes) == 1, "同一请求应只建立一个总 deadline"
        scopes[0].reschedule(asyncio.get_running_loop().time() - 1)
