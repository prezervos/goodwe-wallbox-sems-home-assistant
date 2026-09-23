"""Transport preference survives reload independently of transient fallback."""

import importlib
import types
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_native_handover_guard import PACKAGE, coordinator

module = importlib.import_module(PACKAGE + ".native_connection_intent")


def store(data=None):
    return types.SimpleNamespace(async_load=AsyncMock(return_value=data), async_save=AsyncMock())


@pytest.mark.asyncio
async def test_manual_tcp_roundtrip_and_manual_cloud_clear_fallback():
    persistence = store()
    intent = module.ConnectionIntent(persistence)
    await intent.async_load()
    assert not intent.manual_tcp and not intent.automatic_tcp
    await intent.async_manual(True)
    persistence.async_load.return_value = persistence.async_save.call_args.args[0]
    reloaded = module.ConnectionIntent(persistence)
    await reloaded.async_load()
    assert reloaded.manual_tcp and not reloaded.automatic_tcp
    await reloaded.async_automatic(False)
    assert reloaded.manual_tcp
    await reloaded.async_manual(False)
    assert not reloaded.manual_tcp and not reloaded.automatic_tcp


@pytest.mark.asyncio
async def test_automatic_tcp_hint_persists_until_verified_cloud():
    persistence = store()
    intent = module.ConnectionIntent(persistence)
    await intent.async_automatic(True)
    persistence.async_load.return_value = persistence.async_save.call_args.args[0]
    reloaded = module.ConnectionIntent(persistence)
    await reloaded.async_load()
    assert reloaded.automatic_tcp and not reloaded.manual_tcp
    await reloaded.async_automatic(False)
    assert not reloaded.automatic_tcp


@pytest.mark.asyncio
async def test_failed_save_does_not_change_in_memory_preference():
    persistence = store(); persistence.async_save.side_effect = OSError()
    intent = module.ConnectionIntent(persistence)
    with pytest.raises(OSError):
        await intent.async_manual(True)
    assert not intent.manual_tcp


@pytest.mark.asyncio
@pytest.mark.parametrize("manual,automatic,enabled", [(True,False,False),(True,False,True),(False,True,True),(False,True,False),(False,False,True)])
async def test_initialize_restores_manual_tcp_or_starts_short_cloud_trial(manual, automatic, enabled):
    intent = module.ConnectionIntent(store({"manual_tcp":manual,"automatic_tcp":automatic}))
    auto = types.SimpleNamespace(enabled=enabled, pause=Mock(), start=Mock(), trial_deadline=None, TRIAL_TIMEOUT=60, reason=None)
    owner = types.SimpleNamespace(connection_intent=intent, _handover_store=store(),
        endpoint=types.SimpleNamespace(async_load=AsyncMock(),journal=None),
        charge_mode_policy=types.SimpleNamespace(async_load=AsyncMock(),async_seed_power=AsyncMock()),
        transport=types.SimpleNamespace(accepting=False,async_listen=AsyncMock()),
        initial_power=4.2, port=18899, automatic_fallback=auto,
        _set_local=AsyncMock(), _mark_cloud_handover=AsyncMock())
    await coordinator.NativeCoordinator.async_initialize(owner)
    if manual:
        owner._set_local.assert_awaited_once_with(True)
        auto.pause.assert_called_once()
        assert auto.trial_deadline is None
    elif automatic and enabled:
        assert auto.trial_deadline is not None and auto.reason=="startup_cloud_trial"
        owner._set_local.assert_not_awaited()
        owner._mark_cloud_handover.assert_awaited_once()
    else:
        owner._set_local.assert_not_awaited()
        assert auto.trial_deadline is None
    auto.start.assert_called_once()


@pytest.mark.asyncio
async def test_startup_cloud_read_is_bounded_by_remaining_trial_budget():
    import asyncio
    import time
    async def stuck():
        await asyncio.Event().wait()
    owner = types.SimpleNamespace(
        routing_epoch=0, local=False, transitioning=False, _closed=False,
        automatic_fallback=types.SimpleNamespace(trial_deadline=time.monotonic()+.02, observation=Mock()),
        _async_read_data=stuck,
    )
    with pytest.raises(coordinator.UpdateFailed):
        await asyncio.wait_for(coordinator.NativeCoordinator._async_update_data(owner), 1)
    owner.automatic_fallback.observation.assert_called_once_with(False, "cloud_unavailable", blocked=False)


@pytest.mark.asyncio
async def test_crash_journal_recovers_automatic_hint_before_restoration():
    intent = module.ConnectionIntent(store())
    auto = types.SimpleNamespace(enabled=True, pause=Mock(), start=Mock(), trial_deadline=None, TRIAL_TIMEOUT=60, reason=None)
    owner = types.SimpleNamespace(connection_intent=intent, _handover_store=store(),
        endpoint=types.SimpleNamespace(async_load=AsyncMock(),journal={"original":"owned"},async_restore=AsyncMock()),
        charge_mode_policy=types.SimpleNamespace(async_load=AsyncMock(),async_seed_power=AsyncMock()),
        transport=types.SimpleNamespace(accepting=False,async_listen=AsyncMock()),
        initial_power=4.2, port=18899, automatic_fallback=auto,
        _set_local=AsyncMock(), _mark_cloud_handover=AsyncMock())
    await coordinator.NativeCoordinator.async_initialize(owner)
    assert intent.automatic_tcp and auto.trial_deadline is not None
    owner.endpoint.async_restore.assert_awaited_once()
