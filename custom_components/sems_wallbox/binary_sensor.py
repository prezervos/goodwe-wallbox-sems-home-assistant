"""Read-only actual charging activity, independent of a requested Start."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .observed_state import charging_active


async def async_setup_entry(hass, entry, async_add_entities):
    """Add observed activity only for the cloud/native coordinator."""
    runtime = hass.data["sems_wallbox"][entry.entry_id]
    if runtime.get("connection_type") == "native_tcp":
        async_add_entities([ChargingActiveSensor(runtime["coordinator"])])


class ChargingActiveSensor(CoordinatorEntity, BinarySensorEntity):
    """Distinguish actual charging from an enabled but waiting PV request."""

    _attr_has_entity_name = True
    _attr_translation_key = "charging_active"
    _attr_device_class = "running"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_charging_active"
        self._attr_device_info = {"identifiers": {("sems_wallbox", coordinator.serial)}}

    @property
    def is_on(self):
        values = (self.coordinator.data or {}).get(self.coordinator.serial, {})
        return charging_active(values, local=self.coordinator.local)
