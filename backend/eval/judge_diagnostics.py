"""Judge 诊断白名单；不读取异常文本、HTTP 正文、请求或请求头。"""
import re


class EmptyJudgeResponseError(RuntimeError):
    pass


class TruncatedJudgeResponseError(RuntimeError):
    pass


class JudgeCallError(RuntimeError):
    def __init__(self, diagnostics):
        super().__init__("judge_call_failed")
        self.diagnostics = diagnostics


def safe_parameters(body):
    """只报告已知思考选项，任意 extra_body 内容不进入报告。"""
    body = body or {}
    result = {}
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "disabled", "low", "high", "max"):
        result["thinking"] = {"type": thinking["type"]}
    if body.get("reasoning_effort") in ("none", "minimal", "low", "medium", "high", "max"):
        result["reasoning_effort"] = body["reasoning_effort"]
    return result


def diagnose(exc):
    if isinstance(exc, JudgeCallError):
        return dict(exc.diagnostics)
    chain, seen = [], set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = {cls.__name__ for item in chain for cls in type(item).__mro__}
    status, code = None, None
    for item in chain:
        candidate = getattr(item, "status_code", None)
        if candidate is None:
            candidate = getattr(getattr(item, "response", None), "status_code", None)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and 100 <= candidate <= 599:
            status = candidate
        # OpenAI SDK 已解析的 body，只取 code，不调用 response.json/text。
        body = getattr(item, "body", None)
        nested = body.get("error", body) if isinstance(body, dict) else {}
        candidate = getattr(item, "code", None)
        if candidate is None and isinstance(nested, dict):
            candidate = nested.get("code")
        if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
            candidate = str(candidate)
            # 提供方数字码和常见符号码；禁止把任意 body 字符串当作错误码导出。
            if re.fullmatch(r"[0-9]{1,12}", candidate) or candidate in {
                "rate_limit_exceeded", "insufficient_quota", "invalid_request_error",
                "invalid_api_key", "server_error", "context_length_exceeded",
            }:
                code = candidate
    timeout = any("timeout" in name.lower() for name in names)
    truncated = bool(names & {"TruncatedJudgeResponseError", "LengthFinishReasonError"})
    empty = "EmptyJudgeResponseError" in names
    parse = bool(names & {"ValueError", "TypeError", "KeyError", "OutputParserException",
                         "RagasOutputParserException", "ValidationError", "JSONDecodeError",
                         "APIResponseValidationError"})
    if truncated:
        category = "truncated"
    elif timeout:
        category = "timeout"
    elif status == 429:
        category = "rate_limit"
    elif status is not None and status >= 500:
        category = "server_error"
    elif status is not None:
        category = "http_error"
    elif empty:
        category = "empty_response"
    elif parse:
        category = "parse_error"
    else:
        category = "unknown"
    name = type(exc).__name__
    return {"category": category,
            "exception_type": name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", name) else "Exception",
            "http_status": status, "provider_error_code": code,
            "is_parse_error": category == "parse_error", "is_timeout": timeout,
            "is_empty_response": empty, "is_truncated": truncated,
            "finish_reason": "length" if truncated else None}


RETRYABLE = {"parse_error", "empty_response", "timeout", "rate_limit", "server_error"}
