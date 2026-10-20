"""离线账号仓库与支持事务/TTL/Lua 语义的 Redis 替身。"""
import json
import threading
from collections import defaultdict
from uuid import uuid4

from app.account_store import ATTEMPT_SCRIPT, FAIL_SCRIPT, LOCK_SCRIPT, AccountStore
from app.auth import AccountService
from app.config import Settings
from app.contracts import AppError
from app.main import create_app
from app.quota import DailyQuota
from chat_fakes import FakeServices

HEADERS = {"X-Requested-With": "ecomrag"}
PASSWORD = "A-unique-password!"


class FakeRedis:
    def __init__(self, clock):
        self.clock = clock
        self.values, self.expires, self.zsets = {}, {}, defaultdict(dict)
        self.down = False
        self.lock = threading.RLock()

    def _check(self):
        if self.down:
            raise OSError("PRIVATE_REDIS_ERROR")
        for key, end in list(self.expires.items()):
            if end <= self.clock():
                self.values.pop(key, None)
                self.zsets.pop(key, None)
                del self.expires[key]

    def ping(self):
        self._check()
        return True

    def get(self, key):
        self._check()
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self._check()
        self.values[key] = value
        if ex:
            self.expire(key, ex)
        return True

    def delete(self, *keys):
        self._check()
        for key in keys:
            self.values.pop(key, None)
            self.expires.pop(key, None)
            self.zsets.pop(key, None)
        return len(keys)

    def expire(self, key, ttl):
        self._check()
        self.expires[key] = self.clock() + ttl
        return True

    def incr(self, key):
        value = int(self.get(key) or 0) + 1
        self.values[key] = value
        return value

    def sadd(self, key, value):
        self._check()
        self.values.setdefault(key, set()).add(value)
        return 1

    def srem(self, key, value):
        self._check()
        self.values.get(key, set()).discard(value)
        return 1

    def eval(self, script, number, key, *args):
        assert number == 1
        with self.lock:
            self._check()
            if script == ATTEMPT_SCRIPT:
                now, member = args
                entries = self.zsets[key]
                for item, score in list(entries.items()):
                    if score <= now - 600:
                        del entries[item]
                if len(entries) >= 20:
                    import math
                    return max(1, math.ceil(min(entries.values()) + 600 - now))
                entries[member] = now
                self.expire(key, 600)
                return 0
            if script in {FAIL_SCRIPT, LOCK_SCRIPT}:
                count = self.incr(key) if script == FAIL_SCRIPT else int(self.get(key) or 0)
                if script == FAIL_SCRIPT and count == 5:
                    self.expire(key, 900)
                return [count, int(self.expires[key] - self.clock()) if key in self.expires else -1]
            assert "SMEMBERS" in script
            for session in list(self.values.get(key, set())):
                self.delete(session)
                self.delete("auth:session_guard:" + session[len("auth:session:"):])
            return self.delete(key)

    def pipeline(self, transaction=True):
        assert transaction
        owner = self

        class Pipeline:
            def __init__(self):
                self.commands = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __getattr__(self, name):
                def enqueue(*args, **kwargs):
                    self.commands.append((name, args, kwargs))
                    return self
                return enqueue

            def execute(self):
                with owner.lock:
                    owner._check()
                    return [getattr(owner, name)(*args, **kwargs) for name, args, kwargs in self.commands]

        return Pipeline()

    def close(self):
        pass


