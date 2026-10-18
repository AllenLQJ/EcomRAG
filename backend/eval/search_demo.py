"""手动真实环境验证：python -m eval.search_demo '问题'。"""
from __future__ import annotations

import argparse

from app.retrieval.embedder import BgeM3Embedder
from app.retrieval.reranker import BgeReranker
from app.retrieval.retriever import Retriever
from ingest.loaders import resolve_data_path


def main(argv=None, *, settings=None) -> int:
    parser = argparse.ArgumentParser(description="比较关闭/开启 rerank 的检索结果")
    parser.add_argument("question")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    embedder = BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size)
    reranker = BgeReranker(resolve_data_path(settings.reranker_path),
                           max_length=settings.rerank_max_length, batch_size=settings.rerank_batch_size,
                           fp16=settings.rerank_fp16)
    retriever = Retriever(None, embedder, reranker, uri=settings.milvus_uri,
                          timeout_s=settings.milvus_timeout_s,
                          rerank_candidates=settings.rerank_candidates, rerank_max_chars=settings.rerank_max_chars,
                          rerank_text_mode=settings.rerank_text_mode)
    try:
        for enabled in (False, True):
            retriever.rerank_enabled = enabled
            print(f"rerank={'on' if enabled else 'off'}")
            for index, doc in enumerate(retriever.retrieve([args.question], args.question, top_k=5), 1):
                print(f"{index}. {doc.title} score={doc.score:.6f} snippet={doc.snippet[:60]}")
    finally:
        if retriever.client is not None:
            retriever.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
