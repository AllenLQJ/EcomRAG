from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.schemas import Intent, Strategy

# 统一业务错误码。
HTTP_ERROR = 40000
INVALID_ARGUMENT = 40001
NOT_FOUND = 40401
METHOD_NOT_ALLOWED = 40501
RATE_LIMITED = 42901  # 仅用于本服务的 IP/session 限流；上游限流耗尽使用 SERVICE_BUSY。
INTERNAL_ERROR = 50001
LLM_ERROR = 50201
DEPENDENCY_UNAVAILABLE = 50301
SERVICE_BUSY = 50302  # 本地容量满载或上游模型服务持续限流。
TIMEOUT = 50401


class AppError(Exception):
    def __init__(self, code: int, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


class RetrievedDoc(BaseModel):
    parent_id: str
    doc_id: str
    title: str
    source: str
    content: str
    snippet: str
    score: float


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    strategy: Strategy = "direct"
    rewritten_query: str = Field(min_length=1)


class FaqHit(BaseModel):
    faq_id: int = Field(validation_alias=AliasChoices("faq_id", "id"))
    question: str
    answer: str
    score: float
    category: str | None = None
    source: Literal["cache", "faq"] = "faq"

    @property
    def id(self) -> int:
        """兼容原有 FAQ 标识的读取方式；序列化统一使用 faq_id。"""
        return self.faq_id

    @property
    def top_score(self) -> float:
        return self.score


def retrieve(queries: list[str], rerank_query: str, top_k: int = 5) -> list[RetrievedDoc]:
    """同步检索契约；实际使用注入依赖的 Retriever.retrieve。"""
    raise NotImplementedError


async def embed(texts: list[str]) -> list[list[float]]:
    """将文本编码为向量，暂不加载模型。"""
    raise NotImplementedError


def faq_lookup(question: str, *, use_cache: bool = True) -> FaqHit | None:
    """同步 FAQ 契约；多轮禁用缓存，阈值仍由 FaqService 构造时注入。"""
    raise NotImplementedError
