from time import perf_counter
from uuid import uuid4

from app.schemas import ChatRequest, ChatResponse, TraceItem


async def run_chat(chat: ChatRequest, request_id: str | None = None) -> ChatResponse:
    """返回脚手架占位结果，不执行缓存、FAQ、检索或模型调用。"""
    started = perf_counter()
    return ChatResponse(
        request_id=request_id or str(uuid4()),
        session_id=chat.session_id,
        answer="这是电商智能客服的占位回答，真实问答功能尚未接入。",
        route="general",
        strategy=None,
        sources=[],
        is_mock=True,
        trace=[TraceItem(
            stage="mock", ms=(perf_counter() - started) * 1000,
            route="general", detail="仅返回固定占位回答",
        )] if chat.debug else None,
    )
