"""一个逻辑请求只建立一次总超时；直接调用 LLM 时同样覆盖排队与退避。"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

deadline = ContextVar("request_deadline", default=None)


@asynccontextmanager
async def request_budget(seconds):
    if deadline.get() is not None:
        yield
        return
    token = deadline.set(asyncio.get_running_loop().time() + seconds)
    try:
        async with asyncio.timeout_at(deadline.get()):
            yield
    finally:
        deadline.reset(token)
