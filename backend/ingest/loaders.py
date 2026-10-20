from __future__ import annotations

import hashlib
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}
CATEGORIES = {"after_sales", "logistics", "payment", "product", "promotion", "account", "store"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LoaderError(RuntimeError):
    """文档读取或可选依赖不可用。"""


def filename_category(path: str | Path) -> str:
    """与在线加载共用前缀映射；未指定前缀时保持 product 的历史默认值。"""
    return next((name for name in sorted(CATEGORIES)
                 if Path(path).stem.startswith((name + "_", name + "-"))), "product")


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    text: str
    source: str
    category: str


def resolve_data_path(value: str | Path) -> Path:
    """相对路径先按当前目录查找，再按仓库根目录查找。"""
    path = Path(value)
    return path if path.is_absolute() or path.exists() else PROJECT_ROOT / path


def _optional_module(name: str, description: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise LoaderError(f"{description}；请通过 uv 安装 pyproject.toml 声明的依赖。") from exc


def load_document(
    path: str | Path, *, source: str | None = None, metadata: dict | None = None,
) -> Document:
    """读取文档；PDF、DOCX 仅在实际读取时导入解析器。"""
    path = Path(path)
    metadata = metadata or {}
    suffix = path.suffix.lower()
    if suffix not in EXTENSIONS:
        raise LoaderError(f"不支持的文档扩展名：{suffix}")
    if suffix in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8-sig")
    elif suffix == ".pdf":
        module = _optional_module("pymupdf", "读取 PDF 需要 PyMuPDF（pymupdf）")
        with module.open(str(path)) as pdf:
            text = "\n\n".join(page.get_text() for page in pdf)
    else:
        module = _optional_module("docx", "读取 DOCX 需要 python-docx")
        document = module.Document(str(path))
        parts = [paragraph.text for paragraph in document.paragraphs]
        parts.extend(" | ".join(cell.text for cell in row.cells)
                     for table in document.tables for row in table.rows)
        text = "\n\n".join(parts)
    if not text.strip():
        raise LoaderError(f"文档没有可读取文本：{path.name}（扫描 PDF 需要另行 OCR）")
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text) if suffix == ".md" else None
    title = metadata.get("title") or (heading.group(1) if heading else path.stem)
    category = metadata.get("category") or filename_category(path)
    if category not in CATEGORIES:
        raise LoaderError(f"未知文档分类：{category}")
    source = source or path.name
    doc_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return Document(doc_id, title, text, source, category)


def load_directory(directory: str | Path) -> list[Document]:
    """按相对路径排序读取支持的文件，保证文档标识稳定。"""
    directory = Path(directory)
    if not directory.is_dir():
        raise LoaderError(f"文档目录不存在：{directory}")
    return [load_document(path, source=path.relative_to(directory).as_posix())
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in EXTENSIONS]
