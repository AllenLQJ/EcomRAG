"""手动真实环境运行：python -m eval.chat_demo '问题'。"""
import argparse
import asyncio

from app.pipeline import run_chat
from app.schemas import ChatRequest
from app.services import Services


async def demo(question, settings, services):
    if settings.warmup_on_start:
        await services.warmup()
    return await run_chat(ChatRequest(session_id="chat-demo", message=question, debug=True),
                          settings=settings, services=services)


def main(argv=None, *, settings=None):
    parser = argparse.ArgumentParser(description="调用真实聊天链路，打印完整 JSON")
    parser.add_argument("question")
    args = parser.parse_args(argv)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()

    async def run():
        services = Services(settings)
        try:
            result = await demo(args.question, settings, services)
            print(result.model_dump_json(indent=2))
        finally:
            await services.aclose()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
