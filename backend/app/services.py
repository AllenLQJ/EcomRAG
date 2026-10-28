"""应用持有服务生命周期；导入及构造不加载模型，不读取配置文件。"""
import threading
import logging
from functools import lru_cache

from app.contracts import AppError, DEPENDENCY_UNAVAILABLE
from app.executor import BoundedExecutor
from app.faq.cache import FaqCache
from app.faq.repo import FaqRepo
from app.faq.service import FaqService
from app.graph.lookup import GraphLookup
from app.llm.client import LLMClient
from app.llm.generator import Generator
from app.llm.router import Router
from app.retrieval.embedder import BgeM3Embedder
from app.retrieval.reranker import BgeReranker
from app.retrieval.remote_reranker import RemoteReranker
from app.retrieval.retriever import Retriever
from app.products import ProductCards
from ingest.loaders import resolve_data_path

logger = logging.getLogger(__name__)


def dependency_error(exc):
    # 只映射连接故障和 SDK 的基础设施异常；编程错误保持原样。
    if isinstance(exc, ConnectionError):
        return True
    return any(
        (base.__module__.startswith("pymysql") and base.__name__ in {"OperationalError", "InterfaceError"})
        or (base.__module__.startswith("pymilvus") and base.__name__ == "MilvusException")
        or base.__module__.startswith("grpc")
        for base in type(exc).__mro__
    )


def unavailable():
    return AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试")


@lru_cache
def _models(embed_path, batch_size, rerank_path, rerank_max_length, rerank_batch_size, rerank_fp16):
    # 全进程单例，锁同时防止首次加载竞争以及底层模型并发调用。
    return (BgeM3Embedder(embed_path, batch_size),
            BgeReranker(rerank_path, max_length=rerank_max_length, batch_size=rerank_batch_size,
                        fp16=rerank_fp16), threading.Lock())


@lru_cache
def _embedding_model(embed_path, batch_size):
    return BgeM3Embedder(embed_path, batch_size), threading.Lock()


