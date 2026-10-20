"""服务器历史的写入预算；在序列化前限制每个字段和引用条数。"""
import json
import math
from collections.abc import Mapping
from itertools import islice

from app.contracts import AppError, INVALID_ARGUMENT

MAX_CONTENT_CHARS = 4000
MAX_SOURCES = 8
MAX_SOURCES_BYTES = 8 * 1024
MAX_MESSAGE_BYTES = 32 * 1024
MAX_CONVERSATION_MESSAGES = 200
MAX_ELAPSED_MS = 60 * 60 * 1000


def bounded_elapsed_ms(value):
    if value is not None and (type(value) is not int or not 0 <= value <= MAX_ELAPSED_MS):
        raise AppError(INVALID_ARGUMENT, 400, "耗时须为0–3600000毫秒的整数")
    return value


def message_metadata(sources, elapsed_ms=None, request_id=None):
    # 旧 sources 数组保持可读；新消息复用该 JSON 列承载元数据，无需 DDL。
    if elapsed_ms is None and request_id is None:
        return sources
    metadata = dict(sources=sources)
    if elapsed_ms is not None:
        metadata["elapsed_ms"] = bounded_elapsed_ms(elapsed_ms)
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise AppError(INVALID_ARGUMENT, 400, "请求标识非法")
        metadata["request_id"] = request_id
    return metadata


def check_message_size(content, route, metadata):
    if len(json_bytes(dict(role="assistant", content=content, route=route, sources=metadata))) > MAX_MESSAGE_BYTES:
        raise AppError(INVALID_ARGUMENT, 400, "消息体积超出上限")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def clipped(value, length):
    # 无效的孤立代理字符也不能穿透到 MySQL 的 UTF-8 编码器。
    return value[:length].encode("utf-8", errors="replace").decode("utf-8") if isinstance(value, str) else ""


def bounded_sources(sources):
    if sources is None:
        return None
    result = []
    # 不完整遍历超长 sources，更不先把它整体序列化。
    for item in islice(sources, MAX_SOURCES):
        if not isinstance(item, Mapping):
            continue
        index, score = item.get("index"), item.get("score")
        if type(index) is not int or not 0 <= index <= 2147483647 or type(score) not in {int, float}:
            continue
        try:
            score = float(score)
        except OverflowError:
            continue
        if not math.isfinite(score):
            continue
        compact = dict(index=index, doc_id=clipped(item.get("doc_id"), 128),
                       title=clipped(item.get("title"), 200), snippet=clipped(item.get("snippet"), 500),
                       score=score)
        if len(json_bytes(result + [compact])) > MAX_SOURCES_BYTES:
            break
        result.append(compact)
    return result


def bounded_message(content, route=None, sources=None, elapsed_ms=None, request_id=None):
    bounded_elapsed_ms(elapsed_ms)
    content, route = clipped(content, MAX_CONTENT_CHARS), clipped(route, 16) if route is not None else None
    sources = bounded_sources(sources)
    # 连 JSON 转义后的体积也受限；DB 中的原始 UTF-8 正文只会更小。
    metadata = message_metadata(sources, elapsed_ms, request_id)
    check_message_size(content, route, metadata)
    return content, route, sources
