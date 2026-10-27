"""隔离开发者终端配置；配置读取测试可以在清理后主动设置环境变量。"""
import os


APP_PREFIXES = (
    "LLM_", "QWEN_", "ROUTER_", "GENERATE_", "MYSQL_", "REDIS_", "MILVUS_",
    "FAQ_", "REFUSE_", "RERANK_", "RERANKER_", "BGE_", "EMBED_", "CHILD_",
    "PARENT_", "REQUEST_", "RATE_LIMIT_", "HEALTH_", "LOG_", "CHAT_LOG_", "GRAPH_", "NEO4J_",
)


def clear_app_environment(monkeypatch, field_names):
    names = {name.upper() for name in field_names}
    for name in list(os.environ):
        if name.upper() in names or name.upper().startswith(APP_PREFIXES):
            monkeypatch.delenv(name, raising=False)
