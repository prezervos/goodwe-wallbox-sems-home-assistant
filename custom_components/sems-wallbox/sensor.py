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
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
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
        entities.append(SemsChargePowerLimitSensor(coordinator, sn))
        entities.append(SemsChargeDurationSensor(coordinator, sn))

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
        # workStu=6 from getLastCharge is the authoritative charging signal.
        # The detail endpoint's status field is always 'available' in PV mode.
        if data.get("last_charge_work_status") == 6:
            return "charging"
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
        """Return meaningful state attributes."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        attrs: dict[str, Any] = {}
        # Raw status string for display / automations
        if data.get("status"):
            attrs["statusText"] = data["status"]
        # Scheduling
        for key in ("chargeMode", "scheduleMode", "schedule_total_minute"):
            if (v := data.get(key)) is not None:
                attrs[key] = v
        # Power management
        for key in ("set_charge_power", "charge_from_grid", "ensure_minimum_charging_power"):
            if (v := data.get(key)) is not None:
                attrs[key] = v
        # Last charge session (from getLastCharge)
        for key in ("last_charge_work_status", "last_charge_power", "last_charge_duration_minutes"):
            if (v := data.get(key)) is not None:
                attrs[key] = v
        return attrs

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
        # When actively charging, the Gen2 API still reports 'available_gun_no_insered'.
        # Override with dash (as the old API did via empty string during charging).
        if data.get("last_charge_work_status") == 6:
            return "dash"
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
        """Return the actual charging power in kW; 0 when not actively charging.

        Uses pevChar from getLastCharge (last_charge_power) as the real drawn
        power.  The detail endpoint's chargePower is the inverter allocation
        limit, which can differ (e.g. 2-phase vs 3-phase sessions).
        """
        data = self.coordinator.data.get(self.sn, {}) or {}
        if data.get("last_charge_work_status") != 6:
            return 0.0
        try:
            power = float(data.get("last_charge_power") or 0)
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
    """Energy sensor in kWh — shows current session energy from getLastCharge."""

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
    def native_value(self) -> Decimal | None:
        """Return current session energy in kWh (currentChargeQuantity from getLastCharge)."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        raw = data.get("last_charge_energy")
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:  # noqa: BLE001
            return None

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


class SemsChargePowerLimitSensor(CoordinatorEntity, SensorEntity):
    """Readonly sensor for the current allocated charge power limit (kW).

    In PV modes (1 & 2) the inverter dynamically adjusts this value based on
    solar / battery availability.  In Fast mode (0) it reflects the configured
    fixed limit.  Always available — unlike the number entity which is only
    editable in Fast mode.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "set_charge_power_limit"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the charge power limit sensor."""
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_set_charge_power_limit"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("set_charge_power")
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
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


class SemsChargeDurationSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing the duration of the current (or last) charge session in minutes."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "charge_duration"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_charge_duration"

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("last_charge_duration_minutes")
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
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
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()
