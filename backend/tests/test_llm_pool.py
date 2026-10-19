"""连接池配置与复用只用 MockTransport 验证，不建立 TCP/TLS 连接。"""
import asyncio
import socket
from unittest.mock import Mock

import httpx
import pytest

from app.config import Settings
from app.llm.client import LLMClient


@pytest.mark.parametrize("keepalive,concurrency", [(60, 2), (60, 50), (17.5, 4), (0, 1)])
@pytest.mark.parametrize("socket_options_supported", [True, False])
def test_pool_parameters_reach_client_and_transport_and_client_is_reused(
        monkeypatch, keepalive, concurrency, socket_options_supported):
    transports, calls = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    def legacy_transport(*, limits, trust_env, retries):
        transport = httpx.MockTransport(handler)
        transports.append((transport, limits, trust_env, retries))
        return transport
    options_seen = []
    def modern_transport(*, limits, trust_env, retries, socket_options):
        options_seen.append(socket_options)
        return legacy_transport(limits=limits, trust_env=trust_env, retries=retries)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", modern_transport if socket_options_supported else legacy_transport)
    factory = Mock(wraps=httpx.AsyncClient)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    async def run():
        client = LLMClient(Settings(_env_file=None, llm_api_key="POOL_TEST_KEY", llm_timeout_s=12,
                                    llm_keepalive_s=keepalive, llm_max_concurrency=concurrency))
        http = client._http
        try:
            for _ in range(2):
                assert await client.complete(model="fake", messages=[], temperature=0, max_tokens=8) == "ok"
                assert client._http is http and not http.is_closed
        finally:
            await client.aclose()
        assert http.is_closed
    asyncio.run(run())
    assert factory.call_count == len(transports) == 1 and len(calls) == 2
    transport, limits, trust_env, retries = transports[0]
    assert limits.max_keepalive_connections == limits.max_connections == concurrency
    assert limits.keepalive_expiry == keepalive
    assert trust_env is False and retries == 0  # 重试统一交给请求预算控制。
    assert factory.call_args.kwargs == dict(transport=transport, limits=limits, timeout=12,
                                           follow_redirects=False, trust_env=False)
    assert options_seen == ([[(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]] if socket_options_supported else [])


def test_injected_transport_is_preserved_and_skips_default_transport(monkeypatch):
    factory = Mock(side_effect=AssertionError("不得替换注入的传输"))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    client_factory = Mock(wraps=httpx.AsyncClient)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    async def run():
        client = LLMClient(Settings(_env_file=None), transport=transport)
        await client.aclose()
    asyncio.run(run())
    factory.assert_not_called()
    assert client_factory.call_args.kwargs["transport"] is transport
    limits = client_factory.call_args.kwargs["limits"]
    assert (limits.keepalive_expiry, limits.max_keepalive_connections, limits.max_connections) == (60, 50, 50)
