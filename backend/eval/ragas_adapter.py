"""采集记录到 RAGAS 单轮输入的纯映射；不导入 ragas。"""


def not_applicable_category(case):
    """保持对外 route 原值；旧 fallback 无意图/拒答信息时不能推断为普通拒答。"""
    intent = case.get("intent", "unknown")
    refuse_reason = case.get("refuse_reason", "unknown")
    if intent == "sensitive" or refuse_reason == "sensitive":
        return "sensitive"
    route = case.get("route", "unknown")
    if route == "fallback" and intent == refuse_reason == "unknown":
        return "unknown"
    return route


def to_sample(case):
    route = case.get("route")
    contexts = case.get("contexts", [])
    valid_contexts = isinstance(contexts, list) and all(
        isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
        for item in contexts)
    if case.get("status") == "error" or route == "error":
        status, reason = "error", "collection_error"
    elif route in {"faq", "cache", "general", "sensitive", "fallback"}:
        status, reason = "not_applicable", f"route_{route}"
        if not_applicable_category(case) == "sensitive":
            reason = "route_sensitive"
    elif route != "rag":
        status, reason = "invalid", "unknown_route"
    elif not case.get("contexts"):
        status, reason = "invalid", "missing_rag_contexts"
    elif not valid_contexts:
        status, reason = "invalid", "invalid_rag_contexts"
    else:
        status, reason = "applicable", None
    # 上下文顺序来自 Generator 输入，绝不按分数或 sources 子集重排。
    sample = {"user_input": case["question"], "response": case.get("answer", ""),
              "retrieved_contexts": [item["text"] for item in contexts] if valid_contexts else [],
              "reference": case.get("reference")}
    return {"status": status, "reason": reason, "sample": sample}
