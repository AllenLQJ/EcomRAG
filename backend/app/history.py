"""客户端历史的统一边界处理，不依赖模型、配置或服务。"""


def normalize_history(value):
    """先校验全部结构，再保留最近六条、截断单条、按总量丢弃最早消息。"""
    if not isinstance(value, list):
        raise ValueError("history 必须为消息数组")
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"role", "content"}
                or item["role"] not in ("user", "assistant") or not isinstance(item["content"], str)):
            raise ValueError("history 消息结构非法")
    recent = [dict(role=item["role"], content=item["content"][:1000]) for item in value[-6:]]
    total = sum(len(item["content"]) for item in recent)
    while total > 4000:
        total -= len(recent.pop(0)["content"])
    return recent
