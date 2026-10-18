"""OpenAI 兼容客户端：对用户保持通用错误，日志提供经过脱敏的供应商诊断。"""
import asyncio
import errno
import json
import logging
import math
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

import httpx

from app.contracts import AppError, LLM_ERROR, SERVICE_BUSY, TIMEOUT
from app.llm.diagnostics import diagnostic_info, extra_body_keys
from app.llm.responses import extract_text, response_structure
from app.budget import request_budget

logger = logging.getLogger(__name__)
# 每个运行中的事件循环共享同一上限；隔离测试/CLI 创建的不同事件循环。
_limits = WeakKeyDictionary()
TRUNCATED_REASON = "输出被长度限制截断，请调大 ROUTER_MAX_TOKENS/GENERATE_MAX_TOKENS"
REASONING_TRUNCATED_REASON = (
    "思考消耗了全部输出配额，请调大 ROUTER_MAX_TOKENS/GENERATE_MAX_TOKENS，或在模型支持时降低思考强度"
)


@dataclass(frozen=True)
class Completion:
    # 最终文本不参与 repr，诊断只使用其余经过白名单过滤的元数据。
    text: str = field(repr=False)
    finish_reason: str | None
    reasoning_tokens: int | None


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


def rate_limit_delay(response, number):
    """429 固定从 1 秒指数退避；只接受有效秒数的 Retry-After，等待不超过 8 秒。"""
    delay = 2 ** min(number - 1, 3)
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        seconds = 0
    if math.isfinite(seconds) and seconds > 0:
        delay = max(delay, seconds)
    return min(delay, 8)


