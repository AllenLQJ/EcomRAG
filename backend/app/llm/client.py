"""OpenAI 兼容客户端：对用户保持通用错误，日志提供经过脱敏的供应商诊断。"""
import asyncio
import errno
import json
import logging
from weakref import WeakKeyDictionary

import httpx

from app.contracts import AppError, LLM_ERROR, RATE_LIMITED, TIMEOUT
from app.llm.diagnostics import diagnostic_info, extra_body_keys
from app.llm.responses import extract_text

logger = logging.getLogger(__name__)
# 每个运行中的事件循环共享同一上限；隔离测试/CLI 创建的不同事件循环。
_limits = WeakKeyDictionary()
TRUNCATED_REASON = "输出被长度限制截断，可调大 max_tokens 或关闭思考模式"


def retryable_network(exc):
    if isinstance(exc, (httpx.ConnectError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return True
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE}:
            return True
        current = current.__cause__ or current.__context__
    # 有些兼容传输丢弃了 errno，只留下 ReadError/WriteError 的连接重置说明。
    # 这些文本只用于分类，从不记录到日志或错误响应。
    return isinstance(exc, (httpx.ReadError, httpx.WriteError)) and any(
        term in str(exc).lower() for term in ("connection reset", "connection aborted", "broken pipe"))


def _semaphore(limit):
    loop = asyncio.get_running_loop()
    limits = _limits.setdefault(loop, {})
    return limits.setdefault(limit, asyncio.Semaphore(limit))


class LLMClient:
    def __init__(self, settings, *, transport=None, sleep=None):
        self.settings = settings
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._http = httpx.AsyncClient(transport=transport, timeout=settings.llm_timeout_s,
                                      follow_redirects=False, trust_env=False)

    def _failure(self, category, code, status, message, *, model, messages, key,
                 response=None, exception_type=None):
        info = diagnostic_info(category, base_url=self.settings.llm_base_url, model=model,
                               messages=messages, key=key, response=response, exception_type=exception_type,
                               extra_body=self.settings.llm_extra_body)
        if category == "truncated":
            info["reason"] = TRUNCATED_REASON
        logger.warning("llm_error %s", json.dumps(info, ensure_ascii=False))
        error = AppError(code, status, message)
        # 随本次异常传递，避免并发请求共享 last_error 状态；不进入 API 错误响应。
        error.diagnostic = info
        return error

    async def _retry(self, category, status, number, context):
        safe = diagnostic_info(category, base_url=self.settings.llm_base_url,
                               extra_body=self.settings.llm_extra_body, **context)
        info = {name: safe[name] for name in ("category", "host", "model")}
        info.update(http_status=status, retry=number)
        logger.warning("llm_retry %s", json.dumps(info, ensure_ascii=False))
        await self._sleep(self.settings.llm_retry_backoff_s * 2 ** (number - 1))

    async def _request(self, payload, context):
        # 从首次请求到最后一次尝试/退避始终持有同一名额；不会在重试时递归申请。
        async with _semaphore(self.settings.llm_max_concurrency):
            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    async with asyncio.timeout(self.settings.llm_timeout_s):
                        response = await self._http.post(
                            self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                            headers={"Authorization": f'Bearer {context["key"]}'}, json=payload)
                except (httpx.TimeoutException, TimeoutError):
                    raise  # 超时不重试；整请求超时还会取消退避和后续尝试。
                except (httpx.RequestError, OSError) as exc:
                    if attempt >= self.settings.llm_max_retries or not retryable_network(exc):
                        raise
                    await self._retry("network", None, attempt + 1, context)
                    continue
                if (response.status_code == 429 or 500 <= response.status_code < 600) \
                        and attempt < self.settings.llm_max_retries:
                    status = response.status_code
                    await response.aclose()
                    await self._retry("http_status", status, attempt + 1, context)
                    continue
                return response

    async def complete(self, *, model, messages, temperature, max_tokens, json_mode=False):
        key = self.settings.llm_api_key.get_secret_value().strip()
        context = dict(model=model, messages=messages, key=key)
        if not key:
            raise self._failure("unconfigured", LLM_ERROR, 502, "模型服务未配置",
                                exception_type="AppError", **context)
        # 核心调用参数优先，避免供应商扩展覆盖模型、问题或配置的 token 上限。
        payload = {**self.settings.llm_extra_body,
                   "model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        logger.info("llm_request extra_body_keys=%s",
                    json.dumps(extra_body_keys(self.settings.llm_extra_body, key, messages), ensure_ascii=False))
        response = None
        try:
            response = await self._request(payload, context)
            if not 200 <= response.status_code < 300:
                limited = response.status_code == 429
                raise self._failure("http_status", RATE_LIMITED if limited else LLM_ERROR,
                                    429 if limited else 502,
                                    "请求太频繁，请稍后再试" if limited else "模型服务暂时不可用",
                                    response=response, exception_type="HTTPStatusError", **context)
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = extract_text(message.get("content"))
            if not content.strip():
                if choice.get("finish_reason") == "length":
                    raise self._failure("truncated", LLM_ERROR, 502, "模型服务返回异常",
                                        response=response, exception_type="ValueError", **context) from None
                raise ValueError
            return content
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise self._failure("timeout", TIMEOUT, 504, "请求超时，请稍后再试",
                                response=response, exception_type=type(exc).__name__, **context) from None
        except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
            raise self._failure("network", LLM_ERROR, 502, "模型服务返回异常",
                                response=response, exception_type=type(exc).__name__, **context) from None
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._failure("bad_response", LLM_ERROR, 502, "模型服务返回异常",
                                response=response, exception_type=type(exc).__name__, **context) from None

    async def aclose(self):
        await self._http.aclose()
