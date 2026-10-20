"""RAG 引用及有来源依据的回答提及商品卡片；每请求最多一次商品查询。"""
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
COLUMNS = ("sku_id", "name", "series", "category", "specs", "image_main")
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
NAME_INDEX_TTL_S = 60
# 商品表没有品牌列；只省略已知品牌，不从任意名称截字推测品牌/系列。
BRAND_PREFIXES = ("灵码",)
MODEL_CODE = r"[A-Za-z]+[0-9]+(?![A-Za-z0-9_])"
MODEL_CONTINUATION = re.compile(rf"\s*[,，、和及与]\s*(?P<model>{MODEL_CODE})")


class ProductNameIndex:
    def __init__(self, rows):
        aliases = {}
        model_aliases = {}
        series_names = set()
        for row in rows:
            sku, name = row.get("sku_id"), row.get("name")
            if not parse_skus([sku]) or not safe_text(name, 160):
                continue
            name = " ".join(name.split())
            names = {name}
            for brand in BRAND_PREFIXES:
                if name.startswith(brand) and name[len(brand):].strip():
                    names.add(name[len(brand):].strip())
            for alias in names:
                aliases.setdefault(alias, set()).add(sku)
            series = row.get("series")
            if not safe_text(series, 32):
                continue
            series = " ".join(series.split())
            # 只从名称中紧跟真实系列名的编码提取型号，不从 SKU 拼音猜系列。
            for variant in names:
                model = re.match(re.escape(series) + r"\s*([A-Za-z]+[0-9]+)(?![A-Za-z0-9_])", variant)
                if model:
                    alias = f"{series} {model[1]}"
                    model_aliases.setdefault(alias, set()).add(sku)
                    series_names.add(series)
        self.aliases = aliases
        self.model_aliases = model_aliases
        ordered_series = sorted(series_names, key=lambda value: (-len(value), value))
        series_pattern = "|".join(re.escape(series) for series in ordered_series)
        self.series_model = (re.compile(rf"(?<![A-Za-z0-9_])(?P<series>{series_pattern})\s+(?P<model>{MODEL_CODE})")
                             if series_pattern else None)

    def _shorthand_matches(self, answer, hidden):
        if self.series_model is None:
            return
        for anchor in self.series_model.finditer(answer):
            end = anchor.end()
            while continuation := MODEL_CONTINUATION.match(answer, end):
                # 只沿紧邻的型号列表继承系列；新系列、说明文字、句号等都会终止当前列表。
                # 不存在、隐藏或歧义的型号只跳过该项，不改变后续型号的系列上下文。
                alias = f"{anchor['series']} {continuation['model']}"
                skus = self.model_aliases.get(alias, set()) - hidden
                if skus:
                    yield 2, continuation.start("model"), continuation.end("model"), skus
                end = continuation.end()

    def match(self, answer, *, hidden_skus=()):
        answer = " ".join(answer.split())
        hidden = set(hidden_skus)
        matches = []
        for priority, aliases in enumerate((self.aliases, self.model_aliases)):
            for alias, skus in aliases.items():
                skus = skus - hidden
                if not skus:
                    continue
                # ASCII 型号不能嵌在另一型号里，例如 S1 / S10、XS1 / S1。
                pattern = ((r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", alias) else "")
                           + re.escape(alias)
                           + (r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", alias) else ""))
                for match in re.finditer(pattern, answer):
                    matches.append((priority, match.start(), match.end(), skus))
        matches.extend(self._shorthand_matches(answer, hidden))
        # 全称、显式系列型号、继承型号依次优先；各级取最长不重叠匹配，歧义也占位。
        selected = []
        for _, start, end, skus in sorted(matches, key=lambda item: (item[0], item[1] - item[2], item[1])):
            if not any(start < right and end > left for left, right, _ in selected):
                selected.append((start, end, skus))
        result = []
        for _, _, skus in sorted(selected, key=lambda item: item[0]):
            if len(skus) == 1:
                sku = next(iter(skus))
                if sku not in result:
                    result.append(sku)
        return result


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


def series_name(row):
    value = row.get("series")
    return " ".join(value.split()) if safe_text(value, 32) else ""


class ProductRepo:
    def __init__(self, settings=None, *, connection=None, connection_factory=None):
        self.settings, self.connection, self.connection_factory = settings, connection, connection_factory

    def fetch(self, skus=None):
        """None 刷新名称索引并返回本次新鲜商品行；列表仅查候选 SKU。"""
        if skus is not None and not skus:
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
                sql = "SELECT " + ", ".join(COLUMNS) + " FROM products"
                if skus is not None:
                    sql += " WHERE sku_id IN (" + ", ".join(["%s"] * len(skus)) + ")"
                cursor.execute(sql, tuple(skus) if skus is not None else ())
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
        self.name_lock = threading.Lock()
        self.name_index = None
        self.name_index_expires = 0
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

    def _rows_and_mentions(self, candidates, answer):
        if not answer.strip():
            return self.repo.fetch(candidates), []
        hidden = set(self.settings.product_hidden_skus)
        # 锁只保护小型名称快照及刷新，防止冷缓存并发请求重复全表刷新。
        with self.name_lock:
            if self.name_index is None or monotonic() >= self.name_index_expires:
                rows = self.repo.fetch(None)
                index = ProductNameIndex(rows)
                self.name_index = index
                self.name_index_expires = monotonic() + NAME_INDEX_TTL_S
                return rows, index.match(answer, hidden_skus=hidden)
            index = self.name_index
        mentions = index.match(answer, hidden_skus=hidden)
        skus = list(dict.fromkeys(candidates + mentions))
        rows = self.repo.fetch(skus)
        # 缓存只用于找候选；删除、改名和规格变化仍用本次查询验证。
        fresh_mentions = set(ProductNameIndex(rows).match(answer, hidden_skus=hidden))
        return rows, [sku for sku in mentions if sku in fresh_mentions]

    def for_sources(self, docs, answer=""):
        stage = {"stage": "source_lookup"}
        token = _card_stage.set(stage)
        try:
            docs = list(docs)
            candidates = []
            hidden = set(self.settings.product_hidden_skus)
            for doc in docs:
                skus = parse_skus(doc.skus) if doc.skus is not None else source_skus(doc.source, self._index())
                for sku in skus:
                    if sku not in hidden and sku not in candidates:
                        candidates.append(sku)
            candidates = candidates[:100]
            if not candidates and not answer.strip():
                return []
            stage["stage"] = "query"
            fetched, mentions = self._rows_and_mentions(candidates, answer)
            rows = {row["sku_id"]: row for row in fetched}
            stage["stage"] = "build_card"
            if mentions:
                # 回答中的名称本身不是商品依据；只按本次引用正文或已引用 SKU 的真实系列补卡。
                # 系列取本次查询的新鲜行，不使用名称缓存中的旧系列，也不从型号/品类猜测。
                cited_series = {series_name(rows[sku]) for sku in candidates if sku in rows} - {""}
                index = ProductNameIndex(fetched)
                source_mentions = {sku for doc in docs
                                   for sku in index.match(doc.content, hidden_skus=hidden)}
                mentions = [sku for sku in mentions
                            if sku in source_mentions or series_name(rows.get(sku, {})) in cited_series]
            cards = []
            for sku in dict.fromkeys(candidates + mentions):
                if sku in hidden:
                    continue
                card = self._card(rows[sku]) if sku in rows else None
                if card is not None:
                    cards.append(card)
                if len(cards) == self.settings.product_cards_max:
                    break
            return cards
        except Exception as exc:
            # 卡片为可选增强；不回显数据库异常、凭据、业务文本。
            log_product_cards_unavailable(exc, stage["stage"])
            return []
        finally:
            _card_stage.reset(token)