class Services:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.products = ProductCards(settings)
        if not settings.rerank_enabled:
            logger.warning("重排已关闭：RRF 分不与 REFUSE_THRESHOLD 比较，仅在无检索结果时拒答")
        self._local_reranker = None
        remote = settings.rerank_provider == "remote" and settings.rerank_enabled
        if remote:
            self.embedder, self._model_lock = _embedding_model(
                str(resolve_data_path(settings.bge_m3_path)), settings.embed_batch_size)
            self.reranker = RemoteReranker(settings, transport=transport)
            if settings.remote_refuse_threshold is None:
                logger.warning("云端分数不用于拒答，拒答依赖路由 sensitive 与生成 NO_ANSWER")
            if not settings.remote_rerank_key or not settings.remote_rerank_base_url:
                logger.warning("远程重排未配置 key 或 base URL；调用时按 RERANK_FALLBACK 处理")
        else:
            self.embedder, self.reranker, self._model_lock = _models(
                str(resolve_data_path(settings.bge_m3_path)), settings.embed_batch_size,
                str(resolve_data_path(settings.reranker_path)),
                settings.rerank_max_length, settings.rerank_batch_size, settings.rerank_fp16,
            )
        self.cache = FaqCache(url=settings.redis_url, ttl_s=settings.faq_cache_ttl_s,
                              warning_interval_s=settings.faq_refresh_warning_interval_s)
        self.faq = None
        self._faq_init_lock = threading.Lock()
        # Retriever 持有可延迟连接的 MilvusClient 封装。
        self.retriever = Retriever(None, self.embedder, self.reranker, uri=settings.milvus_uri,
                                   timeout_s=settings.milvus_timeout_s,
                                   rerank_enabled=settings.rerank_enabled,
                                   rerank_candidates=settings.rerank_candidates,
                                   rerank_max_chars=settings.rerank_max_chars,
                                   rerank_text_mode=settings.rerank_text_mode,
                                   skus_metadata=settings.product_skus_metadata)
        if remote:
            self.retriever.rerank_fallback = settings.rerank_fallback
            self.retriever.local_rerank = self._fallback_local
            # 保留本地同步契约；pipeline.call 对远程方法直接 await。
            self.rerank_candidates = self._arerank_candidates
            self.retrieve = self._aretrieve
        self.llm = LLMClient(settings, transport=transport)
        self.router = Router(self.llm, settings.router_model, max_tokens=settings.router_max_tokens)
        self.generator = Generator(self.llm, settings.generate_model, max_tokens=settings.generate_max_tokens)
        self.model_executor = BoundedExecutor(settings.model_io_workers, name="model-io",
                                              queue_size=settings.model_io_queue_size,
                                              queue_timeout_s=settings.model_io_queue_timeout_s)
        # 刷新不占请求池名额，单 worker 时请求也无需等待整个快照构建。
        self.faq_refresh_executor = BoundedExecutor(1, name="faq-refresh", queue_size=0)
        self.graph = self.graph_executor = None
        if settings.graph_enabled:
            if settings.neo4j_password.get_secret_value():
                self.graph = GraphLookup(settings)
                # 图谱独立小池、不排队：Neo4j 变慢时满了直接跳过，不挤占模型调用的名额。
                self.graph_executor = BoundedExecutor(settings.graph_workers, name="graph", queue_size=0)
            else:
                logger.warning("GRAPH_ENABLED 已开启但未设置 NEO4J_PASSWORD；本进程不查询知识图谱")

    def _initialize(self, *, use_cache=True):
        with self._faq_init_lock:
            if self.faq is None:
                self.faq = FaqService(
                    FaqRepo(settings=self.settings), self.cache, self.embedder,
                    threshold=self.settings.faq_threshold, clear_cache_on_load=use_cache,
                    refresh_interval_s=self.settings.faq_refresh_interval_s,
                    warning_interval_s=self.settings.faq_refresh_warning_interval_s,
                    submit_refresh=self.faq_refresh_executor.try_submit, model_lock=self._model_lock)

    def _warmup(self):
        self._initialize()
        with self._model_lock:
            self.embedder.encode(["店铺服务"])
            if self.settings.rerank_enabled and self.settings.rerank_provider == "local":
                self.reranker.rerank("店铺服务", ["店铺服务"])
            self.retriever._connect()

    async def warmup(self):
        try:
            await self.model_executor.run(self._warmup)
        except Exception as exc:
            if dependency_error(exc):
                raise unavailable() from None
            raise

    def faq_lookup(self, question, *, use_cache=True):
        self._initialize(use_cache=use_cache)
        if use_cache:
            return self.faq.faq_lookup(question)
        return self.faq.faq_lookup(question, use_cache=False)

    def retrieve(self, queries, rerank_query, top_k=None):
        with self._model_lock:
            return self.retriever.retrieve(queries, rerank_query,
                                          top_k=self.settings.rerank_top_k if top_k is None else top_k)

    def retrieve_candidates(self, queries):
        with self._model_lock:
            return self.retriever.retrieve_candidates(queries)

    def rerank_candidates(self, rerank_query, candidates, top_k=None):
        with self._model_lock:
            return self.retriever.rerank_candidates(
                rerank_query, candidates, self.settings.rerank_top_k if top_k is None else top_k)

    async def _arerank_candidates(self, rerank_query, candidates, top_k=None):
        return await self.retriever.arerank_candidates(
            rerank_query, candidates, self.settings.rerank_top_k if top_k is None else top_k)

    async def _aretrieve(self, queries, rerank_query, top_k=None):
        from app.pipeline import call
        candidates = await call(self.retrieve_candidates, queries, executor=self.model_executor)
        return await self._arerank_candidates(rerank_query, candidates, top_k)

    async def _fallback_local(self, query, texts):
        from app.pipeline import call
        def invoke():
            with self._model_lock:
                if self._local_reranker is None:
                    self._local_reranker = BgeReranker(
                        resolve_data_path(self.settings.reranker_path), max_length=self.settings.rerank_max_length,
                        batch_size=self.settings.rerank_batch_size, fp16=self.settings.rerank_fp16)
                return self._local_reranker.rerank(query, texts)
        return await call(invoke, executor=self.model_executor)

    async def route(self, question, history):
        return await self.router.route(question, history)

    async def generate_answer(self, question, docs, history):
        return await self.generator.generate_answer(question, docs, history)

    @property
    def graph_lookup(self):
        return None if self.graph is None else self.graph.lookup

    def product_cards(self, docs, answer=""):
        return self.products.for_sources(docs, answer)

    async def chat_general(self, question, history):
        return await self.generator.chat_general(question, history)

    def generate_answer_stream(self, question, docs, history):
        return self.generator.generate_answer_stream(question, docs, history)

    def chat_general_stream(self, question, history):
        return self.generator.chat_general_stream(question, history)

    async def aclose(self):
        await self.llm.aclose()
        if isinstance(self.reranker, RemoteReranker):
            await self.reranker.aclose()
        await self.faq_refresh_executor.aclose()
        if self.graph_executor is not None:
            await self.graph_executor.aclose()
        # 等待已超时请求的同步工作退出，再关闭共享连接。
        def close():
            if self.faq is not None:
                self.faq.close()
            if self.graph is not None:
                self.graph.close()
            with self._model_lock:
                if self.cache.client is not None:
                    self.cache.client.close()
                if self.retriever.client is not None:
                    self.retriever.client.close()
        await self.model_executor.aclose(close)
