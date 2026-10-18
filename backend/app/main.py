import asyncio
import json
import logging
from contextlib import asynccontextmanager
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException

from app import config
from app.chat_log import ChatLog, ChatLogWriter, persist
from app.contracts import (
    AppError,
    HTTP_ERROR,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
)
from app.logging_config import configure_logging, log_exception, request_id as request_id_context
from app.pipeline import run_chat
from app.rate_limit import RateLimiter, client_ip
from app.schemas import ChatRequest, ChatResponse
from app.services import Services

logger = logging.getLogger(__name__)


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


def create_app(settings: config.Settings | None = None, *, services=None, chat_log_writer=None) -> FastAPI:
    settings = settings if settings is not None else config.get_settings()
    configure_logging(settings)
    chat_logs = ChatLogWriter(settings) if chat_log_writer is None else chat_log_writer
    owned = services is None
    services = Services(settings) if owned else services
    limiter = RateLimiter(settings)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if settings.chat_log_enabled and callable(getattr(chat_logs, "start", None)):
                chat_logs.start()
            if settings.warmup_on_start and settings.chat_log_enabled:
                try:
                    await chat_logs.ensure_schema()
                except Exception:
                    logger.warning("对话表补列失败，请运行 ingest.migrate_db")
            if settings.warmup_on_start and hasattr(services, "warmup"):
                await services.warmup()
            yield
        finally:
            try:
                if owned:
                    await services.aclose()
            finally:
                try:
                    await limiter.aclose()
                finally:
                    if settings.chat_log_enabled and callable(getattr(chat_logs, "aclose", None)):
                        await chat_logs.aclose()

    app = FastAPI(
        lifespan=lifespan,
        title="电商 RAG 智能客服",
        debug=settings.debug,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    app.state.services = services
    app.state.chat_logs = chat_logs
    app.state.rate_limiter = limiter
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
        token = request_id_context.set(request.state.request_id)
        started, status = perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        except Exception as exc:
            # 不再把带原文的异常交给 ASGI 服务器重复记录。
            return await unexpected_error_handler(request, exc)
        finally:
            logger.info("request_done method=%s path=%s status=%d ms=%.3f",
                        request.method, json.dumps(request.url.path), status,
                        (perf_counter() - started) * 1000)
            request_id_context.reset(token)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        response = error_response(request, exc.code, exc.message, exc.http_status)
        payload = getattr(request.state, "chat_payload", None)
        if settings.chat_log_enabled and payload is not None:
            row = ChatLog(request.state.request_id, payload.session_id, payload.message, "", "error",
                          (perf_counter() - request.state.chat_started) * 1000, error_code=exc.code)
            response.background = BackgroundTask(persist, chat_logs, row)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return error_response(request, INVALID_ARGUMENT, "请求参数非法", 400)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        code = {
            404: NOT_FOUND,
            405: METHOD_NOT_ALLOWED,
        }.get(exc.status_code, HTTP_ERROR if exc.status_code < 500 else INTERNAL_ERROR)
        message = {404: "请求的地址不存在", 405: "请求方法不允许"}.get(
            exc.status_code, "请求无法处理" if exc.status_code < 500 else "服务暂时不可用")
        response = error_response(request, code, message, exc.status_code)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        if not getattr(request.state, "unexpected_logged", False):
            log_exception(logger, exc)
            request.state.unexpected_logged = True
        response = error_response(request, INTERNAL_ERROR, "内部错误", 500)
        payload = getattr(request.state, "chat_payload", None)
        if settings.chat_log_enabled and payload is not None:
            row = ChatLog(request.state.request_id, payload.session_id, payload.message, "", "error",
                          (perf_counter() - request.state.chat_started) * 1000, error_code=INTERNAL_ERROR)
            response.background = BackgroundTask(persist, chat_logs, row)
        return response

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
            "llm": "configured" if settings.llm_api_key.get_secret_value().strip() else "unconfigured",
        }

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, request: Request, background_tasks: BackgroundTasks) -> ChatResponse:
        started = perf_counter()
        request.state.chat_payload, request.state.chat_started = payload, started
        trace, result = [], None
        try:
            result = await run_chat(payload, request_id=request.state.request_id,
                                    settings=settings, services=services, trace_sink=trace,
                                    before_chat=lambda: limiter.check_async(client_ip(request, settings), payload.session_id))
            if settings.chat_log_enabled:
                row = ChatLog(result.request_id, result.session_id, payload.message, result.answer,
                              result.route, (perf_counter() - started) * 1000, strategy=result.strategy,
                              sources=[source.model_dump() for source in result.sources])
                background_tasks.add_task(persist, chat_logs, row)
            return result
        finally:
            stages = {}
            for item in trace:
                stages[item.stage] = round(stages.get(item.stage, 0) + item.ms, 3)
            session = payload.session_id
            # session 是用户输入，避免意外与原文或 key 相同，并转义换行。
            for secret in (payload.message, result.answer if result else "",
                           settings.llm_api_key.get_secret_value()):
                if secret:
                    session = session.replace(secret, "[redacted]")
            logger.info("chat_done session_id=%s route=%s strategy=%s total_ms=%.3f stages_ms=%s documents=%d",
                        json.dumps(session), result.route if result else "error",
                        result.strategy if result else None, (perf_counter() - started) * 1000,
                        json.dumps(stages), len(result.sources) if result else 0)

    return app
