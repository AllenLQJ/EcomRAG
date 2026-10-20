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
from app.account_api import account_router
from app.auth import AccountService, GUEST_COOKIE, set_cookie
from app.chat_log import ChatLog, ChatLogWriter, persist
from app.contracts import (
    AppError,
    HTTP_ERROR,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
    INVALID_USERNAME,
    INVALID_PASSWORD,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    FORBIDDEN,
    RATE_LIMITED,
    DAILY_RATE_LIMITED,
)
from app.logging_config import configure_logging, log_exception, request_id as request_id_context
from app.pipeline import run_chat
from app.rate_limit import RateLimiter, client_ip
from app.quota import DailyQuota
from app.schemas import ChatRequest, ChatResponse
from app.services import Services
from app.streaming import SSEStreamingResponse, chat_events

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


def error_response(request: Request, code: int, message: str, status: int, **details) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "request_id": request_id, **details},
        headers={"X-Request-ID": request_id},
    )


def allowed_origin(request: Request, origin: str, whitelist: list[str]) -> bool:
    if len(request.headers.getlist("origin")) != 1:
        return False
    if origin in whitelist:
        return True
    # Caddy preserves Host, but its upstream scheme can be HTTP after TLS termination.
    # Compare the raw Host authority, never X-Forwarded-Host/Proto (even if supplied).
    # Using the Origin scheme to resolve implicit ports also supports that deployment.
    try:
        hosts = request.headers.getlist("host")
        if len(hosts) != 1:
            return False
        host = hosts[0]
        if (not host or host.endswith(":") or origin.endswith(":") or
                any(char.isspace() for char in origin + host) or
                any(char in host for char in "/?#@\\") or any(char in origin for char in "?#")):
            return False
        source = urlsplit(origin)
        target = urlsplit(f"{source.scheme}://{host}")
        if (source.scheme not in {"http", "https"} or not source.hostname or
                source.username is not None or source.password is not None or
                source.path or source.query or source.fragment or "\\" in origin):
            return False
        default_port = 443 if source.scheme == "https" else 80
        return (source.hostname.lower(), source.port if source.port is not None else default_port) == (
            target.hostname.lower(), target.port if target.port is not None else default_port)
    except (ValueError, AttributeError):
        return False


