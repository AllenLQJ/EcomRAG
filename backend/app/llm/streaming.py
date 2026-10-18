"""OpenAI SSE 内容流：不记录上游正文；连接、排队与退避共享整请求预算。"""
import asyncio
import json
from contextlib import aclosing

import httpx

from app.budget import raise_if_expired, request_budget
from app.contracts import LLM_ERROR, SERVICE_BUSY, TIMEOUT
from app.llm.client import _semaphore, rate_limit_delay, retryable_network


async def content_events(response):
    data = []
    finished = False
    async for line in response.aiter_lines():
        if line == "":
            if not data:
                continue
            raw, data = "\n".join(data), []
            if raw == "[DONE]":
                return
            event = json.loads(raw)
            if not isinstance(event, dict) or "error" in event:
                raise ValueError("invalid stream event")
            choices = event.get("choices")
            if choices == []:  # 允许末尾独立 usage 事件。
                continue
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("invalid stream choices")
            choice = choices[0]
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ValueError("invalid stream delta")
            text = delta.get("content")
            if text is not None and not isinstance(text, str):
                raise ValueError("invalid stream content")
            if text:
                yield text
            # reasoning_content 永远不转发，也不延长首内容/内容间空闲超时。
            reason = choice.get("finish_reason")
            if reason == "length":
                raise ValueError("truncated stream")
            if reason is not None:
                finished = True
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data or not finished:
        # EOF 且没有结束标记，不能把半截回答当作成功结果。
        raise httpx.RemoteProtocolError("incomplete stream")


async def stream_completion(client, *, model, messages, temperature, max_tokens):
    settings = client.settings
    context = dict(model=model, messages=messages, key=settings.llm_api_key.get_secret_value().strip())
    if not context["key"]:
        raise client._failure("unconfigured", LLM_ERROR, 502, "模型服务未配置",
                              exception_type="AppError", **context)
    payload = {**settings.llm_extra_body, "model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": True}
    semaphore = _semaphore(settings.llm_max_concurrency)
    network = server = rate = 0
    emitted = False
    try:
        async with request_budget(settings.request_timeout_s):
            while True:
                raise_if_expired()
                acquired = False
                status = None
                response = None
                category = None
                error = None
                delay = 0
                number = 0
                try:
                    try:
                        await semaphore.acquire()
                        acquired = True
                        async with asyncio.timeout(settings.llm_timeout_s) as timeout:
                            async with client._http.stream(
                                "POST", settings.llm_base_url.rstrip("/") + "/chat/completions",
                                headers={"Authorization": f'Bearer {context["key"]}'}, json=payload,
                                # read 由外层可重排 deadline 统一管理，reasoning 不能无限续期。
                                timeout=httpx.Timeout(settings.llm_timeout_s, read=None),
                            ) as response:
                                status = response.status_code
                                if not 200 <= status < 300:
                                    # 不读取或记录供应商自由文本（可能含问题、答案或凭据）。
                                    response.raise_for_status()
                                async with aclosing(content_events(response)) as chunks:
                                    async for text in chunks:
                                        emitted = True
                                        timeout.reschedule(asyncio.get_running_loop().time()
                                                           + settings.llm_stream_idle_timeout_s)
                                        yield text
                                if not emitted:
                                    raise ValueError("empty stream")
                                return
                    except httpx.HTTPStatusError:
                        raise_if_expired()
                        category = "http_status"
                        if status == 429 and rate < settings.llm_rate_limit_retries:
                            rate += 1
                            number, delay = rate, rate_limit_delay(response, rate)
                        elif status is not None and 500 <= status < 600 and server < settings.llm_max_retries:
                            server += 1
                            number, delay = server, settings.llm_retry_backoff_s * 2 ** (server - 1)
                        else:
                            error = True
                    except (httpx.TimeoutException, TimeoutError):
                        raise_if_expired()
                        category = "timeout"
                        if network < settings.llm_network_retries:
                            network += 1
                            number, delay = network, client._network_delay(network)
                        else:
                            error = True
                    except httpx.InvalidURL:
                        category, error = "network", True
                    except (httpx.RequestError, OSError) as exc:
                        raise_if_expired()
                        category = "network"
                        if retryable_network(exc) and network < settings.llm_network_retries:
                            network += 1
                            number, delay = network, client._network_delay(network)
                        else:
                            error = True
                    except (ValueError, KeyError, IndexError, TypeError):
                        category, error = "bad_response", True
                    # 429 退避前释放名额；网络/5xx 的退避继续持有名额。
                    if acquired and category == "http_status" and status == 429:
                        semaphore.release()
                        acquired = False
                    raise_if_expired()
                    if emitted or error or not client._can_retry(delay):
                        if category == "timeout":
                            raise client._failure(category, TIMEOUT, 504, "请求超时，请稍后再试",
                                                  exception_type="TimeoutError", **context)
                        limited = category == "http_status" and status == 429
                        raise client._failure(category, SERVICE_BUSY if limited else LLM_ERROR,
                                              503 if limited else 502,
                                              "服务有点忙，请稍后再试" if limited else
                                              "模型服务暂时不可用" if category == "http_status" else "模型服务返回异常",
                                              exception_type="StreamError", **context)
                    await client._retry(category, status if category == "http_status" else None,
                                        number, context, delay=delay)
                finally:
                    if acquired:
                        semaphore.release()
    except TimeoutError:
        raise client._failure("timeout", TIMEOUT, 504, "请求超时，请稍后再试",
                              exception_type="TimeoutError", **context) from None
