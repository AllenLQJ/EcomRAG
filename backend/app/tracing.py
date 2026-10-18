"""ContextVar 隔离请求，to_thread 复制上下文后仍写入本请求的记录列表。"""
from contextvars import ContextVar
from time import perf_counter

from app.schemas import TraceItem

items = ContextVar("chat_trace", default=None)


def record(stage, started, **fields):
    target = items.get()
    if target is not None:
        target.append(TraceItem(stage=stage, ms=(perf_counter() - started) * 1000, **fields))
