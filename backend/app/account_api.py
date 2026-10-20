"""账号及历史 HTTP 接口：成功体保持现有的直接 JSON 风格。"""
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.auth import SESSION_COOKIE, clear_cookie, set_cookie, validate_password_length
from app.rate_limit import client_ip
from app.message_limits import MAX_CONTENT_CHARS, MAX_ELAPSED_MS, MAX_SOURCES, bounded_message
from app.schemas import Route, Source


class PasswordBody(BaseModel):
    password: SecretStr = Field(repr=False, max_length=64)

    @field_validator("password", mode="before")
    @classmethod
    def limit_password(cls, value):
        if isinstance(value, (str, SecretStr)):
            validate_password_length(value)
        return value


class Credentials(PasswordBody):
    username: str = Field(max_length=4096)


class ConversationCreate(BaseModel):
    title: str = Field(default="", max_length=100)


class ConversationRename(BaseModel):
    title: str = Field(max_length=100)


class AssistantMessageBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    route: Route | None = None
    sources: list[Source] | None = Field(default=None, max_length=MAX_SOURCES)
    elapsed_ms: int | None = Field(default=None, strict=True, ge=0, le=MAX_ELAPSED_MS)

    @field_validator("request_id")
    @classmethod
    def reject_blank(cls, value):
        if not value.strip():
            raise ValueError("请求标识不能为空白")
        return value


def account_router(settings, accounts, quota):
    router = APIRouter(prefix="/api/v1")

    @router.post("/auth/register", status_code=201)
    async def register(body: Credentials, request: Request, response: Response):
        user, token = await accounts.register(body.username, body.password.get_secret_value(),
                                              client_ip(request, settings), request.cookies.get(SESSION_COOKIE))
        set_cookie(response, SESSION_COOKIE, token, request, settings)
        return {"user": user}

    @router.post("/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        user, token = await accounts.login(body.username, body.password.get_secret_value(),
                                           client_ip(request, settings), request.cookies.get(SESSION_COOKIE))
        set_cookie(response, SESSION_COOKIE, token, request, settings)
        return {"user": user}

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response):
        await accounts.logout(request.cookies.get(SESSION_COOKIE))
        clear_cookie(response, request, settings)
        return {}

    @router.get("/auth/me")
    async def me(request: Request):
        user = await accounts.current(request)
        usage = await quota.read(user["id"] if user else None, request.state.guest_id, client_ip(request, settings))
        return {"user": accounts.public(user), "quota": usage}

    @router.delete("/auth/account")
    async def delete_account(body: PasswordBody, request: Request, response: Response):
        user = await accounts.current(request, required=True)
        await accounts.delete_account(user, body.password.get_secret_value(), client_ip(request, settings))
        clear_cookie(response, request, settings)
        return {}

    @router.get("/conversations")
    async def list_conversations(request: Request):
        user = await accounts.current(request, required=True)
        return {"items": await accounts.call("list_conversations", user["id"])}

    @router.post("/conversations", status_code=201)
    async def create_conversation(request: Request, body: ConversationCreate = ConversationCreate()):
        user = await accounts.current(request, required=True)
        return await accounts.call("create_conversation", user["id"], body.title)

    @router.get("/conversations/{conversation_id}")
    async def conversation(conversation_id: str, request: Request):
        user = await accounts.current(request, required=True)
        return await accounts.call("conversation", user["id"], conversation_id)

    @router.patch("/conversations/{conversation_id}")
    async def rename(conversation_id: str, body: ConversationRename, request: Request):
        user = await accounts.current(request, required=True)
        return await accounts.call("rename_conversation", user["id"], conversation_id, body.title)

    @router.post("/conversations/{conversation_id}/messages")
    async def save_assistant(conversation_id: str, body: AssistantMessageBody, request: Request):
        user = await accounts.current(request, required=True)
        content, route, sources = bounded_message(body.content, body.route,
            (source.model_dump() for source in body.sources) if body.sources is not None else None,
            body.elapsed_ms, body.request_id)
        await accounts.call("append_message", user["id"], conversation_id, "assistant", content, route, sources,
                            elapsed_ms=body.elapsed_ms, request_id=body.request_id)
        return {}

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request):
        user = await accounts.current(request, required=True)
        await accounts.call("delete_conversation", user["id"], conversation_id)
        return {}

    return router
