"""只递增 FAQ 版本；不访问 MySQL、不公开 HTTP 管理入口。"""
import argparse
import sys

from app.faq.cache import FaqCache
from app.faq.version import VERSION_KEY


def main(argv=None, *, settings=None, client=None):
    argparse.ArgumentParser(description="递增 FAQ 专用版本号，通知各 worker 重建快照").parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    cache = FaqCache(client, url=settings.redis_url)
    try:
        redis = cache._client(warn_missing=False)
        if redis is None:
            raise ConnectionError
        print(f"FAQ 重载通知已发送：version={int(redis.incr(VERSION_KEY))}")
        return 0
    except Exception:
        print("FAQ 重载通知失败：Redis 不可用，请修复后重试", file=sys.stderr)
        return 1
    finally:
        if client is None and cache.client is not None:
            try:
                cache.client.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
