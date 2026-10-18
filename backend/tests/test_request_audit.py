import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from app.budget import deadline
from app.config import Settings
from app.main import create_app
from chat_fakes import FakeServices, document


def test_api_budget_is_created_once_and_includes_rate_limiter():
    async def run():
        services = FakeServices()
        app = create_app(Settings(_env_file=None, request_timeout_s=0.05, warmup_on_start=False), services=services)
        async def blocked(*args):
            await asyncio.Event().wait()
        app.state.rate_limiter.check_async = blocked
        original = asyncio.timeout_at
        with patch("app.budget.asyncio.timeout_at", wraps=original) as timers:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.post("/api/v1/chat", json={"session_id": "budget", "message": "问题"})
        assert result.status_code == 504 and result.json()["code"] == 50401
        assert timers.call_count == 1 and deadline.get() is None
        services.route.assert_not_awaited()
    asyncio.run(run())


def test_api_handoff_timeout_chat_done_has_no_speculative_stages(caplog):
    async def run():
        services = FakeServices().knowledge()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        services.retrieve_candidates = AsyncMock(return_value=[document()])
        async def rerank(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        services.rerank_candidates = rerank
        app = create_app(Settings(_env_file=None, request_timeout_s=0.1, warmup_on_start=False), services=services)
        app.state.rate_limiter.cache._client = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/api/v1/chat", json={"session_id": "handoff", "message": "问题"})
        assert result.status_code == 504 and entered.is_set()
        await asyncio.wait_for(cancelled.wait(), 1)
    asyncio.run(run())
    messages = [record.getMessage() for record in caplog.records if record.name == "app.main"
                and record.getMessage().startswith("chat_done")]
    assert len(messages) == 1
    assert '"retrieve"' not in messages[0] and '"rerank"' not in messages[0]
    assert '"router"' in messages[0] and '"total"' in messages[0]
