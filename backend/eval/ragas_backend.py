"""唯一 RAGAS 版本适配点：只使用公开 API，安装真实版本后需核对这里。"""
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace

from eval.judge_diagnostics import (
    EmptyJudgeResponseError, JudgeCallError, TruncatedJudgeResponseError, diagnose,
)


class ResponseDiagnostics:
    """每次评分尝试独享的回调状态；从不保存输出文本或 SDK 对象。"""
    def __init__(self):
        self.truncated = False
        self.empty = False

    def on_llm_end(self, response, **kwargs):
        self.empty |= not response.generations or not any(response.generations)
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                metadata = getattr(message, "response_metadata", {}) or {}
                info = getattr(generation, "generation_info", {}) or {}
                self.truncated |= (info.get("finish_reason") or metadata.get("finish_reason")) == "length"
                self.empty |= (not getattr(generation, "text", "").strip()
                               and not getattr(message, "tool_calls", None))

    def failure(self, exc=None, *, secrets=()):
        if self.truncated:
            result = diagnose(TruncatedJudgeResponseError())
        elif self.empty:
            result = diagnose(EmptyJudgeResponseError())
        else:
            return diagnose(exc, secrets=secrets) if exc is not None else None
        result["is_empty_response"] = self.empty
        if exc is not None:
            original = diagnose(exc, secrets=secrets)
            result.update(exception_type=original["exception_type"], http_status=original["http_status"],
                          provider_error_code=original["provider_error_code"],
                          is_parse_error=original["is_parse_error"], is_timeout=original["is_timeout"],
                          is_connection_error=original["is_connection_error"],
                          exception_chain=original["exception_chain"])
        return result


class EvaluationDependencyError(RuntimeError):
    pass


class EvaluationConfigurationError(ValueError):
    pass


def make_scorer(config, *, judge=None, embeddings=None):
    """延迟导入并返回 scorer(sample, metric)；可注入已包装的 judge/embeddings。

    约定返回 {value, reason?, usage?}。默认客户端每次尝试独立创建和关闭；
    注入对象由调用者管理，不由这里关闭。
    """
    import os
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    try:
        import httpx
        from ragas.dataset_schema import SingleTurnSample
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.metrics import (
            Faithfulness, ResponseRelevancy, LLMContextPrecisionWithReference, LLMContextRecall,
        )
        from ragas.run_config import RunConfig
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError:
        raise EvaluationDependencyError("需先安装评估依赖（独立评估环境，见 backend/requirements-eval.txt 与 docs/评估体系与指标解释.md）；"
                                        "再核对 eval/ragas_backend.py 与已安装 RAGAS 的公开 API。") from None

    automatic_judge = judge is None
    if "response_relevancy" in config.metrics and embeddings is None:
        if not config.embedding_model or not config.embedding_base_url:
            raise EvaluationConfigurationError(
                "response_relevancy 需要显式配置 --embedding-model 和 --embedding-base-url")
    classes = {"faithfulness": Faithfulness, "response_relevancy": ResponseRelevancy,
               "context_precision": LLMContextPrecisionWithReference, "context_recall": LLMContextRecall}

    def score(sample, metric):
        class Observer(ResponseDiagnostics, BaseCallbackHandler):
            pass

        observer = Observer()

        async def run_attempt():
            # 所有资源属于本次尝试的循环。显式传入 HTTP clients，避免 SDK/
            # LangChain 默认连接池缓存；关闭连接后 asyncio.run 才关闭循环。
            async with AsyncExitStack() as stack:
                needs_clients = automatic_judge or (metric == "response_relevancy" and embeddings is None)
                if needs_clients:
                    async_client = await stack.enter_async_context(httpx.AsyncClient(timeout=config.timeout_s))
                    sync_client = stack.enter_context(httpx.Client(timeout=config.timeout_s))
                active_judge = judge
                if automatic_judge:
                    active_judge = LangchainLLMWrapper(ChatOpenAI(
                        model=config.judge_model, base_url=config.judge_base_url, api_key=config.api_key,
                        temperature=0, timeout=config.timeout_s, max_retries=0,
                        max_tokens=config.max_tokens, extra_body=config.judge_extra_body,
                        http_client=sync_client, http_async_client=async_client))
                options = {"llm": active_judge}
                if metric == "response_relevancy":
                    active_embeddings = embeddings
                    if active_embeddings is None:
                        active_embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
                            model=config.embedding_model, base_url=config.embedding_base_url,
                            api_key=config.api_key, request_timeout=config.timeout_s, max_retries=0,
                            http_client=sync_client, http_async_client=async_client))
                    options["embeddings"] = active_embeddings
                active_metric = classes[metric](**options)
                active_metric.init(RunConfig(timeout=config.timeout_s, max_retries=0, max_workers=1))
                # 直接等待公开的异步指标 API，不调用可能另行创建/修补事件循环的 evaluate。
                return await active_metric.single_turn_ascore(
                    SingleTurnSample(**sample), callbacks=[observer], timeout=config.timeout_s)

        try:
            value = asyncio.run(run_attempt())
        except Exception as exc:
            raise JudgeCallError(observer.failure(exc, secrets=(config.api_key,))) from None
        failure = observer.failure()
        if failure:
            raise JudgeCallError(failure)
        # 指标公开异步 API 返回标量；没有 token parser 时保持 unknown，不推算。
        return {"value": value, "reason": "ragas_metric_result", "usage": {}}

    # 每个评分任务独立切换配置，避免并发下修改共享 Judge 的请求参数。
    if automatic_judge:
        score.with_extra_body = lambda body: make_scorer(replace(config, judge_extra_body=body),
                                                        embeddings=embeddings)
    return score
