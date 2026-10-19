"""客户端历史的统一边界处理，不依赖模型、配置或服务。"""


def normalize_history(value):
    """先校验结构及非空白，再保留最近六条、截断单条、按总量丢弃最早消息。

    按 Python Unicode 码点计数并切片，不保证 Unicode 字素簇完整。
    """
    if not isinstance(value, list):
        raise ValueError("history 必须为消息数组")
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"role", "content"}
                or item["role"] not in ("user", "assistant") or not isinstance(item["content"], str)):
            raise ValueError("history 消息结构非法")
        if not item["content"].strip():
            raise ValueError("内容不能为空白")
    recent = [dict(role=item["role"], content=item["content"][:1000]) for item in value[-6:]]
    # 非空原文也可能在截断后只剩空白；在请求边界拒绝，确保再次归一化仍合法。
    if any(not item["content"].strip() for item in recent):
        raise ValueError("内容不能为空白")
    total = sum(len(item["content"]) for item in recent)
    while total > 4000:
        total -= len(recent.pop(0)["content"])
    return recent
