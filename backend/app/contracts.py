from pydantic import BaseModel

from app.schemas import Route, Strategy

# 统一业务错误码。
HTTP_ERROR = 40000
INVALID_ARGUMENT = 40001
NOT_FOUND = 40401
METHOD_NOT_ALLOWED = 40501
RATE_LIMITED = 42901
INTERNAL_ERROR = 50001
LLM_ERROR = 50201
DEPENDENCY_UNAVAILABLE = 50301
TIMEOUT = 50401


class AppError(Exception):
    def __init__(self, code: int, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


class RetrievedDoc(BaseModel):
    doc_id: str
    title: str
    content: str
    score: float


class RouteDecision(BaseModel):
    route: Route
    strategy: Strategy | None = None
    reason: str | None = None


class FaqHit(BaseModel):
    id: int
    question: str
    answer: str
    score: float
    category: str | None = None


async def retrieve(query: str, top_k: int = 5) -> list[RetrievedDoc]:
    """检索相关文档，暂不连接向量库。"""
    raise NotImplementedError


async def embed(texts: list[str]) -> list[list[float]]:
    """将文本编码为向量，暂不加载模型。"""
    raise NotImplementedError


async def route(message: str) -> RouteDecision:
    """判断问题的路由与查询策略，暂不调用路由模型。"""
    raise NotImplementedError


async def build_queries(message: str, strategy: Strategy) -> list[str]:
    """按指定策略构建检索查询，暂不改写问题。"""
    raise NotImplementedError


async def generate_answer(message: str, docs: list[RetrievedDoc]) -> str:
    """根据文档生成回答，暂不调用大模型。"""
    raise NotImplementedError


async def chat_general(message: str) -> str:
    """生成通用对话回答，暂不调用大模型。"""
    raise NotImplementedError


async def faq_lookup(message: str, threshold: float = 0.8) -> FaqHit | None:
    """查询达到阈值的 FAQ，暂不访问数据库。"""
    raise NotImplementedError
