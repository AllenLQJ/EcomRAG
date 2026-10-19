"""手动诊断：python -m eval.rerank_ping；测试只注入 MockTransport。"""
import argparse
import asyncio
import json
from time import perf_counter

from app.contracts import AppError
from app.llm.diagnostics import redact, target_host
from app.retrieval.remote_reranker import RemoteReranker


async def ping(settings, *, transport=None):
    client = RemoteReranker(settings, transport=transport)
    started = perf_counter()
    result = {"host": target_host(client.base_url, client.key),
              "model": redact(settings.rerank_remote_model, (client.key,))}
    try:
        batches = await client.rerank_with_metadata(
            "怎么申请退货", ["申请退货：请在订单页面选择申请售后并提交退货申请。", "台灯支持三档亮度。", "本店营业时间为九点至十八点。"])
        batch = batches[0]
        indices = [index for index in batch.indices if index is not None]
        scores = batch.response_scores
        result.update(success=True, returned_count=len(scores),
                      index_min=min(indices) if indices else None, index_max=max(indices) if indices else None,
                      index_base=batch.index_base, scores=scores, score_min=min(scores), score_max=max(scores),
                      descending=all(a >= b for a, b in zip(scores, scores[1:])), usage=batch.usage)
    except AppError as exc:
        result.update(success=False, error_code=exc.code, diagnostic=exc.diagnostic)
    finally:
        await client.aclose()
    result["ms"] = round((perf_counter() - started) * 1000, 3)
    print(json.dumps(result, ensure_ascii=False))
    return result


def main(argv=None, *, settings=None, transport=None):
    argparse.ArgumentParser(description="最小远程重排请求；仅打印脱敏诊断与数字元数据").parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    return 0 if asyncio.run(ping(settings, transport=transport))["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
