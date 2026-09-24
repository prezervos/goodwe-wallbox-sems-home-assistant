"""Read-only actual charging activity, independent of a requested Start."""

import time

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .native_fallback import report_age
from .observed_state import energy_flow_active


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
        """Keep unknown/stale measurements distinct from a confirmed zero."""
        owner = self.coordinator
        if (not owner.last_update_success or owner.transitioning or owner._closed
                or not owner.local and owner.cloud_restored_at is not None):
            return None
        values = (owner.data or {}).get(owner.serial, {})
        if values.get("transport") != ("tcp" if owner.local else "cloud"):
            return None
        if owner.local:
            if (not owner.transport.available
                    or values.get("observed_at") != owner.transport.observed_at):
                return None
        else:
            try:
                age = report_age(values, owner.hass.config.time_zone, time.time())
            except (ValueError, TypeError, OverflowError):
                return None
            # Match the coordinator's existing frozen-cloud-report ceiling,
            # including when automatic TCP fallback is disabled.
            if age > 600:
                return None
        return energy_flow_active(values, local=owner.local)
