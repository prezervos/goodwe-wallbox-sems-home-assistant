"""Exercise preflight and real latest-intent buffering together without device I/O."""

import asyncio
import importlib
from pathlib import Path
import sys
import threading
import types
from unittest.mock import AsyncMock, Mock

import pytest

PACKAGE = "control_fallback_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/sems_wallbox")]
sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".native_control_fallback")
intent = importlib.import_module(PACKAGE + ".native_intent")


def subject(reply=None):
    async def executor(function):
        return function()
    async def locked(operation):
        return await operation()
    owner = types.SimpleNamespace(
        local=False, _closed=False, transitioning=False, cloud_restored_at=None,
        routing_epoch=0, serial="test", last_update_success=True,
        cloud=types.SimpleNamespace(get_data_gen2=Mock(return_value=reply)),
        hass=types.SimpleNamespace(async_add_executor_job=executor,
            async_create_background_task=lambda coro, name: asyncio.create_task(coro)),
        automatic_fallback=types.SimpleNamespace(enabled=True, paused=False, blocked=False,
            next_attempt=0, RETRY_DELAY=300, return_delay=1800, reason=None),
        charge_mode_policy=types.SimpleNamespace(invalidate=Mock(),
            async_setting_write=AsyncMock(side_effect=locked), timeout=1),
        transport=types.SimpleNamespace(available=True),
        connection_intent=types.SimpleNamespace(async_automatic=AsyncMock()),
        async_refresh=AsyncMock(), async_update_listeners=Mock(),
        entry=types.SimpleNamespace(async_start_reauth=Mock()),
    )
    async def handover(local):
        owner.local = local
        owner.routing_epoch += 1
    owner._set_local = AsyncMock(side_effect=handover)
    owner.pending_intent = intent.LatestIntent(owner)
    owner.control_fallback = module.ControlFallback(owner)
    owner.control_fallback._notify = AsyncMock()
    return owner


async def finish(owner):
    await asyncio.wait_for(owner.control_fallback.task, 1)
    await asyncio.wait_for(owner.pending_intent.task, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [True, False])
async def test_route_is_verified_before_single_dispatch(healthy):
    owner = subject({"sn": "test"} if healthy else None)
    operation = AsyncMock()
    assert owner.control_fallback.submit(True, operation)
    operation.assert_not_awaited()
    await finish(owner)
    operation.assert_awaited_once()
    assert owner.local is not healthy
    if healthy:
        owner._set_local.assert_not_awaited()
        owner.control_fallback._notify.assert_not_awaited()
    else:
        owner._set_local.assert_awaited_once_with(True)
        assert owner.control_fallback._notify.await_args_list[-1].args == ("cloud_control_tcp_ready",)


@pytest.mark.asyncio
async def test_newest_stop_supersedes_start_during_preflight():
    owner = subject()
    entered, release = asyncio.Event(), asyncio.Event()
    async def executor(function):
        entered.set()
        await release.wait()
        return None
    owner.hass.async_add_executor_job = executor
    start, stop = AsyncMock(), AsyncMock()
    assert owner.control_fallback.submit(True, start)
    await entered.wait()
    assert not owner.control_fallback.submit(False, stop)
    assert owner.pending_intent.submit("charging", False, stop)
    release.set()
    await finish(owner)
    start.assert_not_awaited()
    stop.assert_awaited_once()
    assert owner._set_local.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["enabled", "paused", "blocked"])
async def test_opt_out_manual_override_and_auth_block_never_take_over(flag):
    owner = subject()
    setattr(owner.automatic_fallback, flag, flag != "enabled")
    assert not owner.control_fallback.submit(True, AsyncMock())
    owner._set_local.assert_not_awaited()
    owner.cloud.get_data_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["auth", "handover", "unverified", "manual", "cooldown"])
