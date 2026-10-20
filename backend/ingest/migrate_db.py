"""python -m ingest.migrate_db：chat_logs 补列及账号/历史表幂等创建。"""
import argparse
import sys

from app.chat_log import connect_mysql, ensure_chat_logs_schema


ACCOUNT_TABLES = (
    """CREATE TABLE IF NOT EXISTS users (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        username_norm VARCHAR(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
        username_display VARCHAR(20) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
        last_login_at DATETIME(6) NULL,
        status TINYINT NOT NULL DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS conversations (
        id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        title VARCHAR(100) NOT NULL DEFAULT '',
        created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
        updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
        INDEX idx_conversations_user_updated (user_id, updated_at),
        CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS messages (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        conversation_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        role ENUM('user','assistant') NOT NULL,
        content TEXT NOT NULL,
        route VARCHAR(16) NULL,
        sources JSON NULL,
        created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
        INDEX idx_messages_conversation_created (conversation_id, created_at, id),
        CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


def ensure_account_schema(connection):
    with connection.cursor() as cursor:
        for sql in ACCOUNT_TABLES:
            cursor.execute(sql)
    connection.commit()


def main(argv=None, *, settings=None, connection_factory=None):
    argparse.ArgumentParser(description="chat_logs 幂等补列并创建账号与历史表").parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    try:
        connection = (connection_factory or (lambda: connect_mysql(settings)))()
        try:
            ensure_chat_logs_schema(connection)
            ensure_account_schema(connection)
        finally:
            connection.close()
        print("chat_logs 补列及账号历史表创建完成")
        return 0
    except Exception:
        print("数据库迁移失败，请检查 MySQL 服务、chat_logs 表与 ALTER 权限", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
