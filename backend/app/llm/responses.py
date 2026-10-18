"""仅解析最终 text；结构诊断只允许类型、布尔值、计数及枚举。"""


def extract_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part["text"] for part in content
                       if isinstance(part, dict) and part.get("type") in (None, "text", "output_text")
                       and isinstance(part.get("text"), str))
    raise TypeError("invalid content type")


def response_structure(data):
    data = data if isinstance(data, dict) else {}
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    try:
        empty = not extract_text(content).strip()
    except TypeError:
        empty = not bool(content)
    finish_reason = choice.get("finish_reason")
    # 不把任意供应商字符串当成结构特征，避免该字段夹带内容文本。
    known_reasons = ("stop", "length", "tool_calls", "function_call", "content_filter")
    finish_reason = finish_reason if isinstance(finish_reason, str) and finish_reason in known_reasons else (
        None if finish_reason is None else "unknown")
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    def tokens(value):
        return value if type(value) is int and value >= 0 else None
    reasoning_tokens = tokens(details.get("reasoning_tokens"))
    if reasoning_tokens is None:
        reasoning_tokens = tokens(usage.get("reasoning_tokens"))
    return {"choices_count": len(choices) if isinstance(choices, list) else None,
            "finish_reason": finish_reason,
            "content_type": type(content).__name__ if content is not None else "None",
            "content_empty": empty,
            "has_reasoning_content": "reasoning_content" in message,
            "has_reasoning": "reasoning" in message,
            "usage": {"prompt_tokens": tokens(usage.get("prompt_tokens")),
                      "completion_tokens": tokens(usage.get("completion_tokens")),
                      "reasoning_tokens": reasoning_tokens}}
