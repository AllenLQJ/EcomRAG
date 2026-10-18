"""手动诊断：python -m eval.llm_ping；离线测试须注入 MockTransport。"""
import argparse
import asyncio
import json
from time import perf_counter

from app.contracts import AppError
from app.llm.client import LLMClient
from app.llm.diagnostics import configuration


async def ping(settings, *, transport=None):
    safe = configuration(settings)
    print("配置：" + json.dumps(safe, ensure_ascii=False))
    client = LLMClient(settings, transport=transport)
    results = []
    try:
        for role, model in (("router", settings.router_model), ("generate", settings.generate_model)):
            started = perf_counter()
            try:
                answer = await client.complete(model=model, messages=[{"role": "user", "content": "回复ok"}],
                                               temperature=0, max_tokens=64)
                result = {"role": role, "model": safe[f"{role}_model"], "success": True,
                          "ms": round((perf_counter() - started) * 1000, 3),
                          "reply_length": len(answer)}
            except AppError as exc:
                result = {"role": role, "model": safe[f"{role}_model"], "success": False,
                          "ms": round((perf_counter() - started) * 1000, 3), "error_code": exc.code,
                          "diagnostic": exc.diagnostic}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
    finally:
        await client.aclose()
    return results


def main(argv=None, *, settings=None, transport=None):
    argparse.ArgumentParser(description="最小请求诊断两个 LLM 服务 模型，绝不打印 key").parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    results = asyncio.run(ping(settings, transport=transport))
    return 0 if all(result["success"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
