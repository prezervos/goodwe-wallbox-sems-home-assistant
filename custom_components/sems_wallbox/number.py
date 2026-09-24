"""Support for number entity controlling GoodWe SEMS Wallbox charge power."""

from __future__ import annotations

from .optimistic_write import optimistic_write

import logging
import time

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .operation_budget import async_execute
from .const import DOMAIN, CONN_TYPE_MODBUS, CAP_OUTPUT_POWER_SETTING, CAP_DYNAMIC_LOAD_CONTROL
from .charge_mode_policy import mode_setting_write, prepare_fast_power
from .coordinator import SemsUpdateCoordinator
from .wallbox_modbus import BREAKER_CURRENT_MIN, BREAKER_CURRENT_MAX
from .cloud_current_limit import (
    current_attributes, current_bounds, current_writable, observed_current, validate_current_write,
)
from .ui_errors import operation_error

_LOGGER = logging.getLogger(__name__)



async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add numbers for passed config_entry in HA."""
    runtime = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = runtime["coordinator"]
    conn_type = runtime.get("connection_type", "cloud")
    if conn_type == "native_tcp":
        from .native_entities import setup_platform
        setup_platform("number", runtime["coordinator"], async_add_entities)
        return

    if conn_type == CONN_TYPE_MODBUS:
        client = runtime["modbus_client"]
        entities = []
        for sn in coordinator.data:
            entities.append(ModbusMaxChargePowerNumber(coordinator, sn, client))
            entities.append(ModbusMaxChargeCapacityNumber(coordinator, sn, client))
            entities.append(ModbusMinChargeCapacityNumber(coordinator, sn, client))
            entities.append(ModbusBatteryDischargeSocNumber(coordinator, sn, client))
            entities.append(ModbusCurrentLimitNumber(coordinator, sn, client))
        async_add_entities(entities)
        return

    api = runtime["api"]
    caps = runtime.get("capabilities", {})
    more_controls = caps.get("more_device_controls", [])

    _LOGGER.debug(
        "Setting up SemsNumber entities for entry %s",

        config_entry.entry_id,
    )

    entities: list[SemsNumber] = []
    for sn, data in coordinator.data.items():
        set_charge_power = data.get("set_charge_power")
        # Charge power slider: show when Output_Power_Setting is listed OR cap list is empty
        if not more_controls or CAP_OUTPUT_POWER_SETTING in more_controls:
            entities.append(SemsNumber(coordinator, sn, api, set_charge_power))
        # Mode-param numbers: available when in the relevant mode (mode 0/2)
        entities.append(SemsMaxEnergyNumber(coordinator, sn, api))
        entities.append(SemsTargetSocNumber(coordinator, sn, api))
        entities.append(SemsMinEnergyNumber(coordinator, sn, api))
        # Output power limit: part of Dynamic Load Management
        if CAP_DYNAMIC_LOAD_CONTROL in more_controls:
            entities.append(SemsOutputPowerLimitNumber(coordinator, sn, api))
        # Current limit: always add (virtually all wallboxes support it)
        try:
            info = await async_execute(hass, api.fetch_device_info, sn)
        except (OSError, ValueError, RuntimeError) as error:
            _LOGGER.debug("Current-limit metadata discovery failed: %s", error)
            info = None
        data["controlItemRanges"] = (
            info.get("controlItemRanges") if isinstance(info, dict) and info else False
        )
        entities.append(SemsCurrentLimitNumber(coordinator, sn, api))

    async_add_entities(entities)


class SemsNumber(CoordinatorEntity, NumberEntity):
    """Number entity for setting wallbox charge power."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "charge_power"

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api, value: float):
        """Initialize the number entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.api = api
        self.sn = sn
        self._attr_native_value = float(value) if value is not None else None
        # Grace period tracking: ignore stale coordinator updates after a set
        self._pending_value: float | None = None
        self._pending_until: float = 0.0
        _LOGGER.debug(
            "Creating SemsNumber for Wallbox %s, initial value=%s",

            self.sn,
            self._attr_native_value,
        )

    @property
    def native_value(self):
        """Expose saved HA intent separately from reported device power."""
        policy = getattr(self.coordinator, "charge_mode_policy", None)
        if policy is not None and policy.desired_power is not None:
            return policy.desired_power
        return self._attr_native_value

    @property
    def extra_state_attributes(self):
        """Expose the reported setpoint without overwriting the HA preference."""
        return {"reported_power_limit": (self.coordinator.data.get(self.sn, {}) or {}).get("set_charge_power")}

    @property
    def device_class(self):
        """Return the device class."""
        return NumberDeviceClass.POWER

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfPower.KILO_WATT

    @property
    def native_step(self):
        """Return the step value."""
        return 0.1

    def _model_limits(self) -> tuple[float, float]:
        """Return (min_kW, max_kW) as fallback based on product model string.

        Per-model defaults when API doesn't return min/max:
          GW7  → 1.4 -  7.0 kW
          GW11 → 4.2 - 11.0 kW
          GW22 → 4.2 - 22.0 kW
        Defaults to GW7 range when model is unknown (smallest / safest).
        """
        model = ((self.coordinator.data.get(self.sn, {}) or {}).get("model") or "").upper()
        if "GW22" in model:
            return 4.2, 22.0
        if "GW11" in model:
            return 4.2, 11.0
        if "GW7" in model:
            return 1.4, 7.0
        return 1.4, 7.0  # safe default (smallest model)

    @property
    def native_min_value(self) -> float:
        """Return the minimum value, read from API data when available."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("min_charge_power")
        try:
            return float(v) if v is not None else self._model_limits()[0]
        except (TypeError, ValueError):
            return self._model_limits()[0]

    @property
    def native_max_value(self) -> float:
        """Return the maximum value, read from API data when available."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("max_charge_power")
        try:
            return float(v) if v is not None else self._model_limits()[1]
        except (TypeError, ValueError):
            return self._model_limits()[1]

    @property
    def unique_id(self) -> str:
        """Return unique id."""
        return f"{self.coordinator.data[self.sn]['sn']}_number_set_charge_power"

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": (self.coordinator.data.get(self.sn, {}) or {}).get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        _LOGGER.debug("SemsNumber added to hass for wallbox %s", self.sn)

    @property
    def available(self) -> bool:
        """Allow preparing a Fast limit while a supported PV mode is selected."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in (0, 1, 2)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        set_charge_power = data.get("set_charge_power")

        # Grace period: after a set, ignore stale API values until device catches up
        now = time.monotonic()
        if self._pending_value is not None and now < self._pending_until:
            if set_charge_power is not None:
                try:
                    if abs(float(set_charge_power) - self._pending_value) < 0.05:
                        self._pending_value = None
                        self._attr_native_value = float(set_charge_power)
                    # else: still stale -- keep _attr_native_value at pending value
                except (TypeError, ValueError):
                    pass
        else:
            # Grace expired or no pending set -- always accept the API value
            if self._pending_value is not None:
                self._pending_value = None
            if set_charge_power is not None:
                try:
                    self._attr_native_value = float(set_charge_power)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "SemsNumber %s: invalid set_charge_power value %r from API",
                        self.sn,
                        set_charge_power,
                    )

        _LOGGER.debug(
            "SemsNumber coordinator update SN=%s -> native_value=%s, available=%s",
            self.sn,
            self._attr_native_value,
            self.available,
        )
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Manual update from HA."""
        await self.coordinator.async_request_refresh()

    @prepare_fast_power
    @mode_setting_write(desired_mode=0, remember_power=True)
    @optimistic_write
    async def async_set_native_value(self, value: float) -> None:
        """Handle change from UI slider -- switches to Fast mode (0) with the given power."""
        _LOGGER.debug(
            "Setting set_charge_power for SN=%s to %s",
            self.sn,
            value,
        )

        # 1) Optimistic UI update -- also write the new power directly into
        # coordinator.data (without going through async_set_updated_data) so
        # that an in-flight select.py mode-switch call can detect it after its
        # own API call finishes and re-send with the correct power.
        # We deliberately avoid async_set_updated_data here: calling it would
        # trigger _handle_coordinator_update on the select entity, which could
        # see the optimistically-written chargeMode and prematurely clear
        # _pending_mode -- causing the very revert we are trying to prevent.
        self._attr_native_value = float(value)
        # Start grace period immediately so coordinator updates during the API
        # call (which can take several seconds) don't revert the optimistic value.
        self._pending_value = float(value)
        self._pending_until = time.monotonic() + 120.0
        device = self.coordinator.data.get(self.sn)
        if device is not None:
            device["set_charge_power"] = float(value)
        self._optimistic_write.capture_device(("set_charge_power",))
        self.async_write_ha_state()

        # 2) Call SEMS API -- always Fast mode (0)
        ok = await async_execute(self.hass,
            self.api.set_charge_mode_gen2,
            self.sn,
            0,
            value,
            None,  # ensure_minimum_charging_power
        )

        if not ok:
            # Shared rollback preserves newer requests and fresh observations.
            _LOGGER.warning(
                "set_charge_mode failed for %s (power=%s), reverting optimistic value",
                self.sn,
                value,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_charge_power_failed",
                translation_placeholders={"value": str(value)},
            )

        # 3) Schedule a delayed refresh to confirm state from the API.
        # set-mode can take up to 90s to return, then device needs more time to apply.
        # Poll 60s after set-mode returns (total from user action up to ~150s).
        self.coordinator.schedule_delayed_refresh(60)


class SemsOutputPowerLimitNumber(CoordinatorEntity, NumberEntity):
    """Cloud entity for the global output power limit (kW) via set-config.

    Part of the Dynamic Load Management feature. The API field is
    ``ratedMaxiChargePower`` in both the detail response and the set-config call.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "output_power_limit"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_native_min_value = 1.4
    _attr_native_step = 0.1
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "slider"

    _PENDING_TIMEOUT = 60.0

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        self.api = api
        self._pending_value: float | None = None
        self._pending_until: float = 0.0

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-number-output-power-limit"

    @property
    def device_info(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def native_max_value(self) -> float:
        data = self.coordinator.data.get(self.sn, {}) or {}
        for key in ("hw_max_charge_power", "max_charge_power", "rated_max_charge_power"):
            v = data.get(key)
            try:
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                pass
        return 22.0

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        api_raw = data.get("rated_max_charge_power")
        try:
            api_val = float(api_raw) if api_raw is not None else None
        except (TypeError, ValueError):
            api_val = None
        now = time.monotonic()
        if self._pending_value is not None:
            if now >= self._pending_until:
                self._pending_value = None
            elif api_val is not None and abs(api_val - self._pending_value) < 0.5:
                self._pending_value = None
            else:
                return self._pending_value
        return api_val

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @mode_setting_write
    @optimistic_write
    async def async_set_native_value(self, value: float) -> None:
        self._pending_value = value
        self._pending_until = time.monotonic() + self._PENDING_TIMEOUT
        self.async_write_ha_state()
        ok = await async_execute(self.hass,
            lambda: self.api.set_config_gen2(self.sn, ratedMaxiChargePower=round(value, 1))
        )
        if not ok:
            _LOGGER.warning("SemsOutputPowerLimitNumber %s: set_config failed", self.sn)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(5.0)


class SemsCurrentLimitNumber(CoordinatorEntity, NumberEntity):
    """Expose the household import-current limit using SEMS+ device bounds."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "current_limit"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = 0.01
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "box"

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        self.api = api

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-number-current-limit"

    @property
    def device_info(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and current_writable(self.native_value, self.range_metadata)

    @property
    def native_value(self) -> float | None:
        return observed_current((self.coordinator.data.get(self.sn) or {}).get("currentLimit"))

    @property
    def range_metadata(self):
        return (self.coordinator.data.get(self.sn) or {}).get("controlItemRanges")

    @property
    def native_min_value(self):
        try:
            return current_bounds(self.range_metadata)[0]
        except ValueError:
            return 0.0  # HA requires numeric capabilities even while unavailable.

    @property
    def native_max_value(self):
        try:
            return current_bounds(self.range_metadata)[1]
        except ValueError:
            return 2000.0

    @property
    def extra_state_attributes(self):
        return current_attributes(self.native_value, self.range_metadata)

    async def async_update(self):
        """Refresh metadata on explicit user update without changing device settings."""
        info = await async_execute(self.hass, self.api.fetch_device_info, self.sn)
        if isinstance(info, dict) and info and info.get("sn", self.sn) == self.sn:
            self.coordinator.data[self.sn]["controlItemRanges"] = info.get("controlItemRanges")
        await super().async_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @mode_setting_write
    async def async_set_native_value(self, value: float) -> None:
        try:
            value = validate_current_write(self.native_value, value, self.range_metadata)
            if not self.available:
                raise ConnectionError("Cloud configuration is unavailable")
            # Recheck metadata before the device read; neither a stale UI range
            # nor an unsuccessful metadata request may authorize a write.
            info = await async_execute(self.hass, self.api.fetch_device_info, self.sn)
            if not isinstance(info, dict) or not info or info.get("sn", self.sn) != self.sn:
                raise ValueError("Missing or mismatched cloud configuration")
            fresh = await async_execute(self.hass, self.api.get_data_gen2, self.sn)
            if not isinstance(fresh, dict) or fresh.get("sn") != self.sn:
                raise ValueError("Missing or mismatched cloud configuration")
            self.coordinator.data[self.sn]["controlItemRanges"] = info.get("controlItemRanges")
            value = validate_current_write(fresh.get("currentLimit"), value, info.get("controlItemRanges"))
            ok = await async_execute(self.hass,
                lambda: self.api.set_config_gen2(self.sn, currentLimit=value)
            )
            if not ok:
                raise RuntimeError("Device write was not confirmed")
        except (ValueError, OSError, RuntimeError) as error:
            raise operation_error(error) from error
        finally:
            # Keep observed state; an ACK is not a confirmed device setting.
            self.coordinator.schedule_delayed_refresh(5.0)


# ---------------------------------------------------------------------------
# Cloud entities for per-mode numeric params (maxEnergy, minEnergy, soc target)
# ---------------------------------------------------------------------------

class _SemsModeParamNumber(CoordinatorEntity, NumberEntity):
    """Base class for cloud number entities that send their value via set-mode.

    Subclasses must define:
      _attr_translation_key, unique_id, _available_modes, _data_key, _override_kwarg.
    ``_override_kwarg`` must match a kwarg name accepted by set_charge_mode_gen2
    (max_energy | min_energy | soc_target).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "slider"

    _PENDING_TIMEOUT = 60.0
    _available_modes: tuple = (0, 2)  # override in subclass
    _data_key: str = ""
    _override_kwarg: str = ""

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        self.api = api
        self._pending_value: float | None = None
        self._pending_until: float = 0.0

    @property
    def device_info(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in self._available_modes

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        api_raw = data.get(self._data_key)
        try:
            api_val = float(api_raw) if api_raw is not None else None
        except (TypeError, ValueError):
            api_val = None
        now = time.monotonic()
        if self._pending_value is not None:
            if now >= self._pending_until:
                self._pending_value = None
            elif api_val is not None and abs(api_val - self._pending_value) < 0.5:
                self._pending_value = None
            else:
                return self._pending_value
        return api_val

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @mode_setting_write
    @optimistic_write
    async def async_set_native_value(self, value: float) -> None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        mode = data.get("chargeMode", 0)
        # Build full current-param kwargs so the API call preserves other settings.
        # chargeMaxPower is only relevant for mode 0.
        charge_power = data.get("set_charge_power") if mode == 0 else None
        from .mode_parameters import preserved_mode_parameters
        from .ui_errors import operation_error

        try:
            kwargs = preserved_mode_parameters(data, mode)
            if mode == 0 and charge_power is None:
                raise ValueError("Cannot preserve unreported charging power")
        except (ValueError, RuntimeError) as error:
            raise operation_error(error) from error
        kwargs[self._override_kwarg] = int(value)

        self._pending_value = value
        self._pending_until = time.monotonic() + self._PENDING_TIMEOUT
        self.async_write_ha_state()

        ok = await async_execute(self.hass,
            lambda: self.api.set_charge_mode_gen2(
                self.sn, mode, charge_power, None, **kwargs
            )
        )
        if not ok:
            _LOGGER.warning("%s: set_charge_mode failed, reverting pending value", self.unique_id)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(5.0)


class SemsMaxEnergyNumber(_SemsModeParamNumber):
    """Max session energy (kWh). 0 = unlimited. Available in Fast (0) and PV+BAT (2) modes."""

    _attr_translation_key = "max_session_energy"
    _attr_device_class = NumberDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 0.0
    _attr_native_max_value = 200.0
    _attr_native_step = 1.0
    _available_modes = (0, 1, 2)
    _data_key = "max_energy"
    _override_kwarg = "max_energy"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-number-max-energy"


class SemsTargetSocNumber(_SemsModeParamNumber):
    """Target SOC % — stop charging when battery reaches this level.
    0 = no SOC stop condition. Available in Fast (0) and PV+BAT (2) modes."""

    _attr_translation_key = "charge_target_soc"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _available_modes = (0, 2)
    _data_key = "charge_target_soc"
    _override_kwarg = "soc_target"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-number-target-soc"


class SemsMinEnergyNumber(_SemsModeParamNumber):
    """Min guaranteed energy (kWh) in PV+BAT mode.
    0 = no minimum. Only available in PV+BAT mode (2)."""

    _attr_translation_key = "min_session_energy"
    _attr_device_class = NumberDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 0.0
    _attr_native_max_value = 200.0
    _attr_native_step = 1.0
    _available_modes = (1, 2)
    _data_key = "min_energy"
    _override_kwarg = "min_energy"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-number-min-energy"


# ---------------------------------------------------------------------------
# Modbus-specific number entities (local Modbus TCP mode only)
# ---------------------------------------------------------------------------

class _ModbusNumber(CoordinatorEntity, NumberEntity):
    """Base class for Modbus-backed numeric controls."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_mode = "box"

    def __init__(self, coordinator, sn: str, client) -> None:
        super().__init__(coordinator)
        self.sn = sn
        self._client = client
        self._pending_value: float | None = None
        self._pending_until: float = 0.0

    @property
    def device_info(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _api_value(self) -> float | None:
        raise NotImplementedError

    def _do_write(self, value: float) -> bool:
        raise NotImplementedError

    @property
    def native_value(self) -> float | None:
        policy = getattr(self.coordinator, "charge_mode_policy", None)
        if (getattr(self, "_remember_charge_power", False) and policy is not None
                and policy.desired_power is not None):
            return policy.desired_power
        api_val = self._api_value()
        now = time.monotonic()
        if self._pending_value is not None:
            if now >= self._pending_until:
                self._pending_value = None
            elif api_val is not None and abs(api_val - self._pending_value) < 0.05:
                self._pending_value = None
            else:
                return self._pending_value
        return api_val

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @mode_setting_write
    @optimistic_write
    async def async_set_native_value(self, value: float) -> None:
        self._pending_value = value
        self._pending_until = time.monotonic() + 30.0
        self.async_write_ha_state()
        ok = await async_execute(self.hass, self._do_write, value)
        if not ok:
            _LOGGER.warning("%s: write failed, reverting optimistic value", self.unique_id)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(3.0)


class ModbusMaxChargePowerNumber(_ModbusNumber):
    """Max charge power limit in kW (reg 10029, SF=10)."""

    _remember_charge_power = True
    _attr_translation_key = "modbus_charge_power"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_native_step = 0.1
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "slider"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_max_charge_power"

    def _power_limits(self) -> tuple[float, float]:
        data = self.coordinator.data.get(self.sn, {}) or {}
        spec = data.get("modbus_power_spec")
        if spec == 2:
            return 4.2, 22.0
        if spec == 1:
            return 4.2, 11.0
        return 1.4, 7.0

    def _min_power_from_voltage(self) -> float | None:
        """Calculate minimum power from live voltage readings at 6 A per phase.

        Returns None if voltages are not yet available (e.g. first poll).
        Rounds up to the nearest 0.1 kW so we never go below the true minimum.
        """
        data = self.coordinator.data.get(self.sn, {}) or {}
        ua = data.get("modbus_voltage_a")
        if ua is None or ua < 100:  # implausible / not yet read
            return None
        # single-phase: only phase A matters
        if data.get("modbus_pile_type") == 1:
            min_kw = ua * 6 / 1000.0
        else:
            ub = data.get("modbus_voltage_b") or ua
            uc = data.get("modbus_voltage_c") or ua
            min_kw = (ua + ub + uc) * 6 / 1000.0
        # round up to nearest 0.1 kW
        return round(min_kw + 0.049, 1)

    @property
    def native_min_value(self) -> float:
        v = self._min_power_from_voltage()
        return v if v is not None else self._power_limits()[0]

    @property
    def native_max_value(self) -> float:
        return self._power_limits()[1]

    def _api_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_max_charging_power")
        if v is None:
            return None
        # A reported zero means no software limit; a missing register does not.
        if v == 0.0:
            return self.native_max_value
        return round(float(v), 1)

    def _do_write(self, value: float) -> bool:
        return self._client.write_max_charge_power(value)

    @property
    def extra_state_attributes(self):
        """Expose the actual register separately from saved Fast-mode intent."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {"reported_power_limit": data.get("set_charge_power")}

    @prepare_fast_power
    async def async_set_native_value(self, value: float) -> None:
        """Stage PV-mode requests; write immediately only in Fast mode."""
        await super().async_set_native_value(value)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in (0, 1, 2)


class ModbusMaxChargeCapacityNumber(_ModbusNumber):
    """Max session energy limit in kWh (reg 10027, SF=10). 0 = unlimited."""

    _attr_translation_key = "modbus_max_capacity"
    _attr_device_class = NumberDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 0.0
    _attr_native_max_value = 200.0
    _attr_native_step = 0.1
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_max_capacity"

    def _api_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_max_capacity")
        return round(float(v), 1) if v is not None else None

    def _do_write(self, value: float) -> bool:
        return self._client.write_max_charge_capacity(value)


class ModbusMinChargeCapacityNumber(_ModbusNumber):
    """Min session energy target in kWh (reg 10028, SF=10). 0 = no minimum."""

    _attr_translation_key = "modbus_min_capacity"
    _attr_device_class = NumberDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 0.0
    _attr_native_max_value = 200.0
    _attr_native_step = 0.1
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_min_capacity"

    def _api_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_min_capacity")
        return round(float(v), 1) if v is not None else None

    def _do_write(self, value: float) -> bool:
        return self._client.write_min_charge_capacity(value)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in (1, 2)  # PV and PV+battery modes


class ModbusBatteryDischargeSocNumber(_ModbusNumber):
    """Battery discharge SOC threshold in % (reg 10030). Used in PV+battery mode."""

    _attr_translation_key = "modbus_bat_soc_limit"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_bat_soc_limit"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in (0, 2)  # Fast and PV+battery modes

    def _api_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_bat_soc_limit")
        return float(v) if v is not None else None

    def _do_write(self, value: float) -> bool:
        return self._client.write_battery_discharge_soc(int(value))


class ModbusCurrentLimitNumber(_ModbusNumber):
    """Household breaker current (reg 10026), not the EV charging current."""

    _attr_translation_key = "current_limit"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = float(BREAKER_CURRENT_MIN)
    _attr_native_max_value = float(BREAKER_CURRENT_MAX)
    _attr_native_step = 1.0
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "box"

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_current_limit"

    def _api_value(self) -> float | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_breaker_current")
        return float(v) if v is not None else None

    def _do_write(self, value: float) -> bool:
        return self._client.write_breaker_current(value)
