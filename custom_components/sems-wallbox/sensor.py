"""
Support for wallbox sensors from GoodWe SEMS API.

For more details about this platform, please refer to the documentation at
https://github.com/TimSoethout/goodwe-sems-home-assistant
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
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

    # V tuto chvíli už má coordinator data (první refresh proběhl v __init__.py)
    sns = list(coordinator.data.keys())

    entities: list[SensorEntity] = []
    for sn in sns:
        entities.append(SemsSensor(coordinator, sn))
        entities.append(SemsWorkStateSensor(coordinator, sn))
        entities.append(SemsStatisticsSensor(coordinator, sn))
        entities.append(SemsPowerSensor(coordinator, sn))

    async_add_entities(entities)


class SemsSensor(CoordinatorEntity, SensorEntity):
    """Hlavní stav wallboxu (Charging / Standby / Offline / Unknown)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Charging", "Standby", "Offline", "Unknown"]
    _attr_should_poll = False

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the status sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsSensor with id %s", self.sn)

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        model = self.coordinator.data.get(self.sn, {}).get("model", "Wallbox")
        return f"Wallbox {model}"

    @property
    def unique_id(self) -> str:
        """Unique ID based on serial number."""
        return self.coordinator.data.get(self.sn, {}).get("sn", self.sn)

    @property
    def state(self) -> str:
        """Return the state of the device as human readable string."""
        data = self.coordinator.data.get(self.sn, {})
        status = data.get("status")

        if status == "EVDetail_Status_Title_Charging":
            return "Charging"
        if status == "EVDetail_Status_Title_Waiting":
            return "Standby"
        if status == "EVDetail_Status_Title_Offline":
            return "Offline"
        return "Unknown"

class SemsWorkStateSensor(CoordinatorEntity, SensorEntity):
    """Workstate of Wallbox (Not Plugged in / Connected / Finished Charging / -- (charging) / Unknown)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Not Plugged in", "Connected", "Finished Charging", "--", "Unknown"]
    _attr_should_poll = False

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        """Initialize the workstate sensor."""
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsWorkStateSensor with id %s", self.sn)

    @property
    def name(self):
        """Set the name of the WorkStatesensor."""
        return f"Wallbox Workstate"

    @property
    def unique_id(self) -> str:
        """Unique ID for workstate sensor."""
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_workstate"
        
    @property
    def native_value(self) -> str:
        """Is car plugged in or not, Return the workstate of the device as human readable string."""
        data = self.coordinator.data.get(self.sn, {})
        workstate = data.get("workstate")

        if workstate == "EVDetail_Status_Waiting_Stat00":
            return "Not Plugged in"
        if workstate == "EVDetail_Status_Waiting_Stat01":
            return "Connected"
        if workstate == "EVDetail_Status_Waiting_Stat02":
            return "Finished Charging"
        if workstate == "":
            return "--"
        return "Unknown"
        
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
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": self.name,
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
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsPowerSensor with id %s", self.sn)

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Wallbox power"

    @property
    def unique_id(self) -> str:
        """Unique ID for power sensor."""
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}_power"

    @property
    def native_value(self) -> float:
        """Return the power in kW."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        try:
            power = float(data.get("power", 0) or 0)
        except (TypeError, ValueError):
            power = 0.0
        return max(0.0, power)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()


class SemsStatisticsSensor(CoordinatorEntity, SensorEntity):
    """Energy sensor in kWh to enable HA statistics."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn
        _LOGGER.debug("Creating SemsStatisticsSensor with id %s", self.sn)

    @property
    def native_value(self) -> Decimal:
        """Return the value reported by the sensor (kWh)."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        raw = data.get("chargeEnergy", 0)

        # API vrací string – ošetříme to defensivně.
        try:
            return Decimal(str(raw))
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Unable to parse chargeEnergy=%s for %s, falling back to 0", raw, self.sn
            )
            return Decimal("0")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Wallbox Energy"

    @property
    def unique_id(self) -> str:
        sn = self.coordinator.data.get(self.sn, {}).get("sn", self.sn)
        return f"{sn}-energy"

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()
