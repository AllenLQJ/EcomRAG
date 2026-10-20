"""RAG 引用的可选商品卡片；MySQL 单次批量查询，不持有连接或商品快照。"""
from __future__ import annotations

import importlib
import json
import logging
import re
import threading
from collections.abc import Mapping
from pathlib import Path

from app.schemas import ProductCard
from ingest.loaders import resolve_data_path
from ingest.product_metadata import ProductSourceIndex, parse_skus, source_skus

logger = logging.getLogger(__name__)
COLUMNS = ("sku_id", "name", "category", "specs", "image_main")
SPEC_KEYS = {
    "无线耳机": ("佩戴方式", "关闭主动降噪单次续航", "单次音乐续航", "降噪能力", "续航说明"),
    "桌面台灯": ("额定功率", "色温档位", "亮度调节"),
    "保温杯": ("标称容量", "内胆材料", "空杯重量"),
    "蓝牙音箱": ("额定功率", "音乐续航", "连接方式"),
    "充电设备": ("设备类型", "输出规格", "接口配置"),
    "数据线与配件": ("配件类型", "连接接口", "传输规格"),
    "键盘鼠标": ("设备类型", "连接方式", "按键配置"),
    "收纳包": ("适用物品", "外部尺寸", "面料"),
    "香薰加湿": ("功能类型", "水箱容量", "额定功率"),
}
COMMERCIAL = re.compile(r"价格|售价|单价|原价|优惠|促销|折扣|库存|现货|余量|售罄|[¥￥$€£]|\d\s*元|price|stock|inventory|USD|RMB|CNY", re.I)


def safe_text(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not COMMERCIAL.search(value)


class ProductRepo:
    def __init__(self, settings=None, *, connection=None, connection_factory=None):
        self.settings, self.connection, self.connection_factory = settings, connection, connection_factory

    def fetch(self, skus):
        if not skus:
            return []
        connection = self.connection
        if connection is None:
            if self.connection_factory is not None:
                connection = self.connection_factory()
            else:
                driver = importlib.import_module("pymysql")
                settings = self.settings
                connection = driver.connect(
                    host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user,
                    password=settings.mysql_password.get_secret_value(), database=settings.mysql_database,
                    charset="utf8mb4", autocommit=True, connect_timeout=1, read_timeout=1, write_timeout=1)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT " + ", ".join(COLUMNS) + " FROM products WHERE sku_id IN ("
                               + ", ".join(["%s"] * len(skus)) + ")", tuple(skus))
                return [dict(row) if isinstance(row, Mapping) else dict(zip(COLUMNS, row))
                        for row in cursor.fetchall()]
        finally:
            if self.connection is None:
                connection.close()


class ProductCards:
    def __init__(self, settings, *, repo=None, source_index=None, images_root=None):
        self.repo = repo if repo is not None else ProductRepo(settings)
        self.settings = settings
        self.index = source_index
        self.index_lock = threading.Lock()
        self.images_root = Path(images_root) if images_root is not None else resolve_data_path("data/images")

    def _index(self):
        with self.index_lock:
            if self.index is None:
                self.index = ProductSourceIndex(resolve_data_path(path) for path in self.settings.product_manifest_paths)
        return self.index

    def _image_url(self, sku, relative):
        # 数据库不能借图片地址注入外站 URL、查询参数或任意路径。
        if not isinstance(relative, str) or relative not in {
                f"{sku}/main.{ext}" for ext in ("webp", "png", "jpg", "jpeg", "svg")}:
            return None
        folder = self.images_root / sku
        if (folder / "main.placeholder").is_file() and (folder / "main.svg").is_file():
            relative = f"{sku}/main.svg"
        base = self.settings.image_base_url.rstrip("/")
        return f"{base}/{relative}" if base else relative

    def _card(self, row):
        sku, name, category = (row.get(key) for key in ("sku_id", "name", "category"))
        if not parse_skus([sku]) or not safe_text(name, 160) or category not in SPEC_KEYS:
            return None
        specs = row.get("specs")
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except ValueError:
                return None
        if not isinstance(specs, dict):
            return None
        highlights = [f"{key}：{specs[key].strip()}" for key in SPEC_KEYS[category]
                      if safe_text(specs.get(key), 200)][:2]
        if not highlights:
            return None
        return ProductCard(sku=sku, name=name, category=category, highlights=highlights,
                           image_url=self._image_url(sku, row.get("image_main")))

    def for_sources(self, docs):
        try:
            candidates = []
            hidden = set(self.settings.product_hidden_skus)
            for doc in docs:
                skus = parse_skus(doc.skus) if doc.skus is not None else source_skus(doc.source, self._index())
                for sku in skus:
                    if sku not in hidden and sku not in candidates:
                        candidates.append(sku)
            candidates = candidates[:100]
            if not candidates:
                return []
            rows = {row["sku_id"]: row for row in self.repo.fetch(candidates)}
            cards = []
            for sku in candidates:
                card = self._card(rows[sku]) if sku in rows else None
                if card is not None:
                    cards.append(card)
                if len(cards) == 3:
                    break
            return cards
        except Exception:
            # 卡片为可选增强；不回显数据库异常、凭据、业务文本。
            logger.warning("product_cards_unavailable")
            return []