class LLMClient:
    def __init__(self, settings, *, transport=None, sleep=None):
        self.settings = settings
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._transport = transport
        self._http = self._new_http()

    def _new_http(self):
        return httpx.AsyncClient(transport=self._transport, timeout=self.settings.llm_timeout_s,
                                 follow_redirects=False, trust_env=False)

    def _failure(self, category, code, status, message, *, model, messages, key,
                 response=None, exception_type=None):
        info = diagnostic_info(category, base_url=self.settings.llm_base_url, model=model,
                               messages=messages, key=key, response=response, exception_type=exception_type,
                               extra_body=self.settings.llm_extra_body)
        if category == "truncated":
            structure = info["response_structure"]
            reasoning = structure["has_reasoning_content"] or structure["has_reasoning"]
            info["reason"] = REASONING_TRUNCATED_REASON if reasoning else TRUNCATED_REASON
        # 429 日志仅保留结构化诊断，避免供应商自由文本回显未知的业务原文。
        if response is not None and response.status_code == 429:
            info["error_message"] = None
        logger.warning("llm_error %s", json.dumps(info, ensure_ascii=False))
        error = AppError(code, status, message)
        # 随本次异常传递，避免并发请求共享 last_error 状态；不进入 API 错误响应。
        error.diagnostic = info
        return error

    async def _retry(self, category, status, number, context, *, response=None, delay=None):
        safe = diagnostic_info(category, base_url=self.settings.llm_base_url,
                               extra_body=self.settings.llm_extra_body, response=response, **context)
        info = {name: safe[name] for name in ("category", "host", "model")}
        info.update(http_status=status, retry=number)
        if status == 429:
            info["error_code"] = safe["error_code"]
        logger.warning("llm_retry %s", json.dumps(info, ensure_ascii=False))
        await self._sleep(self.settings.llm_retry_backoff_s * 2 ** (number - 1) if delay is None else delay)

    async def _request(self, payload, context):
        # 429 使用独立计数，退避前释放名额；网络/5xx/超时仍共用普通重试预算并持有名额。
        semaphore = _semaphore(self.settings.llm_max_concurrency)
        acquired = False
        async with AsyncExitStack() as clients:
            http = self._http
            retries = timeout_retries = rate_limit_retries = 0
            try:
                while True:
                    if not acquired:
                        # 排队不消耗单次传输超时，但始终受外层整请求预算约束。
                        await semaphore.acquire()
                        acquired = True
                    try:
                        async with asyncio.timeout(self.settings.llm_timeout_s):
                            response = await http.post(
                                self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                                headers={"Authorization": f'Bearer {context["key"]}'}, json=payload)
                    except (httpx.TimeoutException, TimeoutError):
                        if retries >= self.settings.llm_max_retries \
                                or timeout_retries >= self.settings.llm_timeout_retries:
                            raise
                        retries += 1
                        timeout_retries += 1
                        await self._retry("timeout", None, retries, context)
                        # 独立连接池，避免复用挂起连接；不关闭其他并发请求共享的客户端。
                        # 整请求预算触发的是 CancelledError，不会进入此超时重试分支。
                        http = await clients.enter_async_context(self._new_http())
                        continue
                    except (httpx.RequestError, OSError) as exc:
                        if retries >= self.settings.llm_max_retries or not retryable_network(exc):
                            raise
                        retries += 1
                        await self._retry("network", None, retries, context)
                        continue
                    if response.status_code == 429 and rate_limit_retries < self.settings.llm_rate_limit_retries:
                        rate_limit_retries += 1
                        delay = rate_limit_delay(response, rate_limit_retries)
                        await response.aclose()
                        semaphore.release()
                        acquired = False
                        await self._retry("http_status", 429, rate_limit_retries, context,
                                          response=response, delay=delay)
                        continue
                    if 500 <= response.status_code < 600 and retries < self.settings.llm_max_retries:
                        retries += 1
                        await response.aclose()
                        await self._retry("http_status", response.status_code, retries, context)
                        continue
                    return response
            finally:
                if acquired:
                    semaphore.release()

    async def complete(self, *, model, messages, temperature, max_tokens, json_mode=False):
        completion = await self.complete_with_metadata(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, json_mode=json_mode)
        return completion.text

    async def complete_with_metadata(self, *, model, messages, temperature, max_tokens, json_mode=False):
        """每次调用独立返回最终文本和安全元数据，避免共享 last_response 的并发串扰。"""
        try:
            async with request_budget(self.settings.request_timeout_s):
                return await self._complete(model=model, messages=messages, temperature=temperature,
                                            max_tokens=max_tokens, json_mode=json_mode)
        except TimeoutError:
            raise self._failure("timeout", TIMEOUT, 504, "请求超时，请稍后再试", model=model, messages=messages,
                                key=self.settings.llm_api_key.get_secret_value().strip(),
                                exception_type="TimeoutError") from None

    async def _complete(self, *, model, messages, temperature, max_tokens, json_mode=False):
        key = self.settings.llm_api_key.get_secret_value().strip()
        context = dict(model=model, messages=messages, key=key)
        if not key:
            raise self._failure("unconfigured", LLM_ERROR, 502, "模型服务未配置",
                                exception_type="AppError", **context)
        # 核心调用参数优先，避免供应商扩展覆盖模型、问题或配置的 token 上限。
        payload = {**self.settings.llm_extra_body,
                   "model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        logger.info("llm_request extra_body_keys=%s",
                    json.dumps(extra_body_keys(self.settings.llm_extra_body, key, messages), ensure_ascii=False))
        response = None
        try:
            response = await self._request(payload, context)
            if not 200 <= response.status_code < 300:
                limited = response.status_code == 429
                raise self._failure("http_status", SERVICE_BUSY if limited else LLM_ERROR,
                                    503 if limited else 502,
                                    "服务有点忙，请稍后再试" if limited else "模型服务暂时不可用",
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
            structure = response_structure(data)
            return Completion(content, structure["finish_reason"], structure["usage"]["reasoning_tokens"])
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise self._failure("timeout", TIMEOUT, 504, "请求超时，请稍后再试",
                                response=response, exception_type=type(exc).__name__, **context) from None
        except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
            raise self._failure("network", LLM_ERROR, 502, "模型服务返回异常",
                                response=response, exception_type=type(exc).__name__, **context) from None
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._failure("bad_response", LLM_ERROR, 502, "模型服务返回异常",
                                response=response, exception_type=type(exc).__name__, **context) from None
        finally:
            if response is not None:
                await response.aclose()

    async def aclose(self):
        await self._http.aclose()
