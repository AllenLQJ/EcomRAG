"""标准库 scrypt、签名 Cookie 与账号服务；不记录凭据或业务内容。"""
import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
from ipaddress import ip_address, ip_network

from app.account_repo import AccountRepo
from app.account_store import AccountStore, SESSION_TTL, unavailable
from app.contracts import AppError, INVALID_ARGUMENT, UNAUTHORIZED
from app.executor import BoundedExecutor
from app.message_limits import bounded_message

logger = logging.getLogger(__name__)
SESSION_COOKIE = "ecomrag_session"
GUEST_COOKIE = "ecomrag_guest"
WEAK_PASSWORDS = frozenset({
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "admin123", "admin12345", "welcome123", "iloveyou",
    "letmein123", "abc12345", "abcd1234", "123456abc", "11111111", "88888888",
    "00000000", "asdfghjk", "changeme", "passw0rd", "administrator",
})


def validate_password_length(password):
    if len(password) > 64:
        raise AppError(INVALID_ARGUMENT, 400, "密码长度须为8–64位")


def validate_registration(username, password):
    validate_password_length(password)
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        raise AppError(INVALID_ARGUMENT, 400, "用户名须为3–20位英文字母、数字或下划线")
    if not 8 <= len(password) <= 64:
        raise AppError(INVALID_ARGUMENT, 400, "密码长度须为8–64位")
    if password.lower() == username.lower():
        raise AppError(INVALID_ARGUMENT, 400, "密码不能与用户名相同")
    if password.isdigit():
        raise AppError(INVALID_ARGUMENT, 400, "密码不能为纯数字")
    if password.lower() in WEAK_PASSWORDS:
        raise AppError(INVALID_ARGUMENT, 400, "密码过于常见，请使用更安全的密码")


def hash_password(password, n=32768, *, salt=None):
    validate_password_length(password)
    salt = secrets.token_bytes(16) if salt is None else salt
    value = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, dklen=32,
                           maxmem=max(64 * 1024 * 1024, 256 * n * 8))
    return f"scrypt${n}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(value).decode()}"


def verify_password(password, encoded):
    validate_password_length(password)
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or n < 32768 or n > 262144 or n & (n - 1) or (r, p) != (8, 1):
            return False
        salt, expected = base64.b64decode(salt, validate=True), base64.b64decode(expected, validate=True)
        if len(salt) != 16 or len(expected) != 32:
            return False
        value = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32,
                               maxmem=max(64 * 1024 * 1024, 256 * n * 8))
        return hmac.compare_digest(value, expected)
    except (ValueError, TypeError):
        return False


def secure_request(request, settings):
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else "unknown"
    try:
        trusted = settings.trust_proxy_headers and any(
            ip_address(peer) in ip_network(network, strict=False) for network in settings.trusted_proxies)
    except ValueError:
        trusted = False
    return trusted and request.headers.get("x-forwarded-proto", "").strip().lower() == "https"


def set_cookie(response, name, value, request, settings):
    response.set_cookie(name, value, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
                        secure=secure_request(request, settings))


def clear_cookie(response, request, settings):
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=secure_request(request, settings))


