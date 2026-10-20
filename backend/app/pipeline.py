import asyncio
import inspect
import logging
from time import perf_counter
from uuid import uuid4

from app.contracts import AppError, TIMEOUT
from app.executor import BoundedExecutor
from app.history import normalize_history
from app.refusals import REFUSAL, SENSITIVE_REFUSAL
from app.llm.strategies import build_queries
from app.llm.answer_stream import forward_answer
from app.llm.generator import clean_generated_answer, validate_citations
from app.retrieval.rewrite import merge_reranked, needs_rewrite_rerank
from app.schemas import ChatRequest, ChatResponse, Source
from app.services import dependency_error, unavailable
from app.tracing import items, record
from app.budget import deadline, request_budget

logger = logging.getLogger(__name__)


def uses_rrf(docs):
    return any(getattr(doc, "score_provider", None) == "fallback_rrf" for doc in docs)


def refuse_threshold(settings, docs):
    if any(getattr(doc, "score_provider", None) in {"local", "fallback_local"} for doc in docs):
        return settings.refuse_threshold
    if any(getattr(doc, "score_provider", None) == "remote" for doc in docs):
        return settings.remote_refuse_threshold
    return settings.rerank_refuse_threshold


async def call(function, *args, executor, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    # 工作线程只写自己的列表；成功 await 后在事件循环内交接，超时/取消时直接丢弃。
    target, worker_trace = items.get(), []
    def invoke():
        token = items.set(worker_trace)
        try:
            return function(*args, **kwargs)
        finally:
            items.reset(token)
    result = await executor.run(invoke)
    if target is not None:
        target.extend(worker_trace)
    return result


def discard(task):
    """取消协程等待并消费结果；已经开始的模型线程不能强制中止。"""
    def consume(done):
        try:
            done.result()
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
        except BaseException:
            outcome = "error"
        logger.debug("speculative_retrieval_discarded outcome=%s", outcome)
    task.add_done_callback(consume)
    if not task.done():
        task.cancel()


async def retrieve(services, queries, question, trace, *, executor, top_k, split=False):
    started = perf_counter()
    try:
        if split:
            candidates = await call(services.retrieve_candidates, queries, executor=executor)
            if not any(item.stage == "retrieve" for item in trace):
                record("retrieve", started, candidates=len(candidates))
            started = perf_counter()
            docs = await call(services.rerank_candidates, question, candidates, top_k=top_k, executor=executor)
        else:
            docs = await call(services.retrieve, queries, rerank_query=question, top_k=top_k, executor=executor)
        if not any(item.stage == "retrieve" for item in trace):
            record("retrieve", started, candidates=len(docs))
        if not any(item.stage == "rerank" for item in trace):
            record("rerank", started if split else perf_counter(), kept=len(docs))
        return (docs, candidates) if split else docs
    except Exception as exc:
        if dependency_error(exc):
            raise unavailable() from None
        raise


async def speculate(services, question, trace, *, executor, top_k):
    token = items.set(trace)
    try:
        return await retrieve(services, [question], question, trace, split=True, executor=executor, top_k=top_k)
    finally:
        items.reset(token)


async def rescue_rerank(services, question, rewritten, docs, candidates, trace, settings, executor):
    # 原问低分时，只要改写规范化后不同且非空就补救，不以文本相似度排除少量增字。
    # 未设置阈值、已过阈值、规范化相同、敏感输入、关闭重排或无结果直接跳过。
    threshold = refuse_threshold(settings, docs)
    if uses_rrf(docs) or threshold is None or not needs_rewrite_rerank(
            question, rewritten, docs, enabled=settings.rerank_enabled, threshold=threshold):
        return docs
    started, trace_start = perf_counter(), len(trace)
    try:
        revised = await call(services.rerank_candidates, rewritten, candidates,
                             top_k=settings.rerank_top_k, executor=executor)
    except Exception as exc:
        if dependency_error(exc):
            raise unavailable() from None
        raise
    # 仅合并两次真正的重排分，绝不把原始候选的 RRF 分当成相关度。
    if uses_rrf(revised):
        return revised
    providers = {doc.score_provider for doc in (*docs, *revised)} - {None}
    result = (revised if len(providers) > 1 else merge_reranked(docs, revised, settings.rerank_top_k))
    if not any(item.stage == "rerank" for item in trace[trace_start:]):
        record("rerank", started, kept=len(result))
    for item in trace[trace_start:]:
        if item.stage == "rerank":
            item.detail = "改写问题补充重排"
            item.top_score = max(doc.score for doc in result)
    return result


def select_context(docs, settings):
    if not docs or not settings.rerank_enabled or uses_rrf(docs):
        return docs
    threshold = settings.rag_context_min_score
    if not any(doc.score_provider in {"local", "fallback_local"} for doc in docs) and (
            settings.rerank_provider == "remote" or any(doc.score_provider == "remote" for doc in docs)):
        # 远程分数需单独校准；未设置时保留 TopK，不沿用本地分数的过滤阈值。
        threshold = settings.remote_rag_context_min_score
    if threshold is None:
        return docs
    highest = max(range(len(docs)), key=lambda i: docs[i].score)
    return [doc for i, doc in enumerate(docs)
            if i == highest or doc.score >= threshold]


async def run_chat(chat: ChatRequest, request_id: str | None = None, *, settings, services,
                   trace_sink=None, history=None, before_chat=None, on_status=None, on_delta=None,
                   collector=None) -> ChatResponse:
    trace = [] if trace_sink is None else trace_sink
    token = items.set(trace)
    started, result = perf_counter(), None
    executor = getattr(services, "model_executor", None)
    owns_executor = executor is None
    if owns_executor:
        # 注入的同步服务也使用独立线程池；真实服务的池由 Services 持有并跨请求共享。
        executor = BoundedExecutor(settings.model_io_workers, name="model-io",
                                   queue_size=settings.model_io_queue_size,
                                   queue_timeout_s=settings.model_io_queue_timeout_s)
    try:
        async with request_budget(settings.request_timeout_s):
            if before_chat is not None:
                await before_chat()
            result = await _run(chat, request_id or str(uuid4()), settings, services, trace,
                                chat.history if history is None else normalize_history(history), executor,
                                on_status=on_status, on_delta=on_delta, collector=collector)
            return result
    except TimeoutError:
        raise AppError(TIMEOUT, 504, "请求超时，请稍后再试") from None
    finally:
        record("total", started)
        if result is not None and chat.debug and settings.allow_trace:
            result.trace = list(trace)
        items.reset(token)
        if owns_executor:
            executor.shutdown()


async def _run(chat, request_id, settings, services, trace, history, executor, *, on_status=None, on_delta=None,
               collector=None):
    # 仅离线调用方显式传入；HTTP/流式入口不传递该回调，也不增加响应字段。
    collected_contexts = None
    intent = "unknown"

    def response(answer, route, sources=(), strategy=None, *, intent_override=None, refuse_reason=None):
        if collector is not None:
            collector({"question": chat.message, "history": [dict(item) for item in history],
                       "answer": answer, "route": route,
                       "intent": intent_override or intent, "refuse_reason": refuse_reason,
                       "contexts": collected_contexts or [],
                       "sources": [{"index": source.index, "doc_id": source.doc_id,
                                    "title": source.title, "score": source.score} for source in sources]})
        return ChatResponse(request_id=request_id, session_id=chat.session_id,
                            answer=answer, route=route, strategy=strategy, sources=list(sources),
                            is_mock=False, trace=trace if chat.debug and settings.allow_trace else None)

    async def lookup_faq(question):
        if on_status is not None:
            await on_status("faq")
        started = perf_counter()
        try:
            options = {"use_cache": False} if history else {}
            hit = await call(services.faq_lookup, question, executor=executor, **options)
        except Exception as exc:
            if dependency_error(exc):
                raise unavailable() from None
            raise
        # 假服务无需实现 tracing；真实服务已分别记录 cache 与 FAQ。
        if history:
            if not any(item.stage == "faq" for item in trace):
                record("faq", started, hit=hit is not None,
                       top_score=hit.score if hit else getattr(services, "top_score", None))
        elif not any(item.stage == "cache" for item in trace):
            record("cache", started, hit=hit is not None and hit.source == "cache")
            if hit is None or hit.source != "cache":
                record("faq", started, hit=hit is not None,
                       top_score=hit.score if hit else getattr(services, "top_score", None))
        if hit:
            return response(hit.answer, hit.source, [Source(index=1, doc_id=f"faq:{hit.faq_id}",
                            title=hit.question, snippet=hit.answer[:60], score=hit.score)],
                            intent_override=hit.source)

    if not history:
        faq_response = await lookup_faq(chat.message)
        if faq_response is not None:
            return faq_response
    speculative, speculative_trace = None, []
    if settings.speculative_retrieval and not history:
        speculative = asyncio.create_task(speculate(services, chat.message, speculative_trace,
                                                    executor=executor, top_k=settings.rerank_top_k))
    try:
        started = perf_counter()
        if on_status is not None:
            await on_status("routing")
        decision = await services.route(chat.message, history)
        intent = decision.intent
        record("router", started, intent=decision.intent)
        if decision.intent in {"sensitive", "general"}:
            if speculative is not None:
                discard(speculative)
                speculative = None
            if decision.intent == "sensitive":
                return response(SENSITIVE_REFUSAL, "fallback", refuse_reason="sensitive")
            started = perf_counter()
            if on_delta is not None:
                if on_status is not None:
                    await on_status("generating")
                answer = await forward_answer(services.chat_general_stream(chat.message, history), on_delta)
            else:
                answer = await services.chat_general(chat.message, history)
            record("generate", started)
            answer, no_answer = clean_generated_answer(answer)
            if no_answer:
                logger.info("chat_fallback reason=generator_no_answer")
                return response(REFUSAL, "fallback", refuse_reason="no_answer")
            return response(answer, "general")
        if history:
            faq_response = await lookup_faq(decision.rewritten_query)
            if faq_response is not None:
                return faq_response
        # 多轮原问可能只有「那台灯呢」，所有重排与补救均使用补齐后的问句。
        rerank_query = decision.rewritten_query if history else chat.message
        if on_status is not None:
            await on_status("retrieving")
        if speculative is not None:
            try:
                docs, candidates = await speculative
            except BaseException:
                discard(speculative)
                speculative = None
                raise
            speculative = None
            # 只有完整成功的任务才能交接；私有列表与其中对象不再被主流程共享。
            trace.extend(item.model_copy(update={"speculative": True}) for item in tuple(speculative_trace))
        elif settings.rerank_enabled and settings.rerank_provider == "remote":
            # 远程改写重排可能退回 RRF，必须保留未改分、未裁剪的候选池。
            docs, candidates = await retrieve(services, build_queries(decision, chat.message), rerank_query, trace,
                                             executor=executor, top_k=settings.rerank_top_k, split=True)
        else:
            docs = await retrieve(services, build_queries(decision, chat.message), rerank_query, trace,
                                  executor=executor, top_k=settings.rerank_top_k)
            # 顺序路径已完成改写检索；复用已返回的 TopK，避免重复检索。
            candidates = docs
        docs = await rescue_rerank(services, rerank_query, decision.rewritten_query, docs, candidates,
                                   trace, settings, executor)
    finally:
        # 路由失败、整请求超时或调用方取消时同样必须消费投机任务的异常。
        if speculative is not None:
            discard(speculative)
    threshold = refuse_threshold(settings, docs)
    if not docs or (settings.rerank_enabled and not uses_rrf(docs) and threshold is not None
                    and max(doc.score for doc in docs) < threshold):
        return response(REFUSAL, "fallback", strategy="direct",
                        refuse_reason="no_docs" if not docs else "low_score")
    # 拒答阈值判断后才筛选生成上下文；过滤后的顺序就是生成器和响应共用的编号。
    docs = select_context(docs, settings)
    if collector is not None:
        # 与 Generator._rag_messages 的资料块逐项一致，保留未引用项和原始编号。
        collected_contexts = [{"index": i, "doc_id": doc.doc_id, "title": doc.title,
                               "score": doc.score, "text": f"[{i}] {doc.title}\n{doc.content}"}
                              for i, doc in enumerate(docs, 1)]
    started = perf_counter()
    if on_delta is not None:
        if on_status is not None:
            await on_status("generating")
        raw = await forward_answer(services.generate_answer_stream(chat.message, docs, history), on_delta)
        generated = validate_citations(raw, len(docs))
    else:
        generated = await services.generate_answer(chat.message, docs, history)
    record("generate", started)
    # 以文本中的有效引用为准；也防止替身/其他生成器的元数据与文本不一致。
    answer, no_answer = clean_generated_answer(generated.answer)
    if no_answer:
        logger.info("chat_fallback reason=generator_no_answer")
        return response(REFUSAL, "fallback", strategy="direct", refuse_reason="no_answer")
    generated = validate_citations(answer, len(docs))
    indices = generated.cited_indices or {max(range(len(docs)), key=lambda i: docs[i].score) + 1}
    sources = [Source(index=i, doc_id=doc.doc_id, title=doc.title,
                      snippet=doc.snippet[:60], score=doc.score)
               for i, doc in enumerate(docs, 1)
               if i in indices]
    result = response(generated.answer, "rag", sources, "direct")
    cards = getattr(services, "product_cards", None)
    if cards is not None:
        cited_docs = [doc for i, doc in enumerate(docs, 1) if i in indices]
        remaining = deadline.get() - asyncio.get_running_loop().time() - 0.05
        if remaining > 0:
            try:
                async with asyncio.timeout(min(settings.product_cards_timeout_s, remaining)):
                    result.products = await call(cards, cited_docs, executor=executor) or None
            except Exception:
                logger.warning("product_cards_unavailable")
    return result
