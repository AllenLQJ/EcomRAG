import logging

from pydantic import ValidationError

from app.contracts import RouteDecision
from app.llm.prompts import block, load

logger = logging.getLogger(__name__)


class Router:
    def __init__(self, client, model, *, max_tokens=64):
        self.client, self.model, self.max_tokens = client, model, max_tokens

    async def route(self, question, history):
        """一次调用区分 general/knowledge/sensitive，非法输出保守降级为 knowledge。"""
        # P1 TODO：显式裁剪和隔离多轮 history；本阶段调用方传 []。
        raw = await self.client.complete(
            model=self.model, temperature=0, max_tokens=self.max_tokens, json_mode=True,
            messages=[{"role": "system", "content": load("router")},
                      {"role": "user", "content": block("问题", question)}],
        )
        try:
            decision = RouteDecision.model_validate_json(raw, strict=True)
            if not decision.rewritten_query.strip():
                raise ValueError
            return decision
        except (ValidationError, ValueError):
            logger.warning("路由模型 JSON 无效，使用默认知识检索策略")
            return RouteDecision(intent="knowledge", rewritten_query=question)
