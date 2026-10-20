-- MySQL 8.0；独立商品表，不修改线上 FAQ、对话表或接口。
CREATE TABLE IF NOT EXISTS products (
    sku_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
    name VARCHAR(160) NOT NULL,
    series VARCHAR(32) NOT NULL,
    category VARCHAR(32) NOT NULL,
    tagline VARCHAR(30) NOT NULL,
    positioning TEXT NOT NULL,
    specs JSON NOT NULL,
    package_contents TEXT NOT NULL,
    not_included TEXT NOT NULL,
    usage_notes MEDIUMTEXT NOT NULL,
    caveats MEDIUMTEXT NOT NULL,
    related_skus TEXT NOT NULL,
    image_main VARCHAR(255) NOT NULL,
    image_alt TEXT NOT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'simulated'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
