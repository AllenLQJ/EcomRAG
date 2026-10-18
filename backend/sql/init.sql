-- 由 MySQL 入口在空数据目录首次启动时执行，数据库由 MYSQL_DATABASE 指定。
CREATE TABLE IF NOT EXISTS faq_pairs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    question VARCHAR(500) NOT NULL,
    answer TEXT NOT NULL,
    category VARCHAR(100) NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    request_id VARCHAR(36) NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    route VARCHAR(16) NOT NULL,
    strategy VARCHAR(16) NULL,
    latency_ms INT UNSIGNED NOT NULL,
    sources_json JSON NULL,
    error_code INT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_chat_logs_request_id (request_id),
    INDEX idx_chat_logs_session_created (session_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
