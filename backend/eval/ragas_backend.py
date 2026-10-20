"""唯一 RAGAS 版本适配点：只使用公开 API，安装真实版本后需核对这里。"""


class EvaluationDependencyError(RuntimeError):
    pass


class EvaluationConfigurationError(ValueError):
    pass


def make_scorer(config, *, judge=None, embeddings=None):
    """延迟导入并返回 scorer(sample, metric)；可注入已包装的 judge/embeddings。

    约定返回 {value, reason?, usage?}；不把异常消息或 SDK 对象导出。
    """
    import os
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    try:
        from ragas import EvaluationDataset, evaluate
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.metrics import (
            Faithfulness, ResponseRelevancy, LLMContextPrecisionWithReference, LLMContextRecall,
        )
        from ragas.run_config import RunConfig
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    except ImportError:
        raise EvaluationDependencyError("需先安装评估依赖（独立评估环境，见 backend/requirements-eval.txt 与 docs/评估体系与指标解释.md）；"
                                        "再核对 eval/ragas_backend.py 与已安装 RAGAS 的公开 API。") from None

    if judge is None:
        judge = LangchainLLMWrapper(ChatOpenAI(
            model=config.judge_model, base_url=config.judge_base_url, api_key=config.api_key,
            temperature=0, timeout=config.timeout_s, max_retries=0,
            extra_body=getattr(config, "judge_extra_body", None) or None))
    if "response_relevancy" in config.metrics and embeddings is None:
        if not config.embedding_model or not config.embedding_base_url:
            raise EvaluationConfigurationError(
                "response_relevancy 需要显式配置 --embedding-model 和 --embedding-base-url")
        embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
            model=config.embedding_model, base_url=config.embedding_base_url,
            api_key=config.api_key, request_timeout=config.timeout_s, max_retries=0))
    classes = {"faithfulness": Faithfulness, "response_relevancy": ResponseRelevancy,
               "context_precision": LLMContextPrecisionWithReference, "context_recall": LLMContextRecall}

    def score(sample, metric):
        options = {"llm": judge}
        if metric == "response_relevancy":
            options["embeddings"] = embeddings
        result = evaluate(
            EvaluationDataset.from_list([sample]), metrics=[classes[metric](**options)],
            raise_exceptions=True, show_progress=False,
            run_config=RunConfig(timeout=config.timeout_s, max_retries=0))
        # 单样本、单指标，避免依赖版本之间变化的结果列名。
        values = list(result.scores[0].values())
        usage = {}
        token_usage = getattr(result, "token_usage", None)
        if callable(token_usage):
            try:
                available = token_usage()
                usage = {name: getattr(available, name) for name in
                         ("input_tokens", "output_tokens", "total_tokens") if hasattr(available, name)}
            except Exception:
                # 未配置 token parser 的版本会报错；保持 unknown，不推算或记零。
                pass
        return {"value": values[0] if len(values) == 1 else None,
                "reason": "ragas_metric_result", "usage": usage}

    return score
