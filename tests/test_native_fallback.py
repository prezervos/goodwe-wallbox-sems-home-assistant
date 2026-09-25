"""Automatic fallback timing and ownership-race regression tests."""

import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, Mock, patch

import pytest

PACKAGE = "fallback_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/sems_wallbox")]
sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".native_fallback")


def subject():
    owner = types.SimpleNamespace(
        _closed=False, transitioning=False, local=False, serial="test",
        last_update_success=True, data={"test": {"raw_state": 0, "power": 0,
                                                 "currents_a": [0, 0, 0]}},
        transport=types.SimpleNamespace(available=True,
                        session_guard=types.SimpleNamespace(phase="idle")),
        connection_intent=types.SimpleNamespace(async_automatic=AsyncMock()),
        async_refresh=AsyncMock(), routing_epoch=0,
        async_cloud_preflight=AsyncMock(return_value=True),
        async_update_listeners=Mock(),
        entry=types.SimpleNamespace(async_start_reauth=Mock()), hass=object(),
    )

    async def handover(local):
        owner.local = local

    async def locked(write):
        return await write()

    owner._set_local = AsyncMock(side_effect=handover)
    owner.charge_mode_policy = types.SimpleNamespace(
        async_setting_write=AsyncMock(side_effect=locked))
    return module.AutomaticFallback(owner, enabled=True)


def fail(policy, since=0):
    policy.failures = 3
    policy.failed_since = since
    policy.reason = "cloud_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("count,now,expected", [(1,200,False),(2,200,False),(3,89,False),(3,90,True)])
async def test_debounce_requires_both_count_and_duration(count, now, expected):
    policy = subject()
    fail(policy)
    policy.failures = count
    with patch.object(module.time, "monotonic", return_value=now):
        await policy.tick()
    assert policy.owner.local is expected
    if expected:
        assert policy.next_attempt == now + 1800
        policy.owner._set_local.assert_awaited_once_with(True)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["paused", "closed", "blocked"])
async def test_manual_shutdown_and_authentication_prevent_takeover(flag):
    policy = subject()
    fail(policy)
    setattr(policy, flag, True)
    with patch.object(module.time, "monotonic", return_value=100):
        await policy.tick()
    policy.owner._set_local.assert_not_awaited()


def test_one_good_report_resets_failure_window():
    policy = subject()
    fail(policy)
    policy.observation(True)
    assert policy.failures == 0 and policy.failed_since is None


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [{}, {"raw_state":2,"power":4.2,"currents_a":[6,6,6]}])
async def test_cloud_return_needs_confirmed_idle(values):
    policy = subject()
    policy.owner.local = True
    policy.owner.data["test"] = values
    await policy.tick()
    policy.owner._set_local.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_trial_returns_to_tcp_and_doubles_backoff():
    policy = subject()
    policy.owner.local = True
    with patch.object(module.time, "monotonic", return_value=2000):
        await policy.tick()
    assert not policy.owner.local and policy.trial_deadline == 2060
    with patch.object(module.time, "monotonic", return_value=2059):
        await policy.tick()
    assert not policy.owner.local
    with patch.object(module.time, "monotonic", return_value=2060):
        await policy.tick()
    assert policy.owner.local
    assert policy.return_delay == 3600 and policy.next_attempt == 5660
    assert policy.trial_deadline is None


@pytest.mark.asyncio
async def test_success_during_handover_refresh_finishes_trial():
    policy = subject()
    policy.owner.local = True
    policy.owner.async_refresh.side_effect = lambda: policy.observation(True)
    await policy.tick()
    assert not policy.owner.local and policy.trial_deadline is None
    assert policy.return_delay == 1800


@pytest.mark.asyncio
async def test_manual_override_while_waiting_for_write_lock():
    policy = subject()
    fail(policy)

    async def locked(write):
        policy.pause()
        return await write()

    policy.owner.charge_mode_policy.async_setting_write.side_effect = locked
    with patch.object(module.time, "monotonic", return_value=100):
        with pytest.raises(RuntimeError, match="superseded"):
            await policy.tick()
    policy.owner._set_local.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_handover_does_not_loop_immediately():
    policy = subject()
    fail(policy)
    policy.owner._set_local.side_effect = ConnectionError("LAN down")
    with patch.object(module.time, "monotonic", return_value=100):
        with pytest.raises(ConnectionError):
            await policy.tick()
        await policy.tick()
    assert policy.owner._set_local.await_count == 1
    assert policy.next_attempt == 400


@pytest.mark.parametrize("value", [None,"garbage","2030-01-01T00:00:00+00:00"])
def test_missing_invalid_future_timestamp_rejected(value):
    with pytest.raises(ValueError):
        module.report_age({"lastUpdate":value}, "UTC", 1704067200)


def test_report_age_uses_ha_timezone():
    assert module.report_age({"lastUpdate":"2024-01-01 01:00:00"}, "Europe/Prague", 1704067260) == 60


@pytest.mark.asyncio
async def test_recovered_cloud_while_waiting_for_lock_cancels_takeover():
    policy = subject()
    fail(policy)

    async def locked(write):
        policy.observation(True)
        return await write()

    policy.owner.charge_mode_policy.async_setting_write.side_effect = locked
    with patch.object(module.time, "monotonic", return_value=100):
        await policy.tick()
    policy.owner._set_local.assert_not_awaited()
    assert policy.next_attempt == 0