class AccountService:
    def __init__(self, settings, *, repo=None, store=None):
        self.settings = settings
        self.repo = repo if repo is not None else AccountRepo(settings)
        self.store = store if store is not None else AccountStore(settings)
        self.secret = settings.auth_secret.get_secret_value()
        if not self.secret.strip():
            if not settings.debug:
                raise ValueError("非开发模式必须配置非空 AUTH_SECRET，所有 worker 必须共享同一密钥")
            self.secret = secrets.token_urlsafe(32)
            logger.warning("开发模式 AUTH_SECRET 未配置，重启后游客与会话将失效；随机密钥不支持多 worker")
        self.executor = BoundedExecutor(settings.auth_io_workers, settings.auth_io_max_in_flight, name="account-io")
        self._dummy, self._dummy_lock = None, threading.Lock()

    def signed_token(self, purpose):
        value = secrets.token_urlsafe(32)
        return value + "." + self.signature(purpose, value)

    def signature(self, purpose, value):
        return hmac.new(self.secret.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def valid_token(self, token, purpose):
        if purpose == "session":
            return bool(token and re.fullmatch(r"[A-Za-z0-9_-]{43}", token))
        if not token or len(token) != 108:
            return False
        value, _, signature = token.partition(".")
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", value) and
                    hmac.compare_digest(signature, self.signature(purpose, value)))

    def guest(self, token):
        return token if self.valid_token(token, "guest") else self.signed_token("guest")

    async def call(self, method, *args, **kwargs):
        return await self.executor.run(getattr(self.repo, method), *args, **kwargs)

    async def current(self, request, *, required=False):
        if not hasattr(request.state, "account_user"):
            token = request.cookies.get(SESSION_COOKIE)
            user = None
            if self.valid_token(token, "session"):
                def load():
                    user_id = self.store.session(token, self.signature("session", token))
                    return self.repo.get_user(user_id) if user_id is not None else None
                user = await self.executor.run(load)
                if user and user.get("status") != 1:
                    user = None
            request.state.account_user = user
        user = request.state.account_user
        if required and user is None:
            raise AppError(UNAUTHORIZED, 401, "请先登录")
        return user

    @staticmethod
    def public(user):
        return {"id": user["id"], "username": user["username"]} if user else None

    def _password_check(self, password, user):
        validate_password_length(password)
        with self._dummy_lock:
            if self._dummy is None:
                self._dummy = hash_password("dummy-comparison-value", self.settings.auth_scrypt_n)
        # 两种认证失败均执行一次 scrypt 和恒定时间比较。
        matches = verify_password(password, user["password_hash"] if user else self._dummy)
        return bool(user and user.get("status") == 1 and matches)

    def _register(self, username, password, ip, old_token):
        validate_password_length(password)
        self.store.attempt(ip)
        validate_registration(username, password)
        try:
            self.store.client().ping()
        except self.store.cache._redis_errors:
            raise unavailable() from None
        user = self.repo.create_user(username, hash_password(password, self.settings.auth_scrypt_n))
        token = secrets.token_urlsafe(32)
        try:
            self.repo.issue_session(user["id"], lambda: self.store.new_session(
                user["id"], token, old_token, signature=self.signature("session", token)))
        except AppError:
            # 创建会话失败时补偿新账号；避免客户端重试只能得到用户名冲突。
            try:
                self.repo.delete_user(user["id"], lambda: self.store.revoke_all(user["id"]))
            except Exception:
                logger.warning("注册会话失败，账号补偿删除未完成")
            raise
        return self.public(user), token

    def _login(self, username, password, ip, old_token):
        validate_password_length(password)
        self.store.attempt(ip)
        username = username.lower()
        try:
            self.store.client().ping()
        except self.store.cache._redis_errors:
            raise unavailable() from None
        user = self.repo.find_user(username)
        if user is not None:
            self.store.check_lock(user["id"])
        if not self._password_check(password, user):
            # 不为不存在的用户名创建锁定；按不可复用的账号 id 隔离旧用户名计数。
            if user is not None:
                self.store.login_failure(user["id"])
            raise AppError(UNAUTHORIZED, 401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        def issue():
            self.store.new_session(user["id"], token, old_token, signature=self.signature("session", token))
            self.store.login_success(user["id"])
        self.repo.issue_session(user["id"], issue)
        return self.public(user), token

    async def register(self, username, password, ip, old_token=None):
        validate_password_length(password)
        return await self.executor.run(self._register, username, password, ip, old_token)

    async def login(self, username, password, ip, old_token=None):
        validate_password_length(password)
        return await self.executor.run(self._login, username, password, ip, old_token)

    async def logout(self, token):
        await self.executor.run(self.store.remove_session, token)

    async def delete_account(self, user, password, ip):
        validate_password_length(password)
        def remove():
            self.store.attempt(ip)
            self.store.check_lock(user["id"])
            if not self._password_check(password, user):
                self.store.login_failure(user["id"])
                raise AppError(UNAUTHORIZED, 401, "用户名或密码错误")
            self.repo.delete_user(user["id"], lambda: self.store.revoke_all(user["id"]))
        await self.executor.run(remove)

    async def save(self, user_id, conversation_id, role, content, route=None, sources=None):
        try:
            content, route, compact = bounded_message(content, route, sources)
            await self.call("append_message", user_id, conversation_id, role, content, route, compact)
        except Exception:
            logger.warning("服务器端历史保存失败，已跳过")

    async def aclose(self):
        await self.executor.aclose(lambda: self.store.cache.client.close()
                                   if self.store.cache.client is not None else None)
