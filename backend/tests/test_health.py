import asyncio

import pytest


@pytest.mark.parametrize("mode,expected", [
    ("success", "up"),
    ("connection_error", "down"),
    ("connection_timeout", "down"),
    ("close_error", "up"),
    ("close_timeout", "up"),
])
def test_tcp_status_connection_and_cleanup(app_factory, monkeypatch, mode, expected):
    from app import main

    class Writer:
        closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            if mode == "close_error":
                raise OSError("模拟关闭连接失败")
            if mode == "close_timeout":
                await asyncio.Event().wait()

    writer = Writer()

    async def connect(*args, **kwargs):
        if mode == "connection_error":
            raise ConnectionRefusedError("模拟连接失败")
        if mode == "connection_timeout":
            await asyncio.Event().wait()
        return None, writer

    monkeypatch.setattr(main.asyncio, "open_connection", connect)
    assert asyncio.run(main.tcp_status("127.0.0.1", 3306, 0.01)) == expected
    assert writer.closed == (expected == "up")
