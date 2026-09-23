"""Support for wallbox sensors from GoodWe SEMS API."""

from __future__ import annotations

from decimal import Decimal
import logging
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONN_TYPE_MODBUS
from .coordinator import SemsUpdateCoordinator
from .observed_state import charging_active

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    runtime: dict[str, Any] = hass.data[DOMAIN][config_entry.entry_id]
    coordinator: SemsUpdateCoordinator = runtime["coordinator"]
    conn_type = runtime.get("connection_type", "cloud")
    if conn_type == "native_tcp":
        from .native_entities import setup_platform
        setup_platform("sensor", runtime["coordinator"], async_add_entities)
        return

    sns = list(coordinator.data.keys())

    entities: list[SensorEntity] = []
    for sn in sns:
        entities.append(SemsSensor(coordinator, sn))
        entities.append(SemsWorkStateSensor(coordinator, sn))
        entities.append(SemsStatisticsSensor(coordinator, sn))
        entities.append(SemsPowerSensor(coordinator, sn))
        entities.append(SemsChargePowerLimitSensor(coordinator, sn))
        entities.append(SemsChargeDurationSensor(coordinator, sn))
        # Add Modbus-specific sensors when running in local Modbus mode
        if conn_type == CONN_TYPE_MODBUS:
            entities.extend([
                SemsModbusEnergyTotalSensor(coordinator, sn),
                SemsModbusVoltageSensor(coordinator, sn, "a"),
                SemsModbusVoltageSensor(coordinator, sn, "b"),
                SemsModbusVoltageSensor(coordinator, sn, "c"),
                SemsModbusCurrentSensor(coordinator, sn, "a"),
                SemsModbusCurrentSensor(coordinator, sn, "b"),
                SemsModbusCurrentSensor(coordinator, sn, "c"),
                SemsModbusStatusSensor(coordinator, sn),
                SemsModbusCarConnectionSensor(coordinator, sn),
                SemsModbusFaultSensor(coordinator, sn),
                SemsModbusCommStatusSensor(coordinator, sn),
                SemsModbusStartModeSensor(coordinator, sn),
                SemsModbusChargingStrategySensor(coordinator, sn),
                SemsModbusAppointmentSensor(coordinator, sn),
                SemsModbusPowerSourceSensor(coordinator, sn),
            ])

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
    _attr_options = ["not_plugged_in", "connected", "finished_charging", "charged", "dash", "unknown"]
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
        # Last-session workStu=8 also follows manual Stop and can outlive the
        # connection. It must not replace the current vehicle observation.
        if charging_active(data, local=False) is True:
            return "connected"
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
        if state in ("finished_charging", "charged"):
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
    def native_value(self) -> float | None:
        """Return the actual charging power in kW; 0 when not actively charging.

        Uses pevChar from getLastCharge (last_charge_power) as the real drawn
        power.  The detail endpoint's chargePower is the inverter allocation
        limit, which can differ (e.g. 2-phase vs 3-phase sessions).
        """
        data = self.coordinator.data.get(self.sn, {}) or {}
        work_status = data.get("last_charge_work_status")
        if work_status is None:
            return None
        if work_status != 6:
            return 0.0
        raw = data.get("last_charge_power")
        if raw is None or isinstance(raw, bool):
            return None
        try:
            power = float(raw)
        except (TypeError, ValueError):
            return None
        return power if math.isfinite(power) and power >= 0 else None

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
    """Energy sensor in kWh -- shows current session energy from getLastCharge."""

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
    fixed limit.  Always available -- unlike the number entity which is only
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


# ---------------------------------------------------------------------------
# Modbus-specific sensor classes (only created in local Modbus mode)
# ---------------------------------------------------------------------------


def _device_info(coordinator, sn: str) -> dict:
    data = coordinator.data.get(sn, {}) or {}
    return {
        "identifiers": {(DOMAIN, sn)},
        "name": data.get("name") or f"GoodWe Wallbox {sn}",
        "manufacturer": "GoodWe",
        "model": data.get("model", "unknown"),
        "sw_version": data.get("modbus_sw_version") or data.get("fireware", "unknown"),
    }


