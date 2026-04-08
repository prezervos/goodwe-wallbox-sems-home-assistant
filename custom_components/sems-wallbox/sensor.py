"""Support for wallbox sensors from GoodWe SEMS API."""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SemsUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    runtime: dict[str, Any] = hass.data[DOMAIN][config_entry.entry_id]
    coordinator: SemsUpdateCoordinator = runtime["coordinator"]

    sns = list(coordinator.data.keys())

    entities: list[SensorEntity] = []
    for sn in sns:
        entities.append(SemsSensor(coordinator, sn))
        entities.append(SemsWorkStateSensor(coordinator, sn))
        entities.append(SemsStatisticsSensor(coordinator, sn))
        entities.append(SemsPowerSensor(coordinator, sn))
        entities.append(SemsCurrentSensor(coordinator, sn))

    async_add_entities(entities)


class SemsSensor(CoordinatorEntity, SensorEntity):
    """Main wallbox status sensor (Charging / Standby / Offline / Unknown)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["charging", "standby", "offline", "unknown"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "status"

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the status sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsSensor with id %s", self.sn)

    @property
    def unique_id(self) -> str:
        """Unique ID based on serial number."""
        return self.coordinator.data.get(self.sn, {}).get("sn", self.sn)

    @property
    def state(self) -> str:
        """Return the state of the device as human readable string."""
        data = self.coordinator.data.get(self.sn, {})
        status = data.get("status")
        # Gen2 EU gateway values
        if status in ("EVDetail_Status_Title_Charging", "charging"):
            return "charging"
        if status in ("EVDetail_Status_Title_Waiting", "available", "standby"):
            return "standby"
        if status in ("EVDetail_Status_Title_Offline", "offline", "unavailable"):
            return "offline"
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the monitored installation."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        attributes = {
            k: v for k, v in data.items() if k is not None and v is not None
        }
        if "status" in data:
            attributes["statusText"] = data["status"]
        return attributes

    @property
    def icon(self) -> str:
        """Return dynamic icon based on status."""
        state = self.state
        if state == "charging":
            return "mdi:battery-charging-100"
        if state == "standby":
            return "mdi:ev-station"
        if state == "offline":
            return "mdi:power-plug-off"
        return "mdi:help-circle-outline"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        """Update the entity via the coordinator."""
        await self.coordinator.async_request_refresh()


class SemsWorkStateSensor(CoordinatorEntity, SensorEntity):
    """Workstate sensor for the wallbox EV plug state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["not_plugged_in", "connected", "finished_charging", "dash", "unknown"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "workstate"

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the workstate sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsWorkStateSensor with id %s", self.sn)

    @property
    def unique_id(self) -> str:
        """Unique ID for workstate sensor."""
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_workstate"

    @property
    def native_value(self) -> str:
        """Return the workstate of the device as a human-readable string."""
        data = self.coordinator.data.get(self.sn, {})
        workstate = data.get("workstate")

        # Old semsportal.com API values
        if workstate == "EVDetail_Status_Waiting_Stat00":
            return "not_plugged_in"
        if workstate == "EVDetail_Status_Waiting_Stat01":
            return "connected"
        if workstate == "EVDetail_Status_Waiting_Stat02":
            return "finished_charging"
        # Gen2 EU gateway values
        if workstate in ("available_gun_no_insered", "available_gun_no_inserted"):
            return "not_plugged_in"
        if workstate in ("available_gun_insered", "available_gun_inserted", "prepare"):
            return "connected"
        if workstate in ("finishing", "finish", "suspended_evse", "suspended_ev"):
            return "finished_charging"
        if workstate == "":
            return "dash"
        return "unknown"

    @property
    def icon(self) -> str:
        """Return a dynamic icon based on workstate."""
        state = self.native_value
        if state == "not_plugged_in":
            return "mdi:power-plug-off-outline"
        if state == "connected":
            return "mdi:power-plug"
        if state == "finished_charging":
            return "mdi:battery-check"
        if state == "dash":
            return "mdi:progress-clock"
        return "mdi:help-circle-outline"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        """Update the entity via the coordinator."""
        await self.coordinator.async_request_refresh()


class SemsPowerSensor(CoordinatorEntity, SensorEntity):
    """Instant power sensor in kW."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "power"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the power sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsPowerSensor with id %s", self.sn)

    @property
    def unique_id(self) -> str:
        """Unique ID for power sensor."""
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_power"

    @property
    def native_value(self) -> float:
        """Return the actual charging power in kW.

        When startStatus is False the device is not actively charging — the
        API's chargePower field reflects the configured *limit*, not actual draw.
        Return 0 in that case so HA statistics and energy dashboard are correct.
        """
        data = self.coordinator.data.get(self.sn, {}) or {}
        if not data.get("startStatus", False):
            return 0.0
        try:
            power = float(data.get("power", 0) or 0)
        except (TypeError, ValueError):
            power = 0.0
        return max(0.0, power)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        """Update the entity via the coordinator."""
        await self.coordinator.async_request_refresh()


class SemsStatisticsSensor(CoordinatorEntity, SensorEntity):
    """Energy sensor in kWh to enable HA statistics."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the statistics sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsStatisticsSensor with id %s", self.sn)

    @property
    def native_value(self) -> Decimal:
        """Return the value reported by the sensor (kWh)."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        raw = data.get("chargeEnergy", 0)
        try:
            return Decimal(str(raw))
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Unable to parse chargeEnergy=%s for %s, falling back to 0", raw, self.sn
            )
            return Decimal("0")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def unique_id(self) -> str:
        """Unique ID for energy sensor."""
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}-energy"

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        """Update the entity via the coordinator."""
        await self.coordinator.async_request_refresh()


class SemsCurrentSensor(CoordinatorEntity, SensorEntity):
    """Instantaneous charging current sensor in A."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the current sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsCurrentSensor with id %s", self.sn)

    @property
    def unique_id(self) -> str:
        """Unique ID for current sensor."""
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_current"

    @property
    def native_value(self) -> float:
        """Return the charging current in A.

        The EU gateway detail endpoint does not expose a current field directly.
        When the device is actively charging (startStatus=True), derive it from
        the actual charge power: I = P / U where U ≈ 230 V (single-phase EU).
        When not charging, return 0.
        """
        data = self.coordinator.data.get(self.sn, {}) or {}
        if not data.get("startStatus", False):
            return 0.0
        # Try direct API field first (may be present in future firmware or old API)
        raw = data.get("current")
        if raw is not None:
            try:
                v = float(raw)
                if v > 0:
                    return round(v, 1)
            except (TypeError, ValueError):
                pass
        # Derive from actual charge power (kW → A at 230 V)
        try:
            power_kw = float(data.get("power", 0) or 0)
        except (TypeError, ValueError):
            power_kw = 0.0
        if power_kw <= 0:
            return 0.0
        return round(power_kw * 1000.0 / 230.0, 1)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        """Update the entity via the coordinator."""
        await self.coordinator.async_request_refresh()
