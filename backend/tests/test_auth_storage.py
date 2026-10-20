import asyncio
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from account_fakes import HEADERS, PASSWORD, login, register
from app.account_repo import AccountRepo
from app.account_store import digest
from app.auth import AccountService, SESSION_COOKIE
from app.contracts import AppError
from app.config import Settings
from ingest.migrate_db import ACCOUNT_TABLES, ensure_account_schema


class ScriptedMySQL:
    """只返回预置数据库行，捕获 SQL/绑定值，不启动数据库。"""
    def __init__(self, replies=(), error=None):
        self.replies = iter(replies)
        self.calls, self.error = [], error
        self.current = None
        self.lastrowid = 7
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self.error:
            raise self.error
        self.current = next(self.replies, None)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current or []

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_mysql_normalizes_username_and_parameters_never_interpolate():
    db = ScriptedMySQL()
    repo = AccountRepo(connection_factory=lambda: db)
    created = repo.create_user("Alice_01", "PRIVATE_HASH")
    sql, params = db.calls[0]
    assert params == ("alice_01", "Alice_01", "PRIVATE_HASH")
    assert "Alice_01" not in sql and "PRIVATE_HASH" not in sql
    assert created["id"] == 7 and db.committed and db.closed


@pytest.mark.parametrize("method,args", [
    ("owned_conversation", (1, "' OR 1=1 --")), ("conversation", (1, "' OR 1=1 --")),
    ("rename_conversation", (1, "' OR 1=1 --", "PRIVATE_TITLE")),
    ("delete_conversation", (1, "' OR 1=1 --")),
    ("append_message", (1, "' OR 1=1 --", "user", "PRIVATE_MESSAGE")),
])
def test_mysql_ownership_is_always_bound_before_access(method, args):
    db = ScriptedMySQL([None])
    with pytest.raises(AppError) as caught:
        getattr(AccountRepo(connection_factory=lambda: db), method)(*args)
    assert caught.value.code == 40401 and caught.value.http_status == 404
    sql, params = db.calls[0]
    assert "WHERE id=%s AND user_id=%s" in sql and params == (args[1], 1)
    assert args[1] not in sql and len(db.calls) == 1 and db.rolled_back and db.closed


def test_mysql_append_caps_content_and_sources_are_parameters():
    db = ScriptedMySQL([dict(id="c", title="", updated_at=datetime(2026, 1, 1))])
    AccountRepo(connection_factory=lambda: db).append_message(1, "c", "user", "私" * 4500)
    assert "FOR UPDATE" in db.calls[0][0]
    assert len(db.calls[1][1][2]) == 4000 and db.calls[2][1][0] == "私" * 20
    assert all("私" not in sql for sql, _ in db.calls)
    assert db.committed


def test_mysql_limits_create_under_user_lock():
    db = ScriptedMySQL([{"id": 1}, {"total": 100}])
    with pytest.raises(AppError) as caught:
        AccountRepo(connection_factory=lambda: db).create_conversation(1)
    assert caught.value.code == 40001
    assert "FOR UPDATE" in db.calls[0][0] and len(db.calls) == 2 and db.rolled_back


def test_mysql_session_issuance_and_delete_share_user_lock():
    db = ScriptedMySQL([{"id": 1}, None])
    seen = []
    repo = AccountRepo(connection_factory=lambda: db)
    repo.issue_session(1, lambda: seen.append("issue"))
    assert seen == ["issue"] and "FOR UPDATE" in db.calls[0][0]
    assert db.calls[1][0].startswith("UPDATE users")
    deleted = ScriptedMySQL([{"id": 1}, None])
    AccountRepo(connection_factory=lambda: deleted).delete_user(1, lambda: seen.append("revoke"))
    assert seen == ["issue", "revoke"] and "FOR UPDATE" in deleted.calls[0][0]
    assert deleted.calls[1] == ("DELETE FROM users WHERE id=%s", (1,))
    missing = ScriptedMySQL([None])
    with pytest.raises(AppError) as caught:
        AccountRepo(connection_factory=lambda: missing).issue_session(1, lambda: seen.append("unexpected"))
    assert caught.value.code == 40101 and seen == ["issue", "revoke"]


def test_mysql_returns_recent_200_in_chronological_order():
    db = ScriptedMySQL([
        dict(id="c", title="t", updated_at=datetime(2026, 1, 1)),
        [dict(role="assistant", content="later", route="faq", sources='[{"index":1}]',
              created_at=datetime(2026, 1, 2)),
         dict(role="user", content="earlier", route=None, sources=None, created_at=datetime(2026, 1, 1))],
    ])
    result = AccountRepo(connection_factory=lambda: db).conversation(1, "c")
    assert [item["content"] for item in result["messages"]] == ["earlier", "later"]
    assert result["messages"][-1]["sources"] == [{"index": 1}]
    assert result["messages"][0]["created_at"].endswith("+00:00")
    assert "ORDER BY created_at DESC, id DESC LIMIT 200" in db.calls[1][0]