class SemsModbusVoltageSensor(CoordinatorEntity, SensorEntity):
    """Phase voltage sensor (A, B or C) read via Modbus."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sn: str, phase: str) -> None:
        super().__init__(coordinator)
        self.sn = sn
        self._phase = phase.upper()
        self._field = f"modbus_voltage_{phase.lower()}"
        self._attr_translation_key = f"voltage_{phase.lower()}"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_voltage_{self._phase}"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get(self._field)
        return round(float(v), 1) if v is not None else None

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusCurrentSensor(CoordinatorEntity, SensorEntity):
    """Phase current sensor (A, B or C) read via Modbus."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sn: str, phase: str) -> None:
        super().__init__(coordinator)
        self.sn = sn
        self._phase = phase.upper()
        self._field = f"modbus_current_{phase.lower()}"
        self._attr_translation_key = f"current_{phase.lower()}"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_current_{self._phase}"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get(self._field)
        return round(float(v), 1) if v is not None else None

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusEnergyTotalSensor(CoordinatorEntity, SensorEntity):
    """Total accumulated energy sensor read via Modbus (register 10065)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_energy_total"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_energy_total"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_energy_total")
        return round(float(v), 1) if v is not None else None

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusStatusSensor(CoordinatorEntity, SensorEntity):
    """Direct Modbus status sensor (register 10017, numeric 0-10)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "idle_no_plug", "idle_plugged", "handshaking", "charging",
        "charging_completed", "abnormal_alarm", "scheduled_start",
        "maintenance", "start_failed", "upgrading", "interrupted", "unknown",
    ]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_charging_status"

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_status"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("modbus_status_name", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data.get(self.sn, {}) or {}
        attrs = {}
        if (v := data.get("modbus_status_raw")) is not None:
            attrs["status_raw"] = v
        for key in (
            "modbus_ems_dispatch",
            "modbus_fault_01", "modbus_fault_02", "modbus_fault_03", "modbus_fault_04",
            "modbus_warn_05", "modbus_warn_06",
            "modbus_hw_fault_07", "modbus_hw_fault_08",
            "modbus_comm_status", "modbus_power_source",
        ):
            if (v := data.get(key)) is not None:
                attrs[key] = v
        # Decoded fault / warning bits
        for reg_key, bit_names, label in (
            ("modbus_fault_01", _AC_FAULT_01_BITS, "faults_01"),
            ("modbus_fault_02", _AC_FAULT_02_BITS, "faults_02"),
            ("modbus_fault_03", _AC_FAULT_03_BITS, "faults_03"),
            ("modbus_hw_fault_07", _HW_FAULT_07_BITS, "hw_faults_07"),
            ("modbus_warn_05", _WARN_05_BITS, "warnings_05"),
            ("modbus_warn_06", _WARN_06_BITS, "warnings_06"),
        ):
            raw = data.get(reg_key)
            if raw:
                active = _decode_bits(raw, bit_names)
                if active:
                    attrs[label] = active
        return attrs

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusCarConnectionSensor(CoordinatorEntity, SensorEntity):
    """Car connection state sensor (register 10075 and CP voltage 10084)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["disconnected", "half_connected", "connected", "unknown"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_car_connection"

    _CAR_MAP = {0: "disconnected", 1: "half_connected", 2: "connected"}

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_car_connection"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_car_connected")
        return self._CAR_MAP.get(v, "unknown") if v is not None else "unknown"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data.get(self.sn, {}) or {}
        attrs = {}
        if (v := data.get("modbus_cp_state_name")) is not None:
            attrs["cp_voltage"] = v
        if (v := data.get("modbus_cp_state")) is not None:
            attrs["cp_state_raw"] = v
        return attrs

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


# Bit-field descriptions for fault / warning registers  (per protocol V1.0.15)
_AC_FAULT_01_BITS = {
    0: "Emergency stop",
    1: "AC overvoltage",
    2: "AC overcurrent",
    3: "AC undervoltage",
    4: "Connector fault",
    5: "S2 disconnected",
    6: "Environment overtemp",
    7: "Gun overtemp",
}

_AC_FAULT_02_BITS = {
    0: "Door access fault",
    1: "Grounding fault",
    2: "Handshake timeout",
    3: "RF card comm fault",
    4: "Serial display comm fault",
    5: "On-board meter IC comm fault",
    6: "Output relay fault",
    7: "Gun lock fault",
}

_AC_FAULT_03_BITS = {
    0: "Output short circuit",
    1: "Leakage current",
    2: "Charge pause >10 min",
    3: "Abnormal meter reading",
    4: "Charger offline on PV/battery start",
    5: "Insufficient PV/battery power",
}

_HW_FAULT_07_BITS = {
    0: "External flash fault",
    1: "EEPROM fault",
    2: "Leak detection device fault",
    3: "Abnormal input power",
    4: "SN not registered",
    5: "Factory parameters abnormal",
    6: "Unauthorized firmware",
}

_WARN_05_BITS = {
    0: "Gun overtemp alarm",
    1: "Grounding alarm",
    2: "Handshake timeout alarm",
    3: "RF card comm alarm",
    4: "Serial display comm alarm",
    5: "On-board meter IC comm alarm",
    6: "Charging stop alarm",
    7: "Abnormal meter reading alarm",
}

_WARN_06_BITS = {
    0: "Environment overtemp alarm",
}


def _decode_bits(value, bit_names):
    if value is None or value == 0:
        return []
    return [name for bit, name in bit_names.items() if value & (1 << bit)]


class SemsModbusFaultSensor(CoordinatorEntity, SensorEntity):
    """Aggregate fault / warning state sensor for Modbus mode.

    State is one of: ok | warning | fault
    Extra attributes list all active fault and warning bits.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "warning", "fault"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_fault_state"

    def __init__(self, coordinator, sn):
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self):
        return f"{self.sn}_modbus_fault_state"

    @property
    def native_value(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        fault_regs = (
            data.get("modbus_fault_01"),
            data.get("modbus_fault_02"),
            data.get("modbus_fault_03"),
            data.get("modbus_hw_fault_07"),
        )
        warn_regs = (
            data.get("modbus_warn_05"),
            data.get("modbus_warn_06"),
        )
        if any(v for v in fault_regs if v):
            return "fault"
        if any(v for v in warn_regs if v):
            return "warning"
        return "ok"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        attrs = {}

        for reg_key, bit_names, label in (
            ("modbus_fault_01", _AC_FAULT_01_BITS, "ac_fault_01"),
            ("modbus_fault_02", _AC_FAULT_02_BITS, "ac_fault_02"),
            ("modbus_fault_03", _AC_FAULT_03_BITS, "ac_fault_03"),
            ("modbus_hw_fault_07", _HW_FAULT_07_BITS, "hw_fault_07"),
            ("modbus_warn_05", _WARN_05_BITS, "warning_05"),
            ("modbus_warn_06", _WARN_06_BITS, "warning_06"),
        ):
            raw = data.get(reg_key)
            if raw is not None:
                attrs[f"{label}_raw"] = raw
                active = _decode_bits(raw, bit_names)
                if active:
                    attrs[label] = active

        return attrs

    @property
    def available(self):
        return self.coordinator.last_update_success

    @property
    def device_info(self):
        return _device_info(self.coordinator, self.sn)


# ---------------------------------------------------------------------------
# Charge start mode (reg 10076), charging strategy (reg 10077),
# appointment sign (reg 10079), power source (reg 10108)
# ---------------------------------------------------------------------------

_START_MODE_MAP: dict[int, str] = {
    0: "auth_card",
    1: "backend",
    2: "local_admin",
    3: "vin",
    4: "wallet_card",
    5: "plug_and_charge",
    6: "scheduled",
    7: "bluetooth",
}

_CHARGING_STRATEGY_MAP: dict[int, str] = {
    0: "auto_full",
    1: "fill_by_time",
    2: "fixed_amount",
    3: "charge_by_energy",
}

_POWER_SOURCE_MAP: dict[int, str] = {
    0x00: "none",
    0x01: "grid",
    0x02: "pv",
    0x03: "grid_pv",
    0x04: "battery",
    0x05: "grid_battery",
    0x06: "pv_battery",
    0x07: "grid_pv_battery",
}


class SemsModbusStartModeSensor(CoordinatorEntity, SensorEntity):
    """How the current / last charge session was started (reg 10076)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_START_MODE_MAP.values()) + ["unknown"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_start_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_start_mode"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_start_mode")
        if v is None:
            return "unknown"
        return _START_MODE_MAP.get(v, "unknown")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusChargingStrategySensor(CoordinatorEntity, SensorEntity):
    """Active charging strategy (reg 10077: 0=auto full, 1=by time, 2=fixed amount, 3=by energy)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_CHARGING_STRATEGY_MAP.values()) + ["unknown"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_charging_strategy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_charging_strategy"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_charging_strategy")
        if v is None:
            return "unknown"
        return _CHARGING_STRATEGY_MAP.get(v, "unknown")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusAppointmentSensor(CoordinatorEntity, SensorEntity):
    """Reservation (appointment) active flag (reg 10079: 0=none, 1=reservation valid)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["no_reservation", "reserved"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_appointment_sign"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_appointment_sign"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_appointment_sign")
        return "reserved" if v else "no_reservation"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


class SemsModbusPowerSourceSensor(CoordinatorEntity, SensorEntity):
    """Active power source during charging (reg 10108: bit0=grid, bit1=PV, bit2=battery)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_POWER_SOURCE_MAP.values()) + ["unknown"]
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_power_source_sensor"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_power_source"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_power_source")
        if v is None:
            return "unknown"
        return _POWER_SOURCE_MAP.get(v & 0x07, "unknown")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)


# Bit labels for register 10018 — Communication Connection Status
_COMM_STATUS_BITS = {
    0: "wifi",
    1: "iot_cloud",
    2: "inverter",
    3: "mid_meter",
    4: "gw_meter",
    5: "ems",
}

class SemsModbusCommStatusSensor(CoordinatorEntity, SensorEntity):
    """Communication connection status sensor (register 10018).

    Reports the number of active connections as state.
    Individual connection bits are exposed as extra_state_attributes.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_comm_status"
    _attr_icon = "mdi:network"
    _attr_native_unit_of_measurement = None
    _attr_state_class = None

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_comm_status"

    @property
    def native_value(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        raw = data.get("modbus_comm_status")
        if raw is None:
            return None
        active = sum(1 for bit in _COMM_STATUS_BITS if raw & (1 << bit))
        return active

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data.get(self.sn, {}) or {}
        raw = data.get("modbus_comm_status")
        if raw is None:
            return {}
        attrs = {"raw": raw}
        for bit, key in _COMM_STATUS_BITS.items():
            attrs[key] = bool(raw & (1 << bit))
        return attrs

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return _device_info(self.coordinator, self.sn)