async def test_failed_preparation_never_dispatches_charging(failure):
    owner = subject()
    if failure == "auth":
        owner.cloud.get_data_gen2.side_effect = module.CloudAuthenticationError()
    elif failure == "handover":
        owner._set_local.side_effect = ConnectionError("LAN unavailable")
    elif failure == "unverified":
        owner.transport.available = False
    elif failure == "cooldown":
        owner.automatic_fallback.next_attempt = float("inf")
    else:
        def changed(*args):
            owner.automatic_fallback.paused = True
        owner.cloud.get_data_gen2.side_effect = changed
    operation = AsyncMock()
    assert owner.control_fallback.submit(True, operation)
    await finish(owner)
    operation.assert_not_awaited()
    assert owner.control_fallback.error == "cloud_control_failed"
    assert not owner.pending_intent.pending
    if failure == "auth":
        owner.entry.async_start_reauth.assert_called_once()
        owner._set_local.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_discards_pending_command():
    owner = subject()
    entered, release = asyncio.Event(), asyncio.Event()
    async def executor(function):
        entered.set()
        await release.wait()
    owner.hass.async_add_executor_job = executor
    command = AsyncMock()
    owner.control_fallback.submit(True, command)
    await entered.wait()
    closing = asyncio.create_task(owner.control_fallback.close())
    release.set()
    await closing
    assert owner.pending_intent.closed
    assert not owner.pending_intent.pending
    command.assert_not_awaited()
    owner._set_local.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_intent", ["stop", "power"])
async def test_latest_intent_survives_real_policy_lock_wait(new_intent):
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    owner = subject()
    owner.charge_mode_policy = policy_module.ChargeModePolicy(None, None)
    policy = owner.charge_mode_policy
    await policy._lock.acquire()
    start, latest = AsyncMock(), AsyncMock()
    owner.control_fallback.submit(True, start)
    try:
        async with asyncio.timeout(1):
            while not policy._lock._waiters:
                await asyncio.sleep(0)
        if new_intent == "stop":
            owner.pending_intent.submit("charging", False, latest)
        else:
            owner.pending_intent.submit("power", 5.0, latest)
    finally:
        policy._lock.release()
    await finish(owner)
    latest.assert_awaited_once()
    assert start.await_count == (new_intent == "power")
    owner._set_local.assert_awaited_once_with(True)
    assert owner.control_fallback.error is None


@pytest.mark.asyncio
async def test_preflight_deadline_prevents_http_after_shared_lock_wait(monkeypatch):
    budgets = importlib.import_module(PACKAGE + ".operation_budget")
    owner = subject()
    lock, entered = threading.Lock(), threading.Event()
    request = Mock()
    def read(serial):
        entered.set()
        with budgets.serialized_request(lock):
            request(timeout=budgets.request_timeout(30))
    owner.cloud.get_data_gen2 = read
    owner.hass.async_add_executor_job = asyncio.to_thread
    monkeypatch.setattr(module, "_PREFLIGHT_TIMEOUT", 0.1)
    command = AsyncMock()
    lock.acquire()
    try:
        owner.control_fallback.submit(True, command)
        await finish(owner)
        assert entered.is_set()
        request.assert_not_called()
        owner._set_local.assert_awaited_once_with(True)
        command.assert_awaited_once()
    finally:
        lock.release()
    assert budgets.CURRENT_BUDGET.get() is None


@pytest.mark.asyncio
async def test_started_handover_is_never_retried_on_supersession():
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    owner = subject()
    owner.charge_mode_policy = policy_module.ChargeModePolicy(None, None)
    owner._set_local.side_effect = policy_module.RequestSuperseded("Already started")
    command = AsyncMock()
    owner.control_fallback.submit(True, command)
    await finish(owner)
    owner._set_local.assert_awaited_once_with(True)
    command.assert_not_awaited()
    assert owner.control_fallback.error == "cloud_control_failed"


@pytest.mark.asyncio
async def test_expired_intent_cannot_trigger_late_handover():
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    owner = subject()
    owner.charge_mode_policy = policy_module.ChargeModePolicy(None, None)
    policy = owner.charge_mode_policy
    await policy._lock.acquire()
    command = AsyncMock()
    owner.control_fallback.submit(True, command)
    try:
        async with asyncio.timeout(1):
            while not policy._lock._waiters:
                await asyncio.sleep(0)
        owner.pending_intent.batch_deadline = 0
    finally:
        policy._lock.release()
    await finish(owner)
    owner._set_local.assert_not_awaited()
    command.assert_not_awaited()