class MemoryRepo:
    def __init__(self):
        self.users, self.conversations = {}, {}
        self.messages = defaultdict(list)
        self.lock = threading.RLock()
        self.save_fails = False
        self.sequence = 0

    def create_user(self, username, password_hash):
        with self.lock:
            if self.find_user(username.lower()):
                raise AppError(40901, 409, "用户名已被使用")
            user = dict(id=len(self.users) + 1, username=username, password_hash=password_hash, status=1)
            self.users[user["id"]] = user
            return dict(user)

    def find_user(self, username):
        return next((dict(user) for user in self.users.values() if user["username"].lower() == username), None)

    def get_user(self, user_id):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def issue_session(self, user_id, issue):
        with self.lock:
            if user_id not in self.users or self.users[user_id]["status"] != 1:
                raise AppError(40101, 401, "用户名或密码错误")
            issue()
            self.users[user_id]["last_login_at"] = "now"

    def delete_user(self, user_id, before_delete=None):
        with self.lock:
            if before_delete:
                before_delete()
            self.users.pop(user_id, None)
            for cid, item in list(self.conversations.items()):
                if item["user_id"] == user_id:
                    self.delete_conversation(user_id, cid)

    def summary(self, item):
        return {key: item[key] for key in ("id", "title", "updated_at")} | {
            "message_count": len(self.messages[item["id"]])}

    def stamp(self):
        self.sequence += 1
        return f"2026-10-19T12:00:00.{self.sequence:06d}+00:00"

    def owned_conversation(self, user_id, cid):
        item = self.conversations.get(cid)
        if item is None or item["user_id"] != user_id:
            raise AppError(40401, 404, "对话不存在")
        return item

    def list_conversations(self, user_id):
        items = [item for item in self.conversations.values() if item["user_id"] == user_id]
        return [self.summary(item) for item in sorted(items, key=lambda item: item["updated_at"], reverse=True)[:100]]

    def create_conversation(self, user_id, title=""):
        with self.lock:
            if len(self.list_conversations(user_id)) >= 100:
                raise AppError(40001, 400, "对话数量已达上限，请先删除不用的对话")
            cid = uuid4().hex
            item = dict(id=cid, title=title, updated_at=self.stamp(), user_id=user_id)
            self.conversations[cid] = item
            return self.summary(item)

    def conversation(self, user_id, cid):
        item = self.owned_conversation(user_id, cid)
        return dict(id=cid, title=item["title"], messages=self.messages[cid][-200:])

    def rename_conversation(self, user_id, cid, title):
        item = self.owned_conversation(user_id, cid)
        item.update(title=title, updated_at=self.stamp())
        return self.summary(item)

    def delete_conversation(self, user_id, cid):
        self.owned_conversation(user_id, cid)
        del self.conversations[cid]
        self.messages.pop(cid, None)

    def append_message(self, user_id, cid, role, content, route=None, sources=None):
        if self.save_fails:
            raise RuntimeError("PRIVATE_QUESTION PRIVATE_ANSWER")
        item = self.owned_conversation(user_id, cid)
        self.messages[cid].append(dict(role=role, content=content[:4000], route=route,
                                       sources=sources, created_at=self.stamp()))
        if role == "user" and not item["title"]:
            item["title"] = content[:20]
        item["updated_at"] = self.stamp()


def build_account_app(**overrides):
    values = dict(warmup_on_start=False, chat_log_enabled=False, rate_limit_per_min=1000,
                  rate_limit_per_day=1000, auth_secret="offline-test-secret")
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    clock = [100000.0]
    redis = FakeRedis(lambda: clock[0])
    store = AccountStore(settings, redis=redis, clock=lambda: clock[0])
    store.cache._client = lambda: redis
    repo = MemoryRepo()
    accounts = AccountService(settings, repo=repo, store=store)
    quota = DailyQuota(settings, redis=redis, clock=lambda: clock[0])
    quota.cache._client = lambda: redis
    app = create_app(settings, services=FakeServices(), account_service=accounts, daily_quota=quota)
    app.state.rate_limiter.cache._client = lambda: None
    app.state.rate_limiter.clock = lambda: clock[0]
    return app, repo, redis, clock


def register(api, username="Alice_01", password=PASSWORD):
    return api.post("/api/v1/auth/register", headers=HEADERS, json=dict(username=username, password=password))


def login(api, username="Alice_01", password=PASSWORD):
    return api.post("/api/v1/auth/login", headers=HEADERS, json=dict(username=username, password=password))


def sse_events(response):
    rows = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        if name:
            rows.append((name, json.loads(next(line[6:] for line in lines if line.startswith("data: ")))))
    return rows