def create_app(settings: config.Settings | None = None, *, services=None, chat_log_writer=None,
               account_service=None, daily_quota=None) -> FastAPI:
    settings = settings if settings is not None else config.get_settings()
    configure_logging(settings)
    logger.info("capacity_config llm=%d rerank=%d model_workers=%d model_queue=%d "
                "rate_workers=%d rate_in_flight=%d per_minute=%d ip_limit=%s",
                settings.llm_max_concurrency, settings.rerank_remote_max_concurrency,
                settings.model_io_workers, settings.model_io_queue_size,
                settings.rate_limit_io_workers, settings.rate_limit_io_max_in_flight,
                settings.rate_limit_per_min, settings.rate_limit_ip_enabled)
    # 在初始化其他资源之前拒绝缺失的正式环境签名密钥。
    accounts = account_service if account_service is not None else AccountService(settings)
    chat_logs = ChatLogWriter(settings) if chat_log_writer is None else chat_log_writer
    owned = services is None
    services = Services(settings) if owned else services
    limiter = RateLimiter(settings)
    quota = daily_quota if daily_quota is not None else DailyQuota(settings)

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
                    try:
                        await asyncio.gather(accounts.aclose(), quota.aclose())
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
    app.state.accounts = accounts
    app.state.quota = quota
    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = str(uuid4())
        token = request_id_context.set(request.state.request_id)
        started, status = perf_counter(), 500
        guest_token = None
        try:
            if request.url.path.startswith("/api/v1/"):
                old_guest = request.cookies.get(GUEST_COOKIE)
                request.state.guest_id = accounts.guest(old_guest)
                if request.state.guest_id != old_guest:
                    guest_token = request.state.guest_id
            protected = (request.url.path == "/api/v1/auth" or request.url.path.startswith("/api/v1/auth/") or
                         request.url.path == "/api/v1/conversations" or
                         request.url.path.startswith("/api/v1/conversations/"))
            origin = request.headers.get("origin")
            invalid_origin = origin is not None and not allowed_origin(request, origin, settings.cors_origins)
            invalid_chat_origin = (
                request.method == "POST" and request.url.path in {"/api/v1/chat", "/api/v1/chat/stream"} and
                invalid_origin
            )
            if invalid_chat_origin or (protected and request.method != "OPTIONS" and (
                invalid_origin or
                (request.method in {"POST", "PATCH", "DELETE"} and
                 request.headers.get("x-requested-with") != "ecomrag")
            )):
                response = error_response(request, FORBIDDEN, "请求来源校验失败", 403)
            else:
                response = await call_next(request)
            status = response.status_code
            if guest_token is not None:
                set_cookie(response, GUEST_COOKIE, guest_token, request, settings)
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        except Exception as exc:
            # 不再把带原文的异常交给 ASGI 服务器重复记录。
            response = await unexpected_error_handler(request, exc)
            if guest_token is not None:
                set_cookie(response, GUEST_COOKIE, guest_token, request, settings)
            return response
        finally:
            audit_path = request.url.path
            if audit_path.startswith("/api/v1/conversations/"):
                audit_path = "/api/v1/conversations/{id}"
            elif audit_path.startswith("/api/v1/auth/") and audit_path not in {
                "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/logout",
                "/api/v1/auth/me", "/api/v1/auth/account",
            }:
                audit_path = "/api/v1/auth/{redacted}"
            logger.info("request_done method=%s path=%s status=%d ms=%.3f",
                        request.method, json.dumps(audit_path), status,
                        (perf_counter() - started) * 1000)
            request_id_context.reset(token)

    # CORS 位于请求中间件外层，CSRF/认证失败响应同样带允许来源的凭据头。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Requested-With"],
        allow_credentials=True,
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        response = error_response(request, exc.code, exc.message, exc.http_status, **exc.details)
        if "retry_after_seconds" in exc.details:
            response.headers["Retry-After"] = str(exc.details["retry_after_seconds"])
        payload = getattr(request.state, "chat_payload", None)
        if settings.chat_log_enabled and payload is not None and not getattr(request.state, "private_chat", False):
            row = ChatLog(request.state.request_id, payload.session_id, payload.message, "", "error",
                          (perf_counter() - request.state.chat_started) * 1000, error_code=exc.code,
                          history_count=len(payload.history))
            response.background = BackgroundTask(persist, chat_logs, row)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/v1/auth/register":
            fields = {error["loc"][-1] for error in exc.errors()}
            if "username" in fields:
                return error_response(request, INVALID_USERNAME, "用户名须为3–20位英文字母、数字或下划线", 400)
            if "password" in fields:
                return error_response(request, INVALID_PASSWORD, "密码长度须为8–64位", 400)
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
        if settings.chat_log_enabled and payload is not None and not getattr(request.state, "private_chat", False):
            row = ChatLog(request.state.request_id, payload.session_id, payload.message, "", "error",
                          (perf_counter() - request.state.chat_started) * 1000, error_code=INTERNAL_ERROR,
                          history_count=len(payload.history))
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

    app.include_router(account_router(settings, accounts, quota))

    async def chat_context(payload, request):
        user = await accounts.current(request)
        request.state.private_chat = user is not None
        conversation_id = payload.conversation_id if user else None
        if conversation_id is not None:
            await accounts.call("owned_conversation", user["id"], conversation_id)

        async def before_chat():
            ip = client_ip(request, settings)
            rate_error = None
            try:
                await limiter.check_async(ip, payload.session_id)
            except AppError as exc:
                if exc.code not in {RATE_LIMITED, DAILY_RATE_LIMITED}:
                    raise
                rate_error = exc
            # 两套计数均沿用入口检查时机；同时超限时优先给出角色日配额提示。
            await quota.check(user["id"] if user else None, request.state.guest_id, ip)
            if rate_error is not None:
                raise rate_error
            if conversation_id is not None:
                await accounts.save(user["id"], conversation_id, "user", payload.message)

        async def after_chat(result):
            if conversation_id is not None:
                await accounts.save(user["id"], conversation_id, "assistant", result.answer, result.route,
                                    (source.model_dump() for source in result.sources), request_id=result.request_id)

        return user, before_chat, after_chat

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, request: Request, background_tasks: BackgroundTasks) -> ChatResponse:
        started = perf_counter()
        request.state.chat_payload, request.state.chat_started = payload, started
        trace, result = [], None
        try:
            user, before_chat, after_chat = await chat_context(payload, request)
            result = await run_chat(payload, request_id=request.state.request_id,
                                    settings=settings, services=services, trace_sink=trace,
                                    before_chat=before_chat)
            await after_chat(result)
            if settings.chat_log_enabled and not request.state.private_chat:
                row = ChatLog(result.request_id, result.session_id, payload.message, result.answer,
                              result.route, (perf_counter() - started) * 1000, strategy=result.strategy,
                              sources=[source.model_dump() for source in result.sources],
                              history_count=len(payload.history))
                background_tasks.add_task(persist, chat_logs, row)
            return result
        finally:
            stages = {}
            for item in trace:
                stages[item.stage] = round(stages.get(item.stage, 0) + item.ms, 3)
            # 多轮不输出客户端 session，避免客户端把历史片段放入标识造成旁路泄露。
            session = ("[redacted]" if payload.history or getattr(request.state, "private_chat", False)
                       or accounts.valid_token(payload.session_id, "session")
                       else payload.session_id)
            # session 是用户输入，避免意外与原文或 key 相同，并转义换行。
            for secret in (payload.message, result.answer if result else "",
                           settings.llm_api_key.get_secret_value(), *request.cookies.values()):
                if secret:
                    session = session.replace(secret, "[redacted]")
            logger.info("chat_done session_id=%s route=%s strategy=%s total_ms=%.3f stages_ms=%s documents=%d history_count=%d",
                        json.dumps(session), result.route if result else "error",
                        result.strategy if result else None, (perf_counter() - started) * 1000,
                        json.dumps(stages), len(result.sources) if result else 0, len(payload.history))

    @app.post("/api/v1/chat/stream")
    async def chat_stream(payload: ChatRequest, request: Request):
        user, before_chat, after_chat = await chat_context(payload, request)
        return SSEStreamingResponse(
            chat_events(payload, request.state.request_id,
                        settings=settings.model_copy(update={"chat_log_enabled": False})
                        if request.state.private_chat else settings,
                        services=services, before_chat=before_chat, after_chat=after_chat, chat_logs=chat_logs),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Content-Encoding": "identity"},
        )

    return app
