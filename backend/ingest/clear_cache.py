"""python -m ingest.clear_cache：仅清理 FAQ 缓存。"""
import argparse
import sys

from app.faq.cache import FaqCache, clear_cache


def main(argv=None, *, settings=None, client=None):
    parser = argparse.ArgumentParser(description="用 SCAN 分批清理 faq:cache:*，保留限流计数")
    parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    cache = FaqCache(client, url=settings.redis_url)
    try:
        redis = cache._client()
        if redis is None:
            raise ConnectionError
        count = clear_cache(redis)
        print(f"FAQ 缓存已删除：{count} 条")
        return 0
    except Exception:
        print("FAQ 缓存清理失败：Redis 不可用或操作失败，请检查服务与连接配置", file=sys.stderr)
        return 1
    finally:
        if client is None and cache.client is not None:
            try:
                cache.client.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
