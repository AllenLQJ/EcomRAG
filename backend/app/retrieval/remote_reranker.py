"""智谱异步重排；不猜测 index 基数、分数范围或返回顺序。"""
import asyncio
import json
import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import uuid4
from weakref import WeakKeyDictionary

import httpx

from app.budget import deadline, request_budget
from app.contracts import AppError, LLM_ERROR, RATE_LIMITED, TIMEOUT
from app.llm.client import retryable_network
from app.llm.diagnostics import diagnostic_info, extra_body_keys, private_texts, redact, target_host

logger = logging.getLogger(__name__)
_limits = WeakKeyDictionary()


def _semaphore(limit):
    limits = _limits.setdefault(asyncio.get_running_loop(), {})
    return limits.setdefault(limit, asyncio.Semaphore(limit))


def document_text(result):
    document = result.get("document")
    if isinstance(document, dict):
        document = document.get("text")
    if document is not None and not isinstance(document, str):
        raise ValueError("invalid document")
    return document


def map_results(results, documents):
    """完整双射才接受；优先可信 0 基 index，否则按文本逐个消耗。"""
    n = len(documents)
    if not isinstance(results, list) or len(results) != n or not all(isinstance(r, dict) for r in results):
        raise ValueError("incomplete results")
    texts = [document_text(result) for result in results]
    indices = [result.get("index") for result in results]
    valid_indices = (all(type(index) is int and 0 <= index < n for index in indices)
                     and len(set(indices)) == n
                     and all(text is None or text == documents[index] for text, index in zip(texts, indices)))
    if not valid_indices:
        available = defaultdict(deque)
        for index, text in enumerate(documents):
            available[text].append(index)
        indices = []
        for text in texts:
            if text is None or not available[text]:
                raise ValueError("unmatched results")
            indices.append(available[text].popleft())
    scores = [0.0] * n
    for index, result in zip(indices, results):
        score = result.get("relevance_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("invalid score")
        scores[index] = float(score)
    return scores


@dataclass(frozen=True)
class RerankResult:
    scores: list[float]
    # 仅含安全的数字元数据，CLI 不保留或打印供应商 document。
    indices: list[int | None]
    index_base: str
    response_scores: list[float]
    usage: dict


class RemoteReranker:
    def __init__(self, settings, *, transport=None, sleep=None):
        self.settings = settings
        self.base_url, self.key = settings.remote_rerank_base_url, settings.remote_rerank_key
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._http = httpx.AsyncClient(
            transport=transport, timeout=settings.rerank_remote_timeout_s,
            limits=httpx.Limits(max_connections=settings.rerank_remote_max_concurrency),
            follow_redirects=False, trust_env=False)

    def _failure(self, category, messages, *, response=None, exception_type=None):
        info = diagnostic_info(category, base_url=self.base_url, model=self.settings.rerank_remote_model,
                               key=self.key, messages=messages, response=response,
                               exception_type=exception_type, extra_body=self.settings.rerank_remote_extra_body)
        # LLM response_structure 与重排无关；也不转存供应商其他任意字段。
        info.pop("response_structure", None)
        info.pop("reason", None)
        info["host"] = redact(target_host(self.base_url, self.key), [self.key, *private_texts(messages)])
        logger.warning("rerank_error %s", json.dumps(info, ensure_ascii=False))
        timeout = category == "timeout"
        limited = response is not None and response.status_code == 429
        error = AppError(TIMEOUT if timeout else RATE_LIMITED if limited else LLM_ERROR,
                         504 if timeout else 502,
                         "重排请求超时，请稍后再试" if timeout else "重排服务暂时不可用")
        error.diagnostic = info
        return error

    async def _request(self, payload, messages):
        async with _semaphore(self.settings.rerank_remote_max_concurrency):
            for number in range(self.settings.rerank_remote_max_retries + 1):
                response = None
                try:
                    async with asyncio.timeout(self.settings.rerank_remote_timeout_s):
                        response = await self._http.post(
                            self.base_url.rstrip("/") + "/rerank", json=payload,
                            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
                except (httpx.TimeoutException, TimeoutError):
                    raise  # 超时不重试；整请求取消也不会被吞掉。
                except (httpx.RequestError, OSError) as exc:
                    if (not isinstance(exc, ConnectionError) and not retryable_network(exc)
                            or number == self.settings.rerank_remote_max_retries):
                        raise
                else:
                    if (response.status_code != 429 and not 500 <= response.status_code < 600
                            or number == self.settings.rerank_remote_max_retries):
                        return response
                delay = self.settings.rerank_remote_retry_backoff_s * 2 ** number
                end = deadline.get()
                if end is not None and end - asyncio.get_running_loop().time() <= delay:
                    if response is not None:
                        return response
                    raise self._failure("network", messages, exception_type="RequestError") from None
                info = {"category": "http_status" if response is not None else "network",
                        "http_status": response.status_code if response is not None else None,
                        "host": redact(target_host(self.base_url, self.key), private_texts(messages)),
                        "model": redact(self.settings.rerank_remote_model, [self.key, *private_texts(messages)]),
                        "retry": number + 1}
                logger.warning("rerank_retry %s", json.dumps(info, ensure_ascii=False))
                if response is not None:
                    await response.aclose()
                await self._sleep(delay)

    async def _batch(self, query, documents, messages):
        if not self.key or not self.base_url:
            raise self._failure("unconfigured", messages, exception_type="AppError")
        payload = {**self.settings.rerank_remote_extra_body,
                   "model": self.settings.rerank_remote_model, "query": query,
                   "documents": documents, "top_n": 0, "return_documents": True,
                   "request_id": uuid4().hex}
        logger.info("rerank_request extra_body_keys=%s", json.dumps(
            extra_body_keys(self.settings.rerank_remote_extra_body, self.key, messages), ensure_ascii=False))
        response = None
        try:
            response = await self._request(payload, messages)
            if response.status_code != 200:
                raise self._failure("http_status", messages, response=response, exception_type="HTTPStatusError")
            data = response.json()
            results = data["results"]
            scores = map_results(results, documents)
            indices = [r.get("index") if type(r.get("index")) is int else None for r in results]
            # 基数推断必须由 document 对应关系确认；没有文本时报告未知。
            texts = [document_text(r) for r in results]
            bases = [base for base in (0, 1) if all(
                type(index) is int and 0 <= index - base < len(documents)
                and text is not None and text == documents[index - base]
                for index, text in zip(indices, texts))]
            index_base = "从 0 开始" if bases == [0] else "从 1 开始" if bases == [1] else "未知"
            usage = data.get("usage", {})
            usage = {name: usage[name] for name in ("prompt_tokens", "total_tokens")
                     if isinstance(usage, dict) and type(usage.get(name)) is int and usage[name] >= 0}
            return RerankResult(scores, indices, index_base,
                                [float(r["relevance_score"]) for r in results], usage)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise self._failure("timeout", messages, response=response, exception_type=type(exc).__name__) from None
        except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
            raise self._failure("network", messages, response=response, exception_type=type(exc).__name__) from None
        except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            raise self._failure("bad_response", messages, response=response, exception_type=type(exc).__name__) from None
        finally:
            if response is not None:
                await response.aclose()

    async def rerank_with_metadata(self, query, texts):
        messages = [{"content": text} for text in (query, *texts, query[:4096], *(t[:4096] for t in texts))]
        try:
            async with request_budget(self.settings.request_timeout_s):
                batches = []
                for offset in range(0, len(texts), 128):
                    batches.append(await self._batch(query[:4096], [t[:4096] for t in texts[offset:offset + 128]], messages))
                return batches
        except TimeoutError:
            raise self._failure("timeout", messages, exception_type="TimeoutError") from None

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [score for batch in await self.rerank_with_metadata(query, texts) for score in batch.scores]

    async def aclose(self):
        await self._http.aclose()
