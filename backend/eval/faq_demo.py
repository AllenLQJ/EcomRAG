"""手动真实环境验证：python -m eval.faq_demo '问题'。"""
from __future__ import annotations

import argparse
import json

from app.faq.cache import FaqCache
from app.faq.repo import FaqRepo
from app.faq.service import FaqService
from app.retrieval.embedder import BgeM3Embedder
from ingest.loaders import resolve_data_path


def main(argv=None, *, settings=None) -> int:
    parser = argparse.ArgumentParser(description="显示 FAQ 命中结果、来源与 top_score")
    parser.add_argument("question")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    cache = FaqCache(url=settings.redis_url, ttl_s=settings.faq_cache_ttl_s)
    try:
        service = FaqService(
            FaqRepo(settings=settings), cache,
            BgeM3Embedder(resolve_data_path(settings.bge_m3_path), settings.embed_batch_size),
            threshold=settings.faq_threshold,
        )
        hit = service.faq_lookup(args.question)
        print(json.dumps({"result": hit.model_dump() if hit else None,
                          "source": hit.source if hit else None,
                          "top_score": service.top_score}, ensure_ascii=False, indent=2))
    finally:
        if cache.client is not None:
            cache.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
