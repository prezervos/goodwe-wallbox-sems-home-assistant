"""Latest-choice buffering never grows into a command replay queue."""

import asyncio
import importlib
from pathlib import Path
import sys
import time
import types
from unittest.mock import Mock

import pytest

PACKAGE = "native_intent_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/sems_wallbox")]
sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".native_intent")


@pytest.fixture
def subject(monkeypatch):
    notifications = types.ModuleType("homeassistant.components.persistent_notification")
    notifications.async_create = Mock()
    monkeypatch.setitem(sys.modules, notifications.__name__, notifications)
    owner = types.SimpleNamespace(
        transitioning=True, local=False, cloud_restored_at=1,
        last_update_success=False, _closed=False,
        charge_mode_policy=types.SimpleNamespace(invalidate=Mock()),
        async_update_listeners=Mock(), entry=types.SimpleNamespace(entry_id="test"),
        hass=types.SimpleNamespace(async_create_background_task=lambda coro, name: asyncio.create_task(coro)),
    )
    return module.LatestIntent(owner), notifications


def ready(pending, *, local=False):
    pending.owner.transitioning = False
    pending.owner.cloud_restored_at = None
    pending.owner.local = local
    pending.owner.last_update_success = True


@pytest.mark.asyncio
async def test_click_burst_keeps_only_latest_value_and_stop_cancels_start(subject):
    pending, _ = subject
    calls = []

    async def start():calls.append("start")
    async def stop():calls.append("stop")
    async def power():calls.append("latest power")
    async def mode():calls.append("latest mode")

    for _ in range(100):
        assert pending.submit("charging", True, start)
        assert pending.submit("charging", False, stop)
        pending.submit("power", 4.2, power)
        pending.submit("mode", 1, mode)
    assert len(pending.pending) == 3
    assert not calls
    ready(pending)
    await asyncio.wait_for(pending.task, 1)
    assert calls == ["stop", "latest power", "latest mode"]
    assert not pending.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("local,expected", [(False,["power","mode","start"]),(True,["mode","power","start"])])
async def test_latest_start_runs_after_transport_specific_prerequisites(subject, local, expected):
    pending, _ = subject
    calls = []
    async def start():calls.append("start")
    async def stop():calls.append("stop")
    async def power():calls.append("power")
    async def mode():calls.append("mode")
    pending.submit("charging", False, stop)
    pending.submit("charging", True, start)
    pending.submit("mode", 1, mode)
    pending.submit("power", 5.0, power)
    ready(pending, local=local)
    await asyncio.wait_for(pending.task, 1)
    assert calls == expected


@pytest.mark.asyncio
async def test_clicks_do_not_extend_batch_expiry(subject):
    pending, notices = subject
    async def forbidden():raise AssertionError("Must not execute expired intent")
    pending.submit("power", 4.2, forbidden)
    deadline = pending.batch_deadline
    pending.submit("power", 5.0, forbidden)
    assert pending.batch_deadline == deadline
    pending.batch_deadline = time.monotonic()-1
    await asyncio.wait_for(pending.task, 1)
    assert pending.error == "expired" and not pending.pending
    notices.async_create.assert_not_called()


@pytest.mark.asyncio
async def test_restart_discards_pending_start(subject):
    pending, _ = subject
    async def forbidden():raise AssertionError("Must not start after shutdown")
    pending.submit("charging", True, forbidden)
    await pending.close()
    assert not pending.pending and pending.task.done()
    assert not module.LatestIntent(pending.owner).pending


@pytest.mark.asyncio
async def test_failed_prerequisite_cancels_pending_start(subject, caplog):
    pending, notices = subject
    async def failed():raise ConnectionError("Mode not confirmed")
    async def forbidden():raise AssertionError("Must not start after failed preparation")
    pending.submit("mode", 1, failed)
    pending.submit("charging", True, forbidden)
    ready(pending)
    await asyncio.wait_for(pending.task, 1)
    assert pending.error == "operation_failed" and not pending.pending
    notices.async_create.assert_not_called()
    assert "Deferred wallbox mode operation failed" in caplog.text
    assert "Mode not confirmed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("new_start", [False, True])
