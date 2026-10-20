"""合成完整暂存知识库；完全离线，源资料只读。"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

from ingest.content_check import FAQ_FIELDS, GROUPS, csv_rows, safe_file
from ingest.load_faq import validate_faq_values
from ingest.loaders import EXTENSIONS, PROJECT_ROOT, load_document
from ingest.products import resolve_path
from ingest.run import analyze_documents
from ingest.product_metadata import ProductSourceIndex, source_skus


def target_path(value, expected):
    path = Path(value)
    if path.is_symlink() or path.resolve() != expected.resolve():
        raise ValueError(f"输出仅允许：{expected}")
    return safe_file(expected.resolve())


def merge_content(out, faq_out, *, dry_run=False, force=False, data_dir=None, with_skus=False):
    data = Path(data_dir) if data_dir is not None else PROJECT_ROOT / "data"
    out = target_path(out, data / "docs_staging")
    faq_out = target_path(faq_out, data / "faq_staging.csv")
    for target in (out, faq_out):
        if target.exists() and not force:
            raise ValueError(f"目标已存在，需 --force：{target}")
    if out.exists() and not out.is_dir() or faq_out.exists() and not faq_out.is_file():
        raise ValueError("目标类型不正确：文档需为目录，FAQ 需为文件")
    if out.exists():
        # --force 也不得触碰环境文件或经链接指向其他内容。
        for existing in out.rglob("*"):
            safe_file(existing)
    selected, messages, overrides = {}, [], []

    def collect(folder, origin):
        if not folder.is_dir():
            messages.append(f"目录不存在，跳过：{folder}")
            return
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                continue
            safe_file(path)
            if any(parent.is_symlink() for parent in path.parents):
                raise ValueError(f"来源目录不能为符号链接：{path}")
            name = path.name
            old = selected.get(name)
            payload = path.read_bytes()
            if old:
                if origin == "generated" and old["origin"] == "online":
                    overrides.append(name)
                elif payload != old["payload"]:
                    raise ValueError(f"同名不同内容冲突：{old['path']} 与 {path}")
            selected[name] = dict(path=path, payload=payload, origin=origin)

    collect(data / "docs", "online")
    collect(data / "docs_generated", "generated")
    for group in GROUPS:
        collect(data / "docs_new" / group, "new")
    # 发现额外组也并入，避免生产者新增组时静默遗漏。
    new_root = data / "docs_new"
    if new_root.is_dir():
        for folder in sorted(new_root.iterdir()):
            if folder.is_dir() and folder.name not in GROUPS:
                collect(folder, "new")
    if not selected:
        raise ValueError("没有可合并的文档")
    documents = [load_document(item["path"], source=name) for name, item in sorted(selected.items())]
    sku_index = ProductSourceIndex([data / "docs", data / "docs_generated", data / "docs_new"]) if with_skus else None
    _, stats = analyze_documents(documents)
    faqs = []
    sources = [data / "faq.csv"]
    if new_root.is_dir():
        sources.extend(folder / "faq.csv" for folder in sorted(new_root.iterdir()) if folder.is_dir())
    for path in sources:
        if not path.exists():
            messages.append(f"FAQ 不存在，跳过：{path}")
            continue
        for index, row in enumerate(csv_rows(path, FAQ_FIELDS), 2):
            validate_faq_values(row, index)
            faqs.append(row)
    stats.update(categories=dict(sorted(Counter(doc.category for doc in documents).items())),
                 faqs=len(faqs), faq_categories=dict(sorted(Counter(row["category"] for row in faqs).items())),
                 overrides=overrides, messages=messages, dry_run=dry_run)
    if not dry_run:
        if out.exists():
            for path in out.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        out.mkdir(parents=True, exist_ok=True)
        for name, item in sorted(selected.items()):
            (out / name).write_bytes(item["payload"])
        if with_skus:
            # 扁平化复制时带上关联，不依赖运行时仍保留原始 docs_new。
            with (out / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("file", "title", "category", "facts", "skus"))
                writer.writeheader()
                for doc in documents:
                    writer.writerow(dict(file=doc.source, title=doc.title, category=doc.category, facts="",
                                         skus=";".join(source_skus(str(selected[doc.source]["path"]), sku_index))))
        with faq_out.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FAQ_FIELDS)
            writer.writeheader()
            writer.writerows(faqs)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--faq-out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--with-skus", action="store_true", help="保留关联 SKU 到暂存目录 manifest.csv")
    args = parser.parse_args(argv)
    try:
        stats = merge_content(resolve_path(args.out), resolve_path(args.faq_out),
                              dry_run=args.dry_run, force=args.force, with_skus=args.with_skus)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"内容合并失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
