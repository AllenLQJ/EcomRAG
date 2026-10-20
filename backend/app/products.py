"""RAG 引用的可选商品卡片；MySQL 单次批量查询，不持有连接或商品快照。"""
from __future__ import annotations

import importlib
import json
import logging
import re
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from time import monotonic

from app.schemas import ProductCard
from ingest.loaders import resolve_data_path
from ingest.product_metadata import ProductSourceIndex, parse_skus, source_skus

logger = logging.getLogger(__name__)
# 每次调用独立跟踪阶段，避免并发查询在共享 repo 上互相覆盖。
_card_stage = ContextVar("product_cards_stage", default=None)
_warning_lock = threading.Lock()
_warning_times = {}
_WARNING_INTERVAL_S = 60
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


def log_product_cards_unavailable(exc, stage, *, log=logger):
    # 不传异常对象、消息或 traceback；当前纯文本 formatter 也能显示全部安全字段。
    exception_type = re.sub(r"[^a-zA-Z0-9_]", "_", type(exc).__name__)[:100]
    mysql_error = any(cls.__module__.split(".")[0] in {"pymysql", "MySQLdb"}
                      for cls in type(exc).__mro__)
    mysql_errno = exc.args[0] if mysql_error and exc.args and type(exc.args[0]) is int else None
    is_timeout = False
    cause = exc
    # PyMySQL 会把 socket.timeout 包装为 OperationalError；只检查异常链类型。
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        is_timeout = is_timeout or isinstance(cause, TimeoutError)
        cause = cause.__cause__ if cause.__cause__ is not None else cause.__context__
    fields = dict(exception_type=exception_type, mysql_errno=mysql_errno,
                  is_timeout=is_timeout, stage=stage)
    key = (exception_type, mysql_errno, is_timeout, stage)
    with _warning_lock:
        now = monotonic()
        # 只保留当前窗口内的错误类别，消息与用户输入不参与限频键。
        for old_key, last in list(_warning_times.items()):
            if now - last >= _WARNING_INTERVAL_S:
                del _warning_times[old_key]
        if key in _warning_times:
            return
        _warning_times[key] = now
    log.warning("product_cards_unavailable exception_type=%s mysql_errno=%s is_timeout=%s stage=%s",
                exception_type, mysql_errno, is_timeout, stage, extra=fields)


def safe_text(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not COMMERCIAL.search(value)


class ProductRepo:
    def __init__(self, settings=None, *, connection=None, connection_factory=None):
        self.settings, self.connection, self.connection_factory = settings, connection, connection_factory

    def fetch(self, skus):
        if not skus:
            return []
        connection = self.connection
        stage = _card_stage.get()
        if connection is None:
            if stage is not None:
                stage["stage"] = "connect"
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
            if stage is not None:
                stage["stage"] = "query"
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
        stage = {"stage": "source_lookup"}
        token = _card_stage.set(stage)
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
            stage["stage"] = "query"
            rows = {row["sku_id"]: row for row in self.repo.fetch(candidates)}
            stage["stage"] = "build_card"
            cards = []
            for sku in candidates:
                card = self._card(rows[sku]) if sku in rows else None
                if card is not None:
                    cards.append(card)
                if len(cards) == 3:
                    break
            return cards
        except Exception as exc:
            # 卡片为可选增强；不回显数据库异常、凭据、业务文本。
            log_product_cards_unavailable(exc, stage["stage"])
            return []
        finally:
            _card_stage.reset(token)
