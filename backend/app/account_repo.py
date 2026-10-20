"""账号与历史的同步 MySQL 仓库；调用方通过有界线程池运行。"""
import json
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import timezone
from uuid import uuid4

from app.chat_log import connect_mysql
from app.contracts import AppError, DEPENDENCY_UNAVAILABLE, INVALID_ARGUMENT, NOT_FOUND, UNAUTHORIZED, USERNAME_CONFLICT
from app.message_limits import MAX_CONVERSATION_MESSAGES, bounded_message, check_message_size, json_bytes, message_metadata


def iso(value):
    if hasattr(value, "isoformat"):
        return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()
    return value


class AccountRepo:
    def __init__(self, settings=None, *, connection_factory=None):
        self.connection_factory = connection_factory or (lambda: connect_mysql(settings))

    @contextmanager
    def transaction(self):
        connection = None
        try:
            connection = self.connection_factory()
            with connection.cursor() as cursor:
                yield cursor
            connection.commit()
        except AppError:
            if connection is not None:
                connection.rollback()
            raise
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if getattr(exc, "args", ()) and exc.args[0] == 1062:
                raise AppError(USERNAME_CONFLICT, 409, "用户名已被使用") from None
            raise AppError(DEPENDENCY_UNAVAILABLE, 503, "服务暂时不可用，请稍后再试") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def row(cursor, value):
        if value is None or isinstance(value, Mapping):
            return value
        return dict(zip((column[0] for column in cursor.description), value))

    def find_user(self, username):
        with self.transaction() as cur:
            cur.execute("SELECT id, username_display AS username, password_hash, status FROM users "
                        "WHERE username_norm=%s", (username,))
            return self.row(cur, cur.fetchone())

    def get_user(self, user_id):
        with self.transaction() as cur:
            cur.execute("SELECT id, username_display AS username, password_hash, status FROM users WHERE id=%s",
                        (user_id,))
            return self.row(cur, cur.fetchone())

    def create_user(self, username, password_hash):
        with self.transaction() as cur:
            cur.execute("INSERT INTO users (username_norm, username_display, password_hash, last_login_at) "
                        "VALUES (%s,%s,%s,UTC_TIMESTAMP(6))", (username.lower(), username, password_hash))
            return {"id": cur.lastrowid, "username": username, "status": 1}

    def issue_session(self, user_id, issue):
        with self.transaction() as cur:
            # 会话签发与注销共享用户行锁，防止注销撤销后并发登录留下新令牌。
            cur.execute("SELECT id FROM users WHERE id=%s AND status=%s FOR UPDATE", (user_id, 1))
            if cur.fetchone() is None:
                raise AppError(UNAUTHORIZED, 401, "用户名或密码错误")
            issue()
            cur.execute("UPDATE users SET last_login_at=UTC_TIMESTAMP(6) WHERE id=%s", (user_id,))

    def delete_user(self, user_id, before_delete=None):
        with self.transaction() as cur:
            cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
            if cur.fetchone() is None:
                return
            if before_delete is not None:
                before_delete()
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))

    def require_conversation(self, cur, user_id, conversation_id, *, lock=False):
        cur.execute("SELECT id, title, updated_at FROM conversations WHERE id=%s AND user_id=%s" +
                    (" FOR UPDATE" if lock else ""), (conversation_id, user_id))
        item = self.row(cur, cur.fetchone())
        if item is None:
            raise AppError(NOT_FOUND, 404, "对话不存在")
        return item

    def owned_conversation(self, user_id, conversation_id):
        with self.transaction() as cur:
            return self.require_conversation(cur, user_id, conversation_id)

    def summary(self, cur, user_id, conversation_id):
        item = self.require_conversation(cur, user_id, conversation_id)
        cur.execute("SELECT COUNT(*) AS message_count FROM messages WHERE conversation_id=%s", (conversation_id,))
        item = dict(item, message_count=self.row(cur, cur.fetchone())["message_count"])
        item["updated_at"] = iso(item["updated_at"])
        return item

    def list_conversations(self, user_id):
        with self.transaction() as cur:
            cur.execute("SELECT c.id, c.title, c.updated_at, "
                        "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
                        "FROM conversations c WHERE c.user_id=%s ORDER BY c.updated_at DESC, c.id DESC LIMIT 100",
                        (user_id,))
            return [dict(self.row(cur, row), updated_at=iso(self.row(cur, row)["updated_at"]))
                    for row in cur.fetchall()]

    def create_conversation(self, user_id, title=""):
        conversation_id = uuid4().hex
        with self.transaction() as cur:
            # 同一用户的创建串行化，避免并发越过 100 条上限。
            cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
            if cur.fetchone() is None:
                raise AppError(UNAUTHORIZED, 401, "请先登录")
            cur.execute("SELECT COUNT(*) AS total FROM conversations WHERE user_id=%s", (user_id,))
            if self.row(cur, cur.fetchone())["total"] >= 100:
                raise AppError(INVALID_ARGUMENT, 400, "对话数量已达上限，请先删除不用的对话")
            cur.execute("INSERT INTO conversations (id,user_id,title) VALUES (%s,%s,%s)",
                        (conversation_id, user_id, title))
            return self.summary(cur, user_id, conversation_id)

    def conversation(self, user_id, conversation_id):
        with self.transaction() as cur:
            item = self.require_conversation(cur, user_id, conversation_id)
            cur.execute("SELECT role, content, route, sources, created_at FROM messages WHERE conversation_id=%s "
                        "ORDER BY created_at DESC, id DESC LIMIT 200", (conversation_id,))
            messages = []
            for row in reversed(cur.fetchall()):
                message = dict(self.row(cur, row))
                message["created_at"] = iso(message["created_at"])
                if isinstance(message["sources"], (str, bytes)):
                    message["sources"] = json.loads(message["sources"])
                if isinstance(message["sources"], dict):
                    metadata = message["sources"]
                    message["sources"] = metadata.get("sources")
                    if "elapsed_ms" in metadata:
                        message["elapsed_ms"] = metadata["elapsed_ms"]
                messages.append(message)
            return dict(id=item["id"], title=item["title"], messages=messages)

    def rename_conversation(self, user_id, conversation_id, title):
        with self.transaction() as cur:
            self.require_conversation(cur, user_id, conversation_id, lock=True)
            cur.execute("UPDATE conversations SET title=%s, updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND user_id=%s",
                        (title, conversation_id, user_id))
            return self.summary(cur, user_id, conversation_id)

    def delete_conversation(self, user_id, conversation_id):
        with self.transaction() as cur:
            self.require_conversation(cur, user_id, conversation_id, lock=True)
            cur.execute("DELETE FROM conversations WHERE id=%s AND user_id=%s", (conversation_id, user_id))

    def append_message(self, user_id, conversation_id, role, content, route=None, sources=None,
                       elapsed_ms=None, request_id=None):
        content, route, sources = bounded_message(content, route, sources, elapsed_ms, request_id)
        metadata = message_metadata(sources, elapsed_ms, request_id)
        with self.transaction() as cur:
            item = self.require_conversation(cur, user_id, conversation_id, lock=True)
            if role == "assistant" and request_id is not None:
                # 自动保存与浏览器回写可能任意先后到达；同一对话行锁下去重。
                cur.execute("SELECT id, content, route, sources FROM messages WHERE conversation_id=%s AND role='assistant' "
                            "AND JSON_UNQUOTE(JSON_EXTRACT(sources,'$.request_id'))=%s LIMIT 1",
                            (conversation_id, request_id))
                existing = self.row(cur, cur.fetchone())
                if existing is not None:
                    if elapsed_ms is not None:
                        stored = existing["sources"]
                        if isinstance(stored, (str, bytes)):
                            stored = json.loads(stored)
                        stored["elapsed_ms"] = elapsed_ms
                        check_message_size(existing["content"], existing["route"], stored)
                        cur.execute("UPDATE messages SET sources=%s WHERE id=%s AND conversation_id=%s",
                                    (json_bytes(stored).decode("utf-8"), existing["id"], conversation_id))
                    return
            cur.execute("INSERT INTO messages (conversation_id,role,content,route,sources) VALUES (%s,%s,%s,%s,%s)",
                        (conversation_id, role, content, route,
                         json_bytes(metadata).decode("utf-8") if metadata is not None else None))
            title = content[:20] if role == "user" and not item["title"] else item["title"]
            cur.execute("UPDATE conversations SET title=%s, updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND user_id=%s",
                        (title, conversation_id, user_id))
            # 在同一对话行锁与事务内插入并裁剪；按时间/id 保留最新 200 条。
            cur.execute("SELECT id, created_at FROM messages WHERE conversation_id=%s "
                        "ORDER BY created_at DESC, id DESC LIMIT 1 OFFSET %s",
                        (conversation_id, MAX_CONVERSATION_MESSAGES - 1))
            boundary = self.row(cur, cur.fetchone())
            if boundary is not None:
                cur.execute("DELETE FROM messages WHERE conversation_id=%s AND (created_at,id)<(%s,%s)",
                            (conversation_id, boundary["created_at"], boundary["id"]))
