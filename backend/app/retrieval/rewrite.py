"""低分改写补救的纯函数，在线链路与离线检索评测共用。"""
from app.llm.suspicious import is_suspicious


def needs_rewrite_rerank(question, rewritten, docs, *, enabled, threshold):
    if not docs or not enabled or max(doc.score for doc in docs) >= threshold or is_suspicious(question):
        return False
    # 少量增字也可能大幅提升重排分；仅跳过规范化后完全相同或为空的改写。
    normalize = lambda text: "".join(char for char in text.casefold() if char.isalnum())
    original, revised = normalize(question), normalize(rewritten)
    return bool(original and revised) and original != revised


def merge_reranked(original, revised, top_k):
    # 两个列表都必须是真正的重排结果，不得混入原始 RRF 候选分。
    merged = {doc.parent_id: doc for doc in original}
    for doc in revised:
        if doc.parent_id not in merged or doc.score > merged[doc.parent_id].score:
            merged[doc.parent_id] = doc
    return sorted(merged.values(), key=lambda doc: doc.score, reverse=True)[:top_k]
