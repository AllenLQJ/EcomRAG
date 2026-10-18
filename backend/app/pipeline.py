import asyncio
import inspect
import logging
from contextvars import copy_context
from functools import partial
from time import perf_counter
from uuid import uuid4

from app.contracts import AppError, TIMEOUT
from app.llm.strategies import build_queries
from app.schemas import ChatRequest, ChatResponse, Source
from app.services import dependency_error, unavailable
from app.tracing import items, record

logger = logging.getLogger(__name__)

REFUSAL = "抱歉，店铺资料里没有找到相关说明。为避免给你不准确的信息，建议联系人工客服确认。"
SENSITIVE_REFUSAL = "抱歉，这类问题我无法回答。涉及他人隐私、医疗效果或超出店铺规则的内容，建议联系人工客服。"


async def call(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    # run_in_executor 不自动复制 ContextVar；保留工作线程内各阶段 trace 的请求归属。
    return await asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, partial(function, *args, **kwargs))


def discard(task):
    """取消协程等待并消费结果；已经开始的模型线程不能强制中止。"""
    def consume(done):
        try:
            done.result()
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
        except Exception:
            outcome = "error"
        logger.debug("speculative_retrieval_discarded outcome=%s", outcome)
    task.add_done_callback(consume)
    if not task.done():
        task.cancel()


async def retrieve(services, queries, question, trace, *, split=False):
    started = perf_counter()
    try:
        if split:
            candidates = await call(services.retrieve_candidates, queries)
            if not any(item.stage == "retrieve" for item in trace):
                record("retrieve", started, candidates=len(candidates))
            started = perf_counter()
            docs = await call(services.rerank_candidates, question, candidates, top_k=5)
        else:
            docs = await call(services.retrieve, queries, rerank_query=question, top_k=5)
        if not any(item.stage == "retrieve" for item in trace):
            record("retrieve", started, candidates=len(docs))
        if not any(item.stage == "rerank" for item in trace):
            record("rerank", started if split else perf_counter(), kept=len(docs))
        return docs
    except Exception as exc:
        if dependency_error(exc):
            raise unavailable() from None
        raise


async def speculate(services, question, trace):
    token = items.set(trace)
    try:
        return await retrieve(services, [question], question, trace, split=True)
    finally:
        items.reset(token)


async def run_chat(chat: ChatRequest, request_id: str | None = None, *, settings, services,
                   trace_sink=None, history=None) -> ChatResponse:
    trace = [] if trace_sink is None else trace_sink
    token = items.set(trace)
    started, result = perf_counter(), None
    try:
        async with asyncio.timeout(settings.request_timeout_s):
            result = await _run(chat, request_id or str(uuid4()), settings, services, trace,
                                [] if history is None else history)
            return result
    except TimeoutError:
        raise AppError(TIMEOUT, 504, "请求超时，请稍后再试") from None
    finally:
        record("total", started)
        if result is not None and chat.debug and settings.allow_trace:
            result.trace = list(trace)
        items.reset(token)


async def _run(chat, request_id, settings, services, trace, history):
    def response(answer, route, sources=(), strategy=None):
        return ChatResponse(request_id=request_id, session_id=chat.session_id,
                            answer=answer, route=route, strategy=strategy, sources=list(sources),
                            is_mock=False, trace=trace if chat.debug and settings.allow_trace else None)

    started = perf_counter()
    try:
        hit = await call(services.faq_lookup, chat.message)
    except Exception as exc:
        if dependency_error(exc):
            raise unavailable() from None
        raise
    # 假服务无需实现 tracing；真实服务已分别记录 cache 与 FAQ。
    if not any(item.stage == "cache" for item in trace):
        record("cache", started, hit=hit is not None and hit.source == "cache")
        if hit is None or hit.source != "cache":
            record("faq", started, hit=hit is not None,
                   top_score=hit.score if hit else getattr(services, "top_score", None))
    if hit:
        return response(hit.answer, hit.source, [Source(index=1, doc_id=f"faq:{hit.faq_id}",
                        title=hit.question, snippet=hit.answer[:60], score=hit.score)])
    speculative, speculative_trace = None, []
    if settings.speculative_retrieval and not history:
        speculative = asyncio.create_task(speculate(services, chat.message, speculative_trace))
    try:
        started = perf_counter()
        decision = await services.route(chat.message, history)
        record("router", started, intent=decision.intent)
        if decision.intent in {"sensitive", "general"}:
            if speculative is not None:
                discard(speculative)
                speculative = None
            if decision.intent == "sensitive":
                return response(SENSITIVE_REFUSAL, "fallback")
            started = perf_counter()
            answer = await services.chat_general(chat.message, history)
            record("generate", started)
            return response(answer, "general")
        if speculative is not None:
            pending, speculative = speculative, None
            try:
                docs = await pending
            finally:
                # 只在知识路径合并：被取消的线程即使稍后完成也不能污染主 trace。
                for item in speculative_trace:
                    item.speculative = True
                trace.extend(speculative_trace)
        else:
            docs = await retrieve(services, build_queries(decision, chat.message), chat.message, trace)
    finally:
        # 路由失败、整请求超时或调用方取消时同样必须消费投机任务的异常。
        if speculative is not None:
            discard(speculative)
    if not docs or max(doc.score for doc in docs) < settings.refuse_threshold:
        return response(REFUSAL, "fallback", strategy="direct")
    started = perf_counter()
    generated = await services.generate_answer(chat.message, docs, history)
    record("generate", started)
    sources = [Source(index=i, doc_id=doc.doc_id, title=doc.title,
                      snippet=doc.snippet[:60], score=doc.score)
               for i, doc in enumerate(docs, 1)
               if not generated.cited_indices or i in generated.cited_indices]
    return response(generated.answer, "rag", sources, "direct")
