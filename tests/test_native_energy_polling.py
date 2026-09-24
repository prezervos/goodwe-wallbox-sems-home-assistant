"""Optional cumulative polling never invents energy or changes transport health."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.test_native_energy import energy, PACKAGE

module = importlib.import_module(PACKAGE + ".native_energy_polling")
idle_module = importlib.import_module(PACKAGE + ".native_idle_polling")


def subject():
    transport = SimpleNamespace(epoch=1, available=True, optional_read_busy=False,
        latest=SimpleNamespace(stopped=True,state=0),session_guard=SimpleNamespace(phase="idle"),
        async_read_energy=AsyncMock(return_value=energy.NativeEnergy(100, 5000, 10)))
    owner = SimpleNamespace(transport=transport, local=True, transitioning=False,
        last_update_success=True, _closed=False, async_update_listeners=Mock(),
        hass=SimpleNamespace(async_create_background_task=lambda c,n: asyncio.create_task(c)))
    return module.NativeEnergyPolling(owner)


@pytest.mark.asyncio
async def test_disabled_entity_does_not_read():
    poller=subject();await poller.tick()
    poller.owner.transport.async_read_energy.assert_not_awaited()
    assert not poller.available


@pytest.mark.asyncio
async def test_idle_interval_and_session_end_refresh():
    poller=subject();poller.enabled=True
    with patch.object(idle_module.time,"monotonic",return_value=10):
        await poller.tick();await poller.tick()
    assert poller.available and poller.value.energy_kwh==100
    poller.owner.transport.async_read_energy.assert_awaited_once()
    poller.owner.transport.latest.stopped=False
    await poller.tick()
    poller.owner.transport.latest.stopped=True
    with patch.object(idle_module.time,"monotonic",return_value=20):await poller.tick()
    assert poller.owner.transport.async_read_energy.await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cloud","transition","epoch"])
async def test_handover_never_exposes_old_energy(change):
    poller=subject();poller.enabled=True;await poller.tick()
    if change=="cloud":poller.owner.local=False
    elif change=="transition":poller.owner.transitioning=True
    else:poller.owner.transport.epoch+=1
    assert not poller.available
    poller.owner.transport.optional_read_busy=True
    await poller.tick()
    assert poller.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(),ValueError(),ConnectionError()])
async def test_optional_read_failure_does_not_poison_coordinator(error):
    poller=subject();poller.enabled=True
    poller.owner.transport.async_read_energy.side_effect=error
    await poller.tick()
    assert not poller.available and poller.value is None
    assert poller.owner.last_update_success and poller.owner.transport.available
    assert poller.error=="read_failed"


@pytest.mark.asyncio
async def test_real_zero_or_counter_reset_is_preserved_without_offsets():
    poller=subject();poller.enabled=True;await poller.tick()
    poller.next_read=0
    poller.owner.transport.async_read_energy.return_value=energy.NativeEnergy(0,0,0)
    await poller.tick()
    assert poller.available and poller.value.energy_kwh==0
    poller.next_read=0
    poller.owner.transport.async_read_energy.return_value=energy.NativeEnergy(.12,4,1)
    await poller.tick()
    assert poller.value.energy_kwh==.12


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["starting","waiting","charging"])
async def test_no_snapshot_during_active_control_or_pv_wait(phase):
    poller=subject();poller.enabled=True
    poller.owner.transport.session_guard.phase=phase
    await poller.tick()
    poller.owner.transport.async_read_energy.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_controls_have_priority():
    poller=subject();poller.enabled=True
    poller.owner.transport.optional_read_busy=True
    await poller.tick()
    poller.owner.transport.async_read_energy.assert_not_awaited()


@pytest.mark.asyncio
async def test_inflight_result_from_old_session_is_discarded():
    poller=subject();poller.enabled=True
    async def read():
        poller.owner.transport.epoch+=1
        return energy.NativeEnergy(100,5000,10)
    poller.owner.transport.async_read_energy.side_effect=read
    await poller.tick()
    assert poller.value is None and not poller.available


@pytest.mark.asyncio
async def test_close_cancels_and_awaits_optional_read():
    poller=subject();entered=asyncio.Event();cancelled=asyncio.Event()
    async def read():
        entered.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    poller.owner.transport.async_read_energy.side_effect=read
    poller.start()
    await asyncio.wait_for(entered.wait(),1)
    await poller.close()
    assert cancelled.is_set() and poller.task is None and not poller.available


@pytest.mark.asyncio
async def test_fresh_status_wakes_reader_instead_of_starving_between_poll_intervals():
    poller=subject();poller.owner.transport.optional_read_busy=True
    entered=asyncio.Event()
    async def read():
        entered.set()
        return energy.NativeEnergy(100,5000,10)
    poller.owner.transport.async_read_energy.side_effect=read
    poller.start()
    try:
        await asyncio.sleep(0)
        poller.owner.transport.async_read_energy.assert_not_awaited()
        poller.owner.transport.optional_read_busy=False
        poller.wake()
        await asyncio.wait_for(entered.wait(),.5)
        assert poller.value.energy_kwh==100
    finally:
        await poller.close()