async def test_uncertain_start_is_not_replayed_but_new_stop_is_honored(subject, new_start):
    pending, _ = subject
    calls = []
    async def stop():calls.append("stop")
    async def another_start():calls.append("replayed start")
    async def start():
        calls.append("start")
        pending.submit("charging", new_start, another_start if new_start else stop)
        raise ConnectionError("Connection lost after sending Start")
    pending.submit("charging", True, start)
    ready(pending)
    await asyncio.wait_for(pending.task, 1)
    assert calls == (["start"] if new_start else ["start","stop"])


def test_healthy_connection_does_not_defer_normal_controls(subject):
    pending, _ = subject
    ready(pending)
    assert pending.submit("charging", True, Mock()) is False
    assert pending.task is None


@pytest.mark.asyncio
async def test_external_task_cancellation_cannot_spawn_another_worker(subject):
    pending, _ = subject
    async def forbidden():raise AssertionError("Cancelled intent must not execute")
    pending.submit("charging", True, forbidden)
    await asyncio.sleep(0)
    task = pending.task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not pending.pending and pending.task is task and task.cancelled()


@pytest.mark.asyncio
async def test_duplicate_deferred_start_does_not_cancel_inflight_preparation(subject):
    pending, notices = subject
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def start():
        calls.append("start")
        entered.set()
        await release.wait()

    assert pending.submit("charging", True, start)
    ready(pending)
    await asyncio.wait_for(entered.wait(), 1)
    version = pending.version
    invalidations = pending.owner.charge_mode_policy.invalidate.call_count
    for _ in range(10):
        assert pending.submit("charging", True, start)
    assert pending.version == version
    assert pending.owner.charge_mode_policy.invalidate.call_count == invalidations
    release.set()
    await asyncio.wait_for(pending.task, 1)
    assert calls == ["start"] and not pending.pending and pending.error is None
    notices.async_create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["power", "mode", "stop"])
@pytest.mark.parametrize("translated", [False, True])
async def test_pre_delivery_supersession_preserves_latest_choices(subject, key, translated):
    pending, notices = subject
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    from unittest.mock import AsyncMock
    entered, release = asyncio.Event(), asyncio.Event()
    state = types.SimpleNamespace(mode=0, power=4.2)
    calls = []

    async def read():
        entered.set()
        await release.wait()
        return policy_module.ModeObservation(state.mode, power=state.power)

    async def start():
        calls.append("start")
        return True

    async def stop():
        calls.append("stop")
        return True

    async def mode(value, before):
        state.mode = value
        calls.append("mode")
        return True

    async def power():
        state.power = 5.0
        calls.append("power")
        return True

    adapter = types.SimpleNamespace(read=read, start=start, stop=stop, write_mode=mode)
    policy = policy_module.ChargeModePolicy(adapter, types.SimpleNamespace(async_save=AsyncMock()),
                                           enabled=True, initial_mode=0, timeout=2)
    pending.owner.charge_mode_policy = policy

    async def initial_start():
        try:
            await policy.async_start()
        except policy_module.RequestSuperseded as exc:
            if not translated:
                raise
            error = RuntimeError("Translated service error")
            error.translation_key = "request_superseded"
            raise error from exc

    pending.submit("charging", True, initial_start)
    ready(pending)
    await entered.wait()
    if key == "power":
        pending.submit(key, 5.0, lambda: policy.async_setting_write(power, desired_power=5.0))
    elif key == "mode":
        pending.submit(key, 1, lambda: policy.async_select_mode(1))
    else:
        pending.submit("charging", False, policy.async_stop)
    release.set()
    await asyncio.wait_for(pending.task, 2)
    assert calls == (["power", "start"] if key == "power" else [key])
    assert pending.error is None and not pending.pending
    notices.async_create.assert_not_called()


@pytest.mark.parametrize("key,value", [("power", 5.0), ("mode", 1)])
@pytest.mark.parametrize("new_start", [False, True])
async def test_failed_setting_preserves_only_newer_stop(subject, key, value, new_start):
    pending, notices = subject
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def setting():
        calls.append(key)
        entered.set()
        await release.wait()
        raise ConnectionError("Connection lost after setting write")
    async def control():
        calls.append("start" if new_start else "stop")
    assert pending.submit(key, value, setting)
    ready(pending)
    await entered.wait()
    assert pending.submit("charging", new_start, control)
    release.set()
    await asyncio.wait_for(pending.task, 1)
    assert calls == ([key] if new_start else [key, "stop"])
    assert pending.error == "operation_failed" and not pending.pending
    notices.async_create.assert_not_called()
