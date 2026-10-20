"""入库冒烟与离线评测共用的来源标注校验，不加载配置、模型或 SDK。"""
from pathlib import Path


def expected_documents(value, *, required=False):
    """去空白、剔除空项并按原顺序去重；null/缺省表示无来源参考。"""
    if value is None and not required:
        return ()
    documents = [value] if isinstance(value, str) else value
    if (not isinstance(documents, list)
            or any(not isinstance(item, str) for item in documents)):
        raise ValueError("expected_doc 必须是非空字符串或非空字符串列表")
    documents = tuple(dict.fromkeys(item.strip() for item in documents if item.strip()))
    if not documents:
        raise ValueError("expected_doc 必须是非空字符串或非空字符串列表")
    return documents


def matches_expected_document(doc, expected):
    """来源全路径、来源文件名或 doc_id 任一相等即命中。"""
    return (doc.source in expected
            or any(Path(doc.source).name == Path(item).name for item in expected)
            or getattr(doc, "doc_id", None) in expected)