@pytest.mark.asyncio
async def test_charging_begins_while_return_waits_for_lock():
    policy = subject()
    policy.owner.local = True

    async def locked(write):
        policy.owner.data["test"] = {"raw_state":2, "power":4.2, "currents_a":[6,6,6]}
        return await write()

    policy.owner.charge_mode_policy.async_setting_write.side_effect = locked
    with pytest.raises(RuntimeError, match="Charging changed"):
        await policy.tick()
    policy.owner._set_local.assert_not_awaited()
    assert policy.trial_deadline is None


@pytest.mark.asyncio
async def test_local_failure_recovers_cloud_despite_healthy_dwell_timer():
    policy = subject()
    policy.owner.local = True
    policy.owner.last_update_success = False
    policy.owner.transport.available = False
    policy.local_failures = 3
    policy.local_failed_since = 0
    policy.next_attempt = 1800
    with patch.object(module.time, "monotonic", return_value=15):
        await policy.tick()
    assert not policy.owner.local and policy.trial_deadline == 75


@pytest.mark.asyncio
async def test_failed_local_recovery_is_rate_limited():
    policy = subject()
    policy.owner.local = True
    policy.local_failures = 3
    policy.local_failed_since = 0
    policy.owner._set_local.side_effect = ConnectionError("restore failed")
    with patch.object(module.time, "monotonic", return_value=15):
        with pytest.raises(ConnectionError):
            await policy.tick()
        await policy.tick()
    assert policy.owner._set_local.await_count == 1


@pytest.mark.asyncio
async def test_background_supervisor_is_cancelled_and_awaited_on_unload():
    import asyncio

    policy = subject()
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def tick():
        entered.set()
        await blocked.wait()

    policy.tick = tick
    policy.owner.cloud = object()
    policy.owner.hass = types.SimpleNamespace(
        async_create_background_task=lambda coroutine, name: asyncio.create_task(coroutine)
    )
    with patch.object(module.asyncio, "sleep", AsyncMock()):
        policy.start()
        task = policy.task
        await asyncio.wait_for(entered.wait(), 1)
        await policy.close()
    assert task.done() and task.cancelled()
    assert policy.closed and policy.task is None


def test_no_background_supervisor_without_explicit_opt_in_or_cloud_account():
    from unittest.mock import Mock

    policy = subject()
    policy.owner.hass = types.SimpleNamespace(async_create_background_task=Mock())
    policy.owner.cloud = object()
    policy.enabled = False
    policy.start()
    policy.enabled = True
    policy.owner.cloud = None
    policy.start()
    policy.owner.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_slow_cloud_refresh_cannot_extend_trial_budget():
    import asyncio
    from types import SimpleNamespace

    policy = subject()
    clock = SimpleNamespace(now=1000.0)
    policy.trial_deadline = clock.now + 0.02
    cancelled = asyncio.Event()

    async def refresh():
        if policy.owner.local:
            return
        try:
            await asyncio.Event().wait()
        finally:
            # Windows event-loop timers may fire slightly before monotonic's
            # deadline. Control only policy time; retain real async cancellation.
            clock.now = policy.trial_deadline
            cancelled.set()

    policy.owner.async_refresh.side_effect = refresh
    with patch.object(module, "time", SimpleNamespace(monotonic=lambda: clock.now)):
        await asyncio.wait_for(policy.tick(), 1)
    assert cancelled.is_set()
    assert policy.owner.local and policy.trial_deadline is None
    policy.owner._set_local.assert_awaited_once_with(True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, TimeoutError(), ConnectionError(), ValueError()])
async def test_failed_preflight_keeps_tcp_and_backs_off(error):
    policy = subject()
    policy.owner.local = True
    policy.owner.async_cloud_preflight.return_value = False
    policy.owner.async_cloud_preflight.side_effect = error
    with patch.object(module.time, "monotonic", return_value=2000):
        await policy.tick()
        await policy.tick()
    assert policy.owner.local and policy.trial_deadline is None
    assert policy.next_attempt == 2300
    assert policy.reason == "cloud_preflight_unavailable"
    policy.owner.async_cloud_preflight.assert_awaited_once()
    policy.owner._set_local.assert_not_awaited()
    policy.owner.charge_mode_policy.async_setting_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_authentication_failure_requests_reauth_without_handover():
    policy = subject()
    policy.owner.local = True
    policy.owner.async_cloud_preflight.side_effect = module.CloudAuthenticationError()
    await policy.tick()
    await policy.tick()
    assert policy.blocked and policy.reason == "authentication_failed"
    policy.owner.entry.async_start_reauth.assert_called_once_with(policy.owner.hass)
    policy.owner._set_local.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manual", "epoch", "shutdown", "charging"])
async def test_control_change_during_preflight_prevents_stale_handover(change):
    policy = subject()
    policy.owner.local = True
    async def probe():
        if change == "manual":
            policy.pause()
        elif change == "epoch":
            policy.owner.routing_epoch += 1
        elif change == "shutdown":
            policy.owner._closed = True
        else:
            policy.owner.data["test"] = {"raw_state": 2, "power": 4.2, "currents_a": [6, 6, 6]}
        return True
    policy.owner.async_cloud_preflight.side_effect = probe
    if change == "charging":
        with pytest.raises(RuntimeError, match="Charging changed"):
            await policy.tick()
    else:
        await policy.tick()
    policy.owner._set_local.assert_not_awaited()
    assert policy.trial_deadline is None


@pytest.mark.asyncio
async def test_failed_local_connection_recovery_bypasses_preflight():
    policy = subject()
    policy.owner.local = True
    policy.local_failures = 3
    policy.local_failed_since = 0
    with patch.object(module.time, "monotonic", return_value=15):
        await policy.tick()
    policy.owner.async_cloud_preflight.assert_not_awaited()
    policy.owner._set_local.assert_awaited_once_with(False)
