from typing import Literal
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

from app.history import normalize_history

Route = Literal["cache", "faq", "rag", "general", "fallback"]
Intent = Literal["general", "knowledge", "sensitive"]
Strategy = Literal["direct", "hyde", "subquery", "stepback"]


class HistoryMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=500)
    debug: bool = False
    history: list[HistoryMessage] = Field(default_factory=list)
    conversation_id: str | None = Field(default=None, max_length=128)

    @field_validator("history", mode="before")
    @classmethod
    def validate_history(cls, value):
        return normalize_history(value)

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
    hit: bool | None = None
    top_score: float | None = None
    intent: Intent | None = None
    candidates: int | None = None
    kept: int | None = None
    speculative: bool | None = None
    provider: Literal["local", "remote", "fallback_rrf", "fallback_local"] | None = None
    fallback: Literal["rrf", "local"] | None = None


class ProductCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    name: str
    category: str
    highlights: list[str] = Field(min_length=1, max_length=2)
    image_url: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    route: Route
    strategy: Strategy | None = None
    sources: list[Source] = Field(default_factory=list)
    is_mock: bool = False
    trace: list[TraceItem] | None = None
    products: list[ProductCard] | None = Field(default=None, max_length=3)

    @model_serializer(mode="wrap")
    def serialize_response(self, handler):
        result = handler(self)
        # 保留现有 strategy/trace 的 null 语义，只省略可选商品字段。
        if not self.products:
            result.pop("products", None)
        return result
