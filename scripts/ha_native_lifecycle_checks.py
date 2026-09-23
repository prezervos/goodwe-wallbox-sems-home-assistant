"""Reproduce lifecycle overlap in real HA with loopback transport only."""
import asyncio
import logging
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState, ConfigEntryDisabler


async def check_lifecycle(hass, entry, device, entered, release):
    """Overlap native handover with reload and disable, then verify recovery."""
    errors = []

    class CaptureErrors(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())

    logger = logging.getLogger("custom_components.sems_wallbox")
    handler = CaptureErrors()
    logger.addHandler(handler)
    for disable in (False, True):
        owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
        entered.clear()
        release.clear()
        moving = asyncio.create_task(owner.async_set_local(True))
        await asyncio.wait_for(entered.wait(), 5)
        version = owner.charge_mode_policy._version
        closing = asyncio.Event()
        original = owner.async_shutdown

        async def shutdown():
            closing.set()
            return await original()

        with patch.object(owner, "async_shutdown", shutdown):
            reloading = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
            await asyncio.wait_for(closing.wait(), 5)
            disabling = None
            if disable:
                disabling = asyncio.create_task(hass.config_entries.async_set_disabled_by(
                    entry.entry_id, ConfigEntryDisabler.USER))
                await asyncio.sleep(0)
            async with asyncio.timeout(5):
                while owner.charge_mode_policy._version == version:
                    await asyncio.sleep(0.01)
            release.set()
            outcome = await asyncio.gather(moving, return_exceptions=True)
            assert await asyncio.wait_for(reloading, 15)
            if disabling is not None:
                assert await asyncio.wait_for(disabling, 15)
                assert entry.state is ConfigEntryState.NOT_LOADED, entry.state
                assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
            assert entry.state is ConfigEntryState.LOADED, entry.state
            print("PASS: overlapping handover/reload", "disable="+str(disable),
                  "handover_result="+type(outcome[0]).__name__, flush=True)
        current = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
        await current.async_set_local(False)
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator
    setup_entered = asyncio.Event()
    setup_release = asyncio.Event()
    initialize = NativeCoordinator.async_initialize

    async def delayed_initialize(owner):
        await initialize(owner)
        setup_entered.set()
        await setup_release.wait()

    with patch.object(NativeCoordinator, "async_initialize", delayed_initialize):
        reloading = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        await asyncio.wait_for(setup_entered.wait(), 10)
        disabling = asyncio.create_task(hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER))
        await asyncio.sleep(0)
        assert entry.disabled_by is ConfigEntryDisabler.USER
        setup_release.set()
        await asyncio.wait_for(reloading, 15)
        assert await asyncio.wait_for(disabling, 15), entry.state
        assert entry.state is ConfigEntryState.NOT_LOADED, entry.state
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    assert entry.state is ConfigEntryState.LOADED
    print("PASS: disable during reload setup; no unloaded-platform failure", flush=True)
    setup_entered.clear()
    setup_release.clear()
    forward = hass.config_entries.async_forward_entry_setups

    async def delayed_forward(selected_entry, platforms):
        platforms = list(platforms)
        await forward(selected_entry, platforms[:1])
        setup_entered.set()
        await setup_release.wait()
        await forward(selected_entry, platforms[1:])

    with patch.object(hass.config_entries, "async_forward_entry_setups", delayed_forward):
        reloading = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        await asyncio.wait_for(setup_entered.wait(), 10)
        disabling = asyncio.create_task(hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER))
        await asyncio.sleep(0)
        setup_release.set()
        await asyncio.wait_for(reloading, 15)
        assert await asyncio.wait_for(disabling, 15), entry.state
        assert entry.state is ConfigEntryState.NOT_LOADED
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    assert entry.state is ConfigEntryState.LOADED
    print("PASS: disable after partial platform setup; clean recovery", flush=True)
    assert 7 not in device.writes, "Lifecycle must never send Start"
    assert await hass.config_entries.async_unload(entry.entry_id)
    logger.removeHandler(handler)
    assert not errors, errors
    print("PASS: final unload, no Start commands or hidden integration errors", flush=True)
