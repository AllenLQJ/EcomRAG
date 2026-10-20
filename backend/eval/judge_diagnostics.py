"""Judge 诊断白名单；异常消息只保留已知基础设施短语，不导出正文、请求或请求头。"""
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


CONNECTION_TYPES = {
    "OpenAIConnectionError", "APIConnectionError", "ConnectionError", "ConnectError",
    "ReadError", "WriteError", "CloseError", "NetworkError", "ProtocolError",
    "RemoteProtocolError", "LocalProtocolError", "ClosedResourceError", "BrokenResourceError",
    "EndOfStream", "gaierror", "BrokenPipeError",
}
# 固定短语替代原始异常消息。只做字符串清理无法保证 SDK 没有把请求正文拼进消息。
# 因此 URL（含用户信息、查询串）、headers、prompt 及任意附加内容都不复制。
INFRASTRUCTURE_MESSAGES = (
    "Event loop is closed", "This event loop is already running",
    "Cannot run the event loop while another loop is running",
    "is bound to a different event loop", "attached to a different loop",
    "There is no current event loop", "Cannot send a request, as the client has been closed.",
    "All connection attempts failed", "Connection refused", "Connection reset by peer",
    "Server disconnected without sending a response.", "Connection aborted",
    "Network is unreachable", "No route to host", "Broken pipe", "Stream is closed",
    "nodename nor servname provided, or not known", "Name or service not known",
    "Temporary failure in name resolution", "Connection error.",
)
LOOP_MESSAGES = INFRASTRUCTURE_MESSAGES[:7]


def _type_name(exc):
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", name) else "Exception"


def _safe_message(exc, secrets):
    names = {cls.__name__ for cls in type(exc).__mro__}
    if not (names & CONNECTION_TYPES or "RuntimeError" in names or "OSError" in names):
        return None
    try:
        message = str(exc)
    except Exception:
        return None
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    # 只在消息起始部分识别；不扫描可能很长的 SDK 回显正文。
    prefix = message[:512].lower()
    for phrase in INFRASTRUCTURE_MESSAGES:
        if phrase.lower() in prefix:
            return phrase[:80]
    return "[REDACTED]" if message else None


def diagnose(exc, *, secrets=()):
    if isinstance(exc, JudgeCallError):
        return dict(exc.diagnostics)
    chain, seen = [], set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = {cls.__name__ for item in chain for cls in type(item).__mro__}
    exception_chain = [{"exception_type": _type_name(item), "message": _safe_message(item, secrets)}
                       for item in chain]
    connection = bool(names & CONNECTION_TYPES) or any(
        item["message"] in LOOP_MESSAGES for item in exception_chain if item["message"] is not None)
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
    elif connection:
        category = "connection"
    elif empty:
        category = "empty_response"
    elif parse:
        category = "parse_error"
    else:
        category = "unknown"
    return {"category": category,
            "exception_type": _type_name(exc), "exception_chain": exception_chain,
            "http_status": status, "provider_error_code": code,
            "is_parse_error": category == "parse_error", "is_timeout": timeout,
            "is_empty_response": empty, "is_truncated": truncated,
            "is_connection_error": connection,
            "finish_reason": "length" if truncated else None}


RETRYABLE = {"parse_error", "empty_response", "timeout", "rate_limit", "server_error", "connection"}
