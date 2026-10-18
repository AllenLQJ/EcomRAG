"""一个逻辑请求只建立一次总超时；直接调用 LLM 时同样覆盖排队与退避。"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

deadline = ContextVar("request_deadline", default=None)
budget_scope = ContextVar("request_budget_scope", default=None)


def budget_expired():
    end, scope = deadline.get(), budget_scope.get()
    if end is None and scope is None:
        return False
    now = asyncio.get_running_loop().time()
    return (scope is not None and scope.expired()) or (
        scope is not None and scope.when() is not None and now >= scope.when()) or (
        end is not None and now >= end)


def raise_if_expired():
    if budget_expired():
        raise TimeoutError


@asynccontextmanager
async def request_budget(seconds):
    if deadline.get() is not None:
        raise_if_expired()
        yield
        raise_if_expired()
        return
    token = deadline.set(asyncio.get_running_loop().time() + seconds)
    scope = asyncio.timeout_at(deadline.get())
    scope_token = budget_scope.set(scope)
    try:
        async with scope:
            yield
            raise_if_expired()
    finally:
        budget_scope.reset(scope_token)
        deadline.reset(token)
