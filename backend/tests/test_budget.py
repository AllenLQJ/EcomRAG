import asyncio

import pytest

from app.budget import budget_expired, budget_scope, deadline, raise_if_expired, request_budget


def test_nested_budget_reuses_scope_and_resets_context():
    async def run():
        async with request_budget(40):
            end, scope = deadline.get(), budget_scope.get()
            async with request_budget(120):
                assert deadline.get() == end and budget_scope.get() is scope
                assert not budget_expired()
        assert deadline.get() is None and budget_scope.get() is None
    asyncio.run(run())


def test_expired_inherited_deadline_is_rejected_before_body():
    async def run():
        token = deadline.set(asyncio.get_running_loop().time() - 1)
        try:
            with pytest.raises(TimeoutError):
                async with request_budget(40):
                    pytest.fail('已到期预算不得执行新尝试')
        finally:
            deadline.reset(token)
    asyncio.run(run())


def test_rescheduled_expiry_is_detected_before_timeout_callback_runs():
    async def run():
        async with request_budget(60):
            scope = budget_scope.get()
            scope.reschedule(asyncio.get_running_loop().time() - 1)
            assert budget_expired()
            with pytest.raises(TimeoutError):
                raise_if_expired()
            scope.reschedule(asyncio.get_running_loop().time() + 60)
    asyncio.run(run())


def test_swallowed_budget_cancellation_cannot_turn_into_success():
    async def run():
        with pytest.raises(TimeoutError):
            async with request_budget(60):
                budget_scope.get().reschedule(asyncio.get_running_loop().time() - 1)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
        assert deadline.get() is None and budget_scope.get() is None
        assert asyncio.current_task().cancelling() == 0
    asyncio.run(run())
