"""入库冒烟与离线评测共用的来源标注校验，不加载配置、模型或 SDK。"""


def expected_documents(value, *, required=False):
    """返回可接受文档；null/缺省表示无来源参考，空列表和非法类型必须报错。"""
    if value is None and not required:
        return ()
    documents = [value] if isinstance(value, str) else value
    if (not isinstance(documents, list) or not documents
            or any(not isinstance(item, str) or not item.strip() for item in documents)):
        raise ValueError("expected_doc 必须是非空字符串或非空字符串列表")
    return tuple(documents)
