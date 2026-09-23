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
    translations = types.ModuleType("homeassistant.helpers.translation")
    async def translated(*args):
        return {"component.sems_wallbox.exceptions." + key + ".message": key
                for key in ("pending_controls_failed", "pending_controls_expired")}
    translations.async_get_translations = translated
    monkeypatch.setitem(sys.modules, translations.__name__, translations)
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
    notices.async_create.assert_called_once()


@pytest.mark.asyncio
async def test_restart_discards_pending_start(subject):
    pending, _ = subject
    async def forbidden():raise AssertionError("Must not start after shutdown")
    pending.submit("charging", True, forbidden)
    await pending.close()
    assert not pending.pending and pending.task.done()
    assert not module.LatestIntent(pending.owner).pending


@pytest.mark.asyncio
async def test_failed_prerequisite_cancels_pending_start(subject):
    pending, notices = subject
    async def failed():raise ConnectionError("Mode not confirmed")
    async def forbidden():raise AssertionError("Must not start after failed preparation")
    pending.submit("mode", 1, failed)
    pending.submit("charging", True, forbidden)
    ready(pending)
    await asyncio.wait_for(pending.task, 1)
    assert pending.error == "operation_failed" and not pending.pending
    notices.async_create.assert_called_once()


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
async def test_new_request_during_error_notification_is_not_stranded(subject, monkeypatch):
    pending, _ = subject
    entered, release = asyncio.Event(), asyncio.Event()
    translation = sys.modules["homeassistant.helpers.translation"]
    original = translation.async_get_translations

    async def translated(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(translation, "async_get_translations", translated)
    calls = []
    async def failed():raise ConnectionError("setting failed")
    async def stop():calls.append("stop")
    pending.submit("mode", 1, failed)
    ready(pending)
    first = pending.task
    await asyncio.wait_for(entered.wait(), 1)
    pending.submit("charging", False, stop)
    release.set()
    await asyncio.wait_for(first, 1)
    await asyncio.wait_for(pending.task, 1)
    assert calls == ["stop"] and not pending.pending


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
