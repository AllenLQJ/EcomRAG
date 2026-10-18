import asyncio
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app import config
from app.contracts import (
    AppError,
    HTTP_ERROR,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
)
from app.pipeline import run_chat
from app.schemas import ChatRequest, ChatResponse


async def tcp_status(host: str, port: int, timeout: float) -> str:
    """连接与关闭共用总超时，已连通的服务不因关闭失败而降级。"""
    connected = False
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(host, port)
            connected = True
            writer.close()
            await writer.wait_closed()
    except Exception:
        pass
    return "up" if connected else "down"


async def uri_status(uri: str, default_port: int, timeout: float) -> str:
    try:
        parsed = urlsplit(uri)
        if not parsed.hostname:
            return "down"
        return await tcp_status(parsed.hostname, parsed.port or default_port, timeout)
    except Exception:
        return "down"


def error_response(request: Request, code: int, message: str, status: int) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


def create_app(settings: config.Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else config.get_settings()
    app = FastAPI(
        title="电商 RAG 智能客服",
        debug=settings.debug,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return error_response(request, exc.code, exc.message, exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return error_response(request, INVALID_ARGUMENT, "请求参数非法", 400)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        code = {
            404: NOT_FOUND,
            405: METHOD_NOT_ALLOWED,
        }.get(exc.status_code, HTTP_ERROR if exc.status_code < 500 else INTERNAL_ERROR)
        response = error_response(request, code, str(exc.detail), exc.status_code)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return error_response(request, INTERNAL_ERROR, "内部错误", 500)

    @app.get("/health")
    async def health() -> dict[str, str]:
        mysql, redis, milvus = await asyncio.gather(
            tcp_status(settings.mysql_host, settings.mysql_port, settings.health_timeout_s),
            uri_status(settings.redis_url, 6379, settings.health_timeout_s),
            uri_status(settings.milvus_uri, 19530, settings.health_timeout_s),
        )
        return {
            "mysql": mysql,
            "redis": redis,
            "milvus": milvus,
            "llm": "configured" if settings.qwen_api_key.get_secret_value().strip() else "unconfigured",
        }

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
        return await run_chat(payload, request_id=request.state.request_id)

    return app
