"""可复用的供应商诊断：只保留允许字段，屏蔽密钥及消息原文。"""
from html import unescape
import json
from urllib.parse import quote, urlsplit

from app.llm.responses import response_structure


def extra_body_values(body):
    """只用于屏蔽供应商错误里的参数值回显，不将值放进诊断对象。"""
    pending = [body]
    values = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and value:
            values.append(value)
        if isinstance(value, (dict, list)) and value:
            values.extend((json.dumps(value, ensure_ascii=False),
                           json.dumps(value, ensure_ascii=False, separators=(",", ":"))))
    return values


def extra_body_keys(body, key="", messages=()):
    secrets = [key, *private_texts(messages), *extra_body_values(body)]
    return sorted(redact(name, secrets) for name in body)

def redact(value, secrets=(), *, limit=200):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value)
    variants = set()
    for secret in secrets:
        if secret:
            variants.update((secret, quote(secret, safe=""),
                             json.dumps(secret, ensure_ascii=True)[1:-1],
                             json.dumps(secret, ensure_ascii=False)[1:-1]))
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text[:limit]


def private_texts(messages):
    """覆盖纯文本、提示词分隔块及转义后的问题；从不返回到诊断对象。"""
    result = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            for text in (content, unescape(content)):
                result.append(text)
                result.extend(line.strip() for line in text.splitlines() if line.strip())
    return result


def target_host(base_url, key=""):
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        host = None
    return redact(host, (key,))


def configuration(settings):
    key = settings.llm_api_key.get_secret_value().strip()
    return {"host": target_host(settings.llm_base_url, key),
            "router_model": redact(settings.router_model, (key,)),
            "generate_model": redact(settings.generate_model, (key,)),
            "key_configured": bool(key), "key_length": len(key)}


def diagnostic_info(category, *, base_url, model, key, messages, response=None, exception_type=None,
                    extra_body=None):
    extra_body = {} if extra_body is None else extra_body
    secrets = [key, *private_texts(messages), *extra_body_values(extra_body)]
    provider = {}
    data = None
    if response is not None:
        try:
            data = response.json()
            if category not in {"bad_response", "truncated"} and isinstance(data, dict) and isinstance(data.get("error"), dict):
                provider = data["error"]
        except (ValueError, TypeError):
            pass  # 非 JSON 响应不记录 body。
    info = {"category": category,
            "http_status": response.status_code if response is not None else None,
            "exception_type": redact(exception_type, secrets),
            "error_code": redact(provider.get("code"), secrets),
            "error_message": redact(provider.get("message"), secrets),
            "host": target_host(base_url, key), "model": redact(model, secrets),
            "extra_body_keys": extra_body_keys(extra_body, key, messages)}
    if category in {"bad_response", "truncated"}:
        info["response_structure"] = response_structure(data)
    if category == "http_status" and str(provider.get("code")) == "1210":
        # 固定配置建议，不回显请求中的扩展参数值或供应商正文。
        info["reason"] = (
            '该模型不支持关闭思考，请把 LLM_EXTRA_BODY 改为 {"thinking":{"type":"enabled","effort":"low"}}'
        )
    return info
