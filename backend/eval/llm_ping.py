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
        for role, model, max_tokens in (
            ("router", settings.router_model, settings.router_max_tokens),
            ("generate", settings.generate_model, settings.generate_max_tokens),
        ):
            started = perf_counter()
            try:
                completion = await client.complete_with_metadata(
                    model=model, messages=[{"role": "user", "content": "回复ok"}],
                    temperature=0, max_tokens=max_tokens)
                result = {"role": role, "model": safe[f"{role}_model"], "success": True,
                          "ms": round((perf_counter() - started) * 1000, 3),
                          "reply_length": len(completion.text),
                          "finish_reason": completion.finish_reason,
                          "reasoning_tokens": completion.reasoning_tokens}
            except AppError as exc:
                structure = exc.diagnostic.get("response_structure", {})
                result = {"role": role, "model": safe[f"{role}_model"], "success": False,
                          "ms": round((perf_counter() - started) * 1000, 3), "error_code": exc.code,
                          "finish_reason": structure.get("finish_reason"),
                          "reasoning_tokens": structure.get("usage", {}).get("reasoning_tokens"),
                          "diagnostic": exc.diagnostic}
            result["max_tokens"] = max_tokens
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
