"""商品来源映射；只读取显式目录中的 manifest.csv，不读取文档正文。"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path, PurePosixPath

SKU_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
logger = logging.getLogger(__name__)


def parse_skus(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[;,，；|\s]+", value.strip())
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(item for item in value
                             if isinstance(item, str) and len(item) <= 64 and SKU_PATTERN.fullmatch(item)))


def source_key(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix().removeprefix("./")


class ProductSourceIndex:
    def __init__(self, paths=()):
        entries = {}
        for location in paths:
            location = Path(location)
            manifests = sorted(location.rglob("manifest.csv")) if location.is_dir() else [location]
            for path in manifests:
                if not path.is_file() or path.name != "manifest.csv":
                    continue
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        name = source_key(row.get("file") or "")
                        if not name or name == ".":
                            continue
                        skus = tuple(parse_skus(row.get("skus")))
                        # basename 兼容 content_merge 的扁平目录；冲突映射不猜测。
                        for key in {name, PurePosixPath(name).name,
                                    source_key(str(path.parent / name))}:
                            entries.setdefault(key, set()).add(skus)
        self.entries = {key: next(iter(values)) for key, values in entries.items() if len(values) == 1}
        self.ambiguous = {key for key, values in entries.items() if len(values) > 1}
        if self.ambiguous:
            # 只报告冲突文件名，避免把目录或清单中的其他内容写进日志。
            names = sorted({PurePosixPath(key).name for key in self.ambiguous})
            logger.warning("product_manifest_ambiguous files=%r；同名 SKU 映射冲突，停止回退；"
                           "生产请只配置当前已发布库对应的一份清单", names)

    def lookup(self, source: str):
        key = source_key(source)
        if key in self.ambiguous:
            return ()
        if key in self.entries:
            return self.entries[key]
        basename = PurePosixPath(key).name
        if basename in self.ambiguous:
            return ()
        return self.entries.get(basename)


def source_skus(source: str, index: ProductSourceIndex) -> list[str]:
    mapped = index.lookup(source)
    if mapped is not None:
        return list(mapped)
    # 只识别单品文件名，不扫描正文、标题或模型生成的答案。
    filename = PurePosixPath(source_key(source)).name
    match = re.fullmatch(r"product_([a-z0-9]+(?:[-_][a-z0-9]+)*)\.md", filename)
    if not match:
        return []
    # 四个历史文件使用下划线；不将 product_guide_* 等误当 SKU。
    value = match[1]
    legacy = {"tinglan_s1", "tinglan_s2", "qiguang_l1", "muche_c1"}
    return parse_skus(value.replace("_", "-") if value in legacy else value if "-" in value else "")