@pytest.mark.parametrize("failure,code", [(OSError("PRIVATE_DB_PASSWORD"), 50301),
                                         (RuntimeError(1062, "PRIVATE_HASH"), 40901)])
def test_mysql_errors_are_safe_and_close_connections(failure, code):
    db = ScriptedMySQL(error=failure)
    with pytest.raises(AppError) as caught:
        AccountRepo(connection_factory=lambda: db).create_user("Alice_01", "PRIVATE_HASH")
    assert caught.value.code == code and "PRIVATE" not in str(caught.value)
    assert db.rolled_back and db.closed


def test_schema_is_idempotent_ddl_with_cascades():
    db = ScriptedMySQL()
    ensure_account_schema(db)
    ensure_account_schema(db)
    assert len(db.calls) == 6
    assert [sql for sql, _ in db.calls[:3]] == list(ACCOUNT_TABLES)
    assert all(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql, _ in db.calls)
    assert sum("ON DELETE CASCADE" in sql for sql, _ in db.calls[:3]) == 2


def test_hash_and_mysql_work_do_not_block_event_loop(account_factory):
    async def run():
        app, repo, _, _ = account_factory()
        entered, ticked = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop, caller = asyncio.get_running_loop(), threading.get_ident()
        original = repo.create_user
        def blocked(*args):
            assert threading.get_ident() != caller
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            return original(*args)
        repo.create_user = blocked
        task = asyncio.create_task(app.state.accounts.register("Alice_01", PASSWORD, "ip"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            loop.call_soon(ticked.set)
            await asyncio.wait_for(ticked.wait(), .5)
        finally:
            release.set()
            await task
            await app.state.accounts.aclose()
            await app.state.quota.aclose()
            await app.state.rate_limiter.aclose()
    asyncio.run(run())


def test_sanitized_validation_failure_and_sessions_never_enter_logs(account_factory, caplog):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        malformed = api.post("/api/v1/auth/register", headers=HEADERS,
                             json={"username": {"password": PASSWORD}, "password": PASSWORD})
        assert malformed.status_code == 400 and PASSWORD not in malformed.text
        register(api)
        token = api.cookies[SESSION_COOKIE]
        app.state.accounts.repo.find_user = lambda value: (_ for _ in ()).throw(RuntimeError(PASSWORD + token))
        failed = login(api)
        assert failed.status_code == 500 and failed.json()["code"] == 50001
        assert PASSWORD not in failed.text + caplog.text and token not in failed.text + caplog.text


def test_valid_session_redis_failure_falls_back_to_guest_and_history_stays_private(account_factory):
    app, _, redis, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        redis.down = True
        me = api.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["user"] is None
        assert me.json()["quota"]["role"] == "guest"
        assert api.get("/api/v1/conversations").status_code == 401
        # 撤销会话仍需成功写 Redis，不能假装已在服务端登出。
        logout = api.post("/api/v1/auth/logout", headers=HEADERS)
        assert logout.status_code == 503 and logout.json()["code"] == 50301


def test_new_secret_invalidates_guest_and_sessions(account_factory):
    app, _, _, _ = account_factory()
    with TestClient(app) as api:
        register(api)
        old_guest = api.cookies["ecomrag_guest"]
        app.state.accounts.secret = "new-process-secret"
        assert api.get("/api/v1/auth/me").json()["user"] is None
        assert api.cookies["ecomrag_guest"] != old_guest


def test_register_session_failure_compensates_created_user(account_factory):
    app, repo, _, _ = account_factory()
    def fail(*args, **kwargs):
        raise AppError(50301, 503, "账号服务暂时不可用，请稍后再试")
    app.state.accounts.store.new_session = fail
    with TestClient(app) as api:
        result = register(api)
        assert result.status_code == 503 and not repo.users


def test_config_scrypt_bounds_and_missing_secret_warning(account_factory, caplog):
    for value in (32767, 32769, 524288):
        with pytest.raises(ValueError):
            Settings(_env_file=None, auth_scrypt_n=value)
    assert Settings(_env_file=None, auth_scrypt_n=65536).auth_scrypt_n == 65536
    app, _, _, _ = account_factory(auth_secret="", debug=True)
    with TestClient(app):
        assert app.state.accounts.secret
    assert "重启后游客与会话将失效" in caplog.text


def test_local_security_capacity_fails_closed_without_evicting(account_factory, caplog):
    app, _, redis, _ = account_factory()
    with TestClient(app):
        store = app.state.accounts.store
        store.capacity = 1
        redis.down = True
        for _ in range(4):
            store.login_failure("alice")
        with pytest.raises(AppError) as caught:
            store.login_failure("bob")
        assert caught.value.code == 50302
        with pytest.raises(AppError) as caught:
            store.login_failure("alice")
        assert caught.value.code == 42903
        warnings = [record for record in caplog.records if "账号防暴力破解 Redis 不可用" in record.getMessage()]
        assert len(warnings) == 1
