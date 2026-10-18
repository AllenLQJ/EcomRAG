from __future__ import annotations

import re
from dataclasses import dataclass

from ingest.loaders import Document


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    child_text: str
    parent_id: str
    parent_text: str
    doc_id: str
    title: str
    source: str
    category: str


def split_text(text: str, size: int, overlap: int = 0) -> list[str]:
    """按字符预算切分，优先在中文句尾停下，保留精确字符重叠。"""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("切块长度必须为正数，且重叠必须小于切块长度")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            minimum = start + max(overlap + 1, size // 2)
            for separators in ("。！？；\n", "，、,; "):
                cuts = [text.rfind(separator, minimum, end) + 1 for separator in separators]
                cut = max(cuts)
                if cut > minimum:
                    end = cut
                    break
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def split_parents(document: Document, max_chars: int = 1200) -> list[str]:
    """Markdown 按二级标题整节切父块，文首介绍并入首节，深层标题保留。"""
    if max_chars <= 0:
        raise ValueError("父块长度必须为正数")
    if document.source.lower().endswith(".md"):
        sections = []
        lines = []
        fence = None
        seen_section = False
        for line in document.text.splitlines(keepends=True):
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                kind = marker.group(1)[0]
                fence = None if fence == kind else kind if fence is None else fence
            if fence is None and re.match(r"^\s{0,3}##\s+", line):
                if seen_section:
                    sections.append("".join(lines))
                    lines = []
                seen_section = True
            lines.append(line)
        if lines:
            sections.append("".join(lines))
    else:
        sections = re.split(r"\n\s*\n", document.text)
    return [part for section in sections if section.strip()
            for part in split_text(section.strip(), max_chars)]


def split_document(
    document: Document, *, parent_max_chars: int = 1200,
    child_chunk_size: int = 300, child_chunk_overlap: int = 50,
) -> list[Chunk]:
    """生成带标题、父块和元数据的子块；长度预算包含标题前缀。"""
    prefix = document.title + "\n"
    body_size = child_chunk_size - len(prefix)
    if not 0 <= child_chunk_overlap < body_size:
        raise ValueError("子块长度扣除标题后必须大于重叠长度")
    chunks = []
    for parent_idx, parent_text in enumerate(split_parents(document, parent_max_chars)):
        parent_id = f"{document.doc_id}#{parent_idx}"
        for child_idx, body in enumerate(split_text(parent_text, body_size, child_chunk_overlap)):
            chunks.append(Chunk(
                chunk_id=f"{parent_id}#{child_idx}", child_text=prefix + body,
                parent_id=parent_id, parent_text=parent_text,
                doc_id=document.doc_id, title=document.title,
                source=document.source, category=document.category,
            ))
    return chunks
