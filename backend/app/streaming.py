"""有界事件交接、注释心跳与断开取消；业务逻辑复用 run_chat。"""
import asyncio
import json
import logging
from time import perf_counter

from anyio import CancelScope
from fastapi.responses import StreamingResponse

from app.chat_log import ChatLog, persist
from app.contracts import AppError, INTERNAL_ERROR
from app.pipeline import run_chat

logger = logging.getLogger(__name__)
HEARTBEAT_S = 10


class SSEStreamingResponse(StreamingResponse):
    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            # 发送数据期间断开时迭代器可能正停在 yield；显式关闭，不能依赖 GC。
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


def encode_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def chat_events(payload, request_id, *, settings, services, before_chat, chat_logs):
    # meta 在限流、模型加载或信号量排队之前发出。
    yield encode_event("meta", dict(request_id=request_id, session_id=payload.session_id))
    queue = asyncio.Queue(maxsize=1)
    trace = []
    started = perf_counter()

    async def produce():
        emitted = False
        result = None
        error = None
        partial = []
        log_task = None

        async def write_once(row):
            nonlocal log_task
            if not settings.chat_log_enabled:
                return
            if log_task is None:
                log_task = asyncio.create_task(persist(chat_logs, row))
            cancelled = False
            # anyio 屏蔽断开作用域，asyncio.shield 保住独立写入任务；多次取消也不重放写入。
            with CancelScope(shield=True):
                while not log_task.done():
                    try:
                        await asyncio.shield(log_task)
                    except asyncio.CancelledError:
                        cancelled = True
                    except BaseException:
                        break
                try:
                    log_task.result()
                except BaseException:
                    logger.warning("对话记录写入失败，已跳过")
            if cancelled:
                raise asyncio.CancelledError

        async def status(stage):
            await queue.put(("status", dict(stage=stage)))

        async def delta(text):
            nonlocal emitted
            await queue.put(("delta", dict(text=text)))
            emitted = True
            partial.append(text)

        try:
            try:
                result = await run_chat(payload, request_id=request_id, settings=settings, services=services,
                                        trace_sink=trace, before_chat=before_chat, on_status=status, on_delta=delta)
            except AppError as exc:
                error = dict(code=exc.code, message=exc.message, request_id=request_id)
            except Exception:
                # 不打印异常文本/栈：上游、SDK 可能把业务原文嵌入异常。
                error = dict(code=INTERNAL_ERROR, message="内部错误", request_id=request_id)
            row = ChatLog(request_id, payload.session_id, payload.message, result.answer if result else "",
                          result.route if result else "error", (perf_counter() - started) * 1000,
                          strategy=result.strategy if result else None,
                          sources=[source.model_dump() for source in result.sources] if result else [],
                          error_code=error["code"] if error else None, history_count=len(payload.history))
            await write_once(row)
            if error is not None:
                await queue.put(("error", error))
            else:
                # 所有直接返回和拒答（含 NO_ANSWER）都给只拼接 delta 的客户端完整文案。
                if not emitted:
                    await delta(result.answer)
                await queue.put(("done", result.model_dump()))
        except asyncio.CancelledError:
            row = ChatLog(request_id, payload.session_id, payload.message, "".join(partial), "error",
                          (perf_counter() - started) * 1000, history_count=len(payload.history))
            try:
                await write_once(row)
            except BaseException:
                # 写入失败或再次取消都不能替代原本的断开取消。
                pass
            raise
        finally:
            logger.info("chat_stream_done route=%s total_ms=%.3f stages=%s history_count=%d",
                        result.route if result else "error", (perf_counter() - started) * 1000,
                        json.dumps([item.stage for item in trace]), len(payload.history))

    producer = asyncio.create_task(produce())
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(queue.get())
            ready, _ = await asyncio.wait({pending, producer}, timeout=HEARTBEAT_S,
                                          return_when=asyncio.FIRST_COMPLETED)
            if pending not in ready:
                if producer in ready:
                    # 消费异常并让已唤醒的 queue.get 完成交接；异常终止也必须有 SSE error。
                    await asyncio.gather(producer, return_exceptions=True)
                    await asyncio.sleep(0)
                    if not pending.done():
                        yield encode_event("error", dict(code=INTERNAL_ERROR, message="内部错误", request_id=request_id))
                        break
                else:
                    yield ": ping\n\n"
                    continue
            event, data = pending.result()
            pending = None
            yield encode_event(event, data)
            if event in {"done", "error"}:
                break
    finally:
        # StreamingResponse 在 http.disconnect / 发送失败时关闭此生成器。
        for task in (pending, producer):
            if task is not None and not task.done():
                task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(*(task for task in (pending, producer) if task is not None), return_exceptions=True)
