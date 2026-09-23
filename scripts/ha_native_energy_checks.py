"""Real HA cumulative entity, source availability and reset semantics checks."""

import asyncio

from homeassistant.components.sensor.recorder import reset_detected
from homeassistant.helpers import entity_registry as er


async def check_energy(hass, entry, device, serial, service, connection):
    """Enable the optional sensor and verify actual TCP frames reach its HA state."""
    registry = er.async_get(hass)
    item = next(e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if e.unique_id == serial + "_native_total_energy")
    assert item.unique_id != serial + "-energy"
    assert device.energy_reads == 0, "Disabled energy entity must generate no reads"
    registry.async_update_entity(item.entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(item.entity_id).state == "unavailable"
    await service("switch", "turn_on", connection)
    for _ in range(80):
        await asyncio.sleep(.1)
        if hass.states.get(item.entity_id).state == "1234.56":break
    else:
        owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
        poller = owner.energy_polling
        raise AssertionError({"state":hass.states.get(item.entity_id).state,
            "enabled":poller.enabled,"error":poller.error,"value":str(poller.value),
            "busy":owner.transport.optional_read_busy,"reads":device.energy_reads,
            "next_read":poller.next_read,"epoch":poller.epoch,"session_epoch":owner.transport.epoch,
            "phase":owner.transport.session_guard.phase,"latest":str(owner.transport.latest),
            "task_error":str(poller.task.exception()) if poller.task and poller.task.done() else None})
    state = hass.states.get(item.entity_id)
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert not reset_detected(hass, item.entity_id, 1234.56, None, state)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
    assert owner.local
    for _ in range(80):
        await asyncio.sleep(.1)
        if hass.states.get(item.entity_id).state == "1234.56":break
    else:raise AssertionError("Energy did not recover after reload")
    assert registry.async_get(item.entity_id).unique_id == item.unique_id
    device.energy_raw = 0
    await owner.transport.async_command("status")
    owner.energy_polling.next_read = 0
    await owner.energy_polling.tick()
    await hass.async_block_till_done()
    state = hass.states.get(item.entity_id)
    assert float(state.state) == 0
    assert reset_detected(hass, item.entity_id, 0, 1234.56, state)
    device.energy_raw = 12
    await owner.transport.async_command("status")
    owner.energy_polling.next_read = 0
    await owner.energy_polling.tick()
    await hass.async_block_till_done()
    state = hass.states.get(item.entity_id)
    assert float(state.state) == .12
    assert not reset_detected(hass, item.entity_id, .12, 0, state)
    await service("switch", "turn_off", connection)
    await hass.async_block_till_done()
    assert hass.states.get(item.entity_id).state == "unavailable"
    print("PASS: optional energy read, units/statistics class, reload identity, reset and cloud unavailability")
