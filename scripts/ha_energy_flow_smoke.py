#!/usr/bin/env python3
"""Verify energy-flow HA state serialization with no device or cloud I/O."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.sems_wallbox.binary_sensor import ChargingActiveSensor
from custom_components.sems_wallbox.native_entities import ChargingSwitch, ValueSensor


async def main():
    """Run real HA entity transitions in an isolated, in-memory coordinator."""
    with tempfile.TemporaryDirectory() as config:
        hass = HomeAssistant(config)
        loader.async_setup(hass)
        hass.config.skip_pip = True
        await bootstrap.async_from_config_dict({}, hass)
        hass.config.time_zone = "UTC"
        coordinator = DataUpdateCoordinator(hass, logging.getLogger(__name__), name="energy-flow-smoke", config_entry=None)
        coordinator.serial = "TEST"
        coordinator.local = False
        coordinator.transitioning = False
        coordinator._closed = False
        coordinator.cloud_restored_at = None
        coordinator.transport = SimpleNamespace(available=True, observed_at=100.0,
            session_guard=SimpleNamespace(phase="idle", mode=0, error=None))
        coordinator.data = {"TEST": {}}
        coordinator.last_update_success = True
        entity = ChargingActiveSensor(coordinator)
        entity.hass = hass
        entity.entity_id = "binary_sensor.test_energy_flow"
        # Explicit opt-in for this isolated smoke; production default is tested separately.
        entity._attr_entity_registry_enabled_default = True
        component = EntityComponent(logging.getLogger(__name__), "binary_sensor", hass)
        await component.async_setup({})
        await component.async_add_entities([entity])
        charging = ChargingSwitch(coordinator, "TEST_switch", "start_charging")
        charging.entity_id = "switch.test_charging"
        switches = EntityComponent(logging.getLogger(__name__), "switch", hass)
        await switches.async_setup({})
        await switches.async_add_entities([charging])
        status = ValueSensor(coordinator, "TEST_status", "status", "status")
        status.entity_id = "sensor.test_wallbox_status"
        sensors = EntityComponent(logging.getLogger(__name__), "sensor", hass)
        await sensors.async_setup({})
        await sensors.async_add_entities([status])

        def publish(values, expected):
            report = {"transport": "cloud", "status": "EVDetail_Status_Title_Charging",
                      "lastUpdate": datetime.now(timezone.utc).isoformat(), **values}
            coordinator.async_set_updated_data({"TEST": report})
            entity.async_write_ha_state()
            actual = hass.states.get(entity.entity_id).state
            assert actual == expected, (report, actual, expected)

        try:
            publish({"power": "4.1"}, "on")
            publish({"power": "0"}, "off")
            publish({}, "unknown")
            publish({"power": "nan"}, "unknown")
            publish({"power": "0", "lastUpdate": (datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()}, "unknown")
            publish({"power": "0"}, "off")
            coordinator.last_update_success = False
            entity.async_write_ha_state()
            assert hass.states.get(entity.entity_id).state == "unavailable"
            coordinator.last_update_success = True
            coordinator.transitioning = True
            publish({"power": "0"}, "unknown")
            coordinator.transitioning = False
            coordinator.local = True
            publish({"power": "0"}, "unknown")
            publish({"transport": "tcp", "observed_at": 100.0,
                     "power": 0, "currents_a": [0, 0, 0]}, "off")
            coordinator.transport.available = False
            publish({"transport": "tcp", "observed_at": 100.0,
                     "power": 0, "currents_a": [0, 0, 0]}, "unknown")
            coordinator.transport.available = True
            for phase, reported_status, power, expected_switch, expected_status in [
                ("idle", "standby", 0, "off", "standby"),
                ("starting", "standby", 0, "on", "starting"),
                ("charging", "charging", 4.1, "on", "charging"),
                ("stopped", "standby", 0, "off", "standby"),
                ("starting", "standby", 0, "on", "starting"),
                ("stopped", "standby", 0, "off", "standby"),
            ]:
                coordinator.transport.session_guard.phase = phase
                publish({"transport": "tcp", "observed_at": 100.0,
                         "status": reported_status, "power": power,
                         "currents_a": [6, 6, 6] if power else [0, 0, 0]},
                        "on" if power else "off")
                charging.async_write_ha_state()
                status.async_write_ha_state()
                assert hass.states.get(charging.entity_id).state == expected_switch
                assert hass.states.get(status.entity_id).state == expected_status
            coordinator.last_update_success = False
            charging.async_write_ha_state()
            assert hass.states.get(charging.entity_id).state == "unavailable"
            print("Real HA energy-flow, supervised Start display and handover smoke passed")
        finally:
            await switches.async_remove_entity(charging.entity_id)
            await sensors.async_remove_entity(status.entity_id)
            await component.async_remove_entity(entity.entity_id)
            await hass.async_stop()


if __name__ == "__main__":
    asyncio.run(main())
