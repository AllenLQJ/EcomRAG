import logging
import json
import re

from pydantic import ValidationError

from app.contracts import RouteDecision
from app.llm.prompts import block, load
from app.llm.suspicious import is_suspicious

logger = logging.getLogger(__name__)


def first_json_object(raw):
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    if text.endswith("```"):
        text = text[:-3]
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
            return value
        except ValueError:
            continue
    raise ValueError("router_json_missing")


class Router:
    def __init__(self, client, model, *, max_tokens=2048):
        self.client, self.model, self.max_tokens = client, model, max_tokens

    async def route(self, question, history):
        """正常一次分类；仅解析失败再试一次，最后才启用可疑输入兜底。"""
        # P1 TODO：显式裁剪和隔离多轮 history；本阶段调用方传 []。
        for attempt in range(2):
            raw = await self.client.complete(
                model=self.model, temperature=0, max_tokens=self.max_tokens, json_mode=True,
                messages=[{"role": "system", "content": load("router")},
                          {"role": "user", "content": block("问题", question)}],
            )
            try:
                decision = RouteDecision.model_validate(first_json_object(raw), strict=True)
                if not decision.rewritten_query.strip():
                    raise ValueError
                return decision
            except (ValidationError, ValueError, TypeError, AttributeError):
                if attempt == 0:
                    logger.warning("router_degraded reason=parse_retry")
                    continue
                intent = "sensitive" if is_suspicious(question) else "knowledge"
                logger.warning("router_degraded reason=parse_fallback_%s", intent)
                return RouteDecision(intent=intent, rewritten_query=question)
