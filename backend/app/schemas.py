from typing import Literal

from pydantic import BaseModel, Field, field_validator

Route = Literal["cache", "faq", "rag", "general", "fallback"]
Strategy = Literal["direct", "hyde", "subquery", "stepback"]


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)
    debug: bool = False

    @field_validator("message", "session_id")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("内容不能为空白")
        return value


class Source(BaseModel):
    index: int
    doc_id: str
    title: str
    snippet: str
    score: float


class TraceItem(BaseModel):
    stage: str
    ms: float = Field(ge=0)
    route: Route | None = None
    strategy: Strategy | None = None
    detail: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    route: Route
    strategy: Strategy | None = None
    sources: list[Source] = Field(default_factory=list)
    is_mock: bool
    trace: list[TraceItem] | None = None
