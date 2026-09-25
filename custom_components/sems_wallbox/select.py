"""Support for select entity controlling GoodWe SEMS Wallbox charge mode."""

from .write_confirmation import confirm_write
from .optimistic_write import optimistic_write

import logging
import time

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud_rate_limit import CloudRateLimitedError
from .operation_budget import async_execute
from .const import DOMAIN, CONN_TYPE_MODBUS
from .charge_mode_policy import async_apply_policy, mode_setting_write
from .coordinator import SemsUpdateCoordinator
from .ui_errors import operation_error

_LOGGER = logging.getLogger(__name__)

# After requesting a mode change, ignore coordinator poll results that contradict
# the pending mode for up to this many seconds (API can take ~10-15 s to apply).
_PENDING_MODE_TIMEOUT = 60.0

_MODE_TO_OPTION: dict[int, str] = {
    0: "fast",
    1: "pv_priority",
    2: "pv_and_battery",
}

_OPTION_TO_MODE: dict[str, int] = {value: key for key, value in _MODE_TO_OPTION.items()}

OPERATION_MODE = SelectEntityDescription(
    key="charge_mode",
    entity_category=None,
    translation_key="charge_mode",
)

_DURATION_OPTIONS = ["asap", "1h", "2h", "3h", "4h", "5h", "6h"]
_DURATION_TO_HOURS: dict[str, int] = {opt: i for i, opt in enumerate(_DURATION_OPTIONS)}
_HOURS_TO_DURATION: dict[int, str] = {i: opt for i, opt in enumerate(_DURATION_OPTIONS)}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the inverter select entities from a config entry."""
    runtime = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = runtime["coordinator"]
    conn_type = runtime.get("connection_type", "cloud")
    if conn_type == "native_tcp":
        from .native_entities import setup_platform
        setup_platform("select", runtime["coordinator"], async_add_entities)
        return

    if conn_type == CONN_TYPE_MODBUS:
        client = runtime["modbus_client"]
        entities = []
        for sn in coordinator.data:
            entities.append(ModbusChargeModeSelect(coordinator, sn, client))
            entities.append(ModbusChargeDurationSelect(coordinator, sn, client))
        async_add_entities(entities)
        return

    api = runtime["api"]

    entities: list[InverterOperationModeEntity] = []

    for sn, inverter in coordinator.data.items():
        active_mode = inverter["chargeMode"]
        entities.append(
            InverterOperationModeEntity(
                coordinator,
                api,
                sn,
                OPERATION_MODE,
                list(_MODE_TO_OPTION.values()),
                _MODE_TO_OPTION.get(active_mode),
            )
        )
        entities.append(SemsChargeDurationSelect(coordinator, sn, api))

    async_add_entities(entities)


class InverterOperationModeEntity(CoordinatorEntity, SelectEntity):
    """Entity representing the wallbox charge mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    @property
    def extra_state_attributes(self):
        """Expose saved intent separately from the displayed device mode."""
        policy = getattr(self.coordinator, "charge_mode_policy", None)
        if policy is None or not policy.enabled:
            return {}
        return {"preferred_mode": _MODE_TO_OPTION.get(policy.desired_mode)}

    def __init__(
        self,
        coordinator: SemsUpdateCoordinator,
        api,
        sn: str,
        description: SelectEntityDescription,
        supported_options: list[str],
        current_mode: str,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.api = api
        self.sn = sn
        self.entity_description = description
        self._attr_unique_id = f"{self.sn}-select-charge-mode"
        self._attr_options = supported_options
        self._attr_current_option = str(current_mode)
        # Pending mode: set while we wait for the API to confirm a mode change.
        # Prevents regular polls from reverting the optimistic UI state.
        self._pending_mode: int | None = None
        self._pending_mode_set_at: float = 0.0
        _LOGGER.debug("Creating SelectEntity for Wallbox %s", self.sn)

    @property
    def device_info(self):
        """Return device info."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    @confirm_write("chargeMode", value=lambda entity, value: _OPTION_TO_MODE.get(value))
    @optimistic_write
    async def async_select_option(self, option: str) -> None:
        """Change mode and translate cooldowns from either cloud write."""
        try:
            await self._async_select_option(option)
        except CloudRateLimitedError as error:
            raise operation_error(error) from error

    async def _async_select_option(self, option: str) -> None:
        """Apply a mode choice through the policy or legacy cloud path."""
        if option not in _OPTION_TO_MODE:
            _LOGGER.warning(
                "Unknown operation mode option %s for wallbox %s",
                option,
                self.sn,
            )
            return

        mode = _OPTION_TO_MODE[option]
        if await async_apply_policy(self.coordinator, "select_mode", mode):
            self._pending_mode = None
            await self.coordinator.async_request_refresh()
            return

        _LOGGER.debug(
            "Setting operation mode for wallbox %s to %s (mode=%s)",
            self.sn,
            option,
            mode,
        )

        # Optimistic UI update for select entity
        self._attr_current_option = option
        self.async_write_ha_state()

        # When switching TO Fast mode (0) the API requires charge_power in the
        # payload, otherwise it silently ignores the command.
        # For PV modes (1, 2) we must NOT send charge_power -- doing so causes
        # the API to revert back to Fast mode.
        charge_power = None
        if mode == 0:
            data = self.coordinator.data.get(self.sn, {}) or {}
            raw = data.get("set_charge_power")
            try:
                cp = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                cp = None

            # Clamp to valid range; fall back to min if unknown/invalid
            _min = 4.2
            _max = 11.0
            try:
                _min = float(data.get("min_charge_power") or _min)
                _max = float(data.get("max_charge_power") or _max)
            except (TypeError, ValueError):
                pass

            if cp is None or not (_min <= cp <= _max):
                cp = _min
            charge_power = cp

        # Only this entity presents the pending mode. Shared coordinator data
        # remains the last report, so failures cannot turn intent into telemetry.
        write = self._optimistic_write
        write.capture_device()
        self._pending_mode = mode
        self._pending_mode_set_at = time.monotonic()

        ok = await async_execute(self.hass,
            self.api.set_charge_mode_gen2,
            self.sn,
            mode,
            charge_power,
        )

        if not ok:
            # Shared rollback preserves newer requests and fresh observations.
            _LOGGER.warning(
                "set_charge_mode failed for %s (mode=%s), reverting optimistic UI state",
                self.sn,
                mode,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"set_charge_mode_failed_{option}",
            )

        # Ignore completion of an older request, or a genuinely different mode
        # reported meanwhile. An unchanged old mode is normal cloud latency.
        current_device_supersede = self.coordinator.data.get(self.sn, {}) or {}
        current_chargemode = current_device_supersede.get("chargeMode")
        if (self._optimistic_write is not write
                or (self._pending_mode != mode and current_chargemode != mode)
                or current_chargemode not in (mode, write.before.get("chargeMode"))):
            _LOGGER.debug(
                "Mode call for %s (mode=%s) superseded (pending=%s, current chargeMode=%s), discarding result",
                self.sn,
                mode,
                self._pending_mode,
                current_chargemode,
            )
            return

        # Race-condition guard for Fast mode: if the user moved the power slider
        # while this API call was in flight, number.py will have written the new
        # value into coordinator.data optimistically.  Re-send with the latest
        # power so the SEMS API ends up with the value the user actually wants
        # (last write wins -- both calls use the same set_charge_mode endpoint).
        if mode == 0:
            current_data = self.coordinator.data.get(self.sn, {}) or {}
            latest_raw = current_data.get("set_charge_power")
            try:
                latest_power = float(latest_raw) if latest_raw is not None else None
            except (TypeError, ValueError):
                latest_power = None
            if (latest_power is not None and latest_power != charge_power
                    and latest_raw != write.before.get("set_charge_power")
                    and _min <= latest_power <= _max):
                _LOGGER.debug(
                    "Power changed during mode switch for %s (%.2f → %.2f kW), re-sending",
                    self.sn,
                    charge_power,
                    latest_power,
                )
                await async_execute(self.hass,
                    self.api.set_charge_mode_gen2,
                    self.sn,
                    0,
                    latest_power,
                )

        # Schedule a delayed refresh (5 s) to confirm state from the API.
        self.coordinator.schedule_delayed_refresh(5)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        inverter = self.coordinator.data.get(self.sn, {}) or {}
        mode = inverter.get("chargeMode")
        _LOGGER.debug(
            "Coordinator update for wallbox %s: chargeMode=%s (pending=%s)",
            self.sn,
            mode,
            self._pending_mode,
        )

        if self._pending_mode is not None:
            # Safety valve: give up waiting after timeout
            if time.monotonic() - self._pending_mode_set_at > _PENDING_MODE_TIMEOUT:
                _LOGGER.warning(
                    "Pending mode %s for wallbox %s timed out, accepting chargeMode=%s from API",
                    self._pending_mode,
                    self.sn,
                    mode,
                )
                self._pending_mode = None
            elif mode == self._pending_mode:
                # API confirmed the change -- stop guarding
                _LOGGER.debug(
                    "Pending mode %s confirmed by API for wallbox %s",
                    self._pending_mode,
                    self.sn,
                )
                self._pending_mode = None
            else:
                # Keep optimistic presentation local to this entity. A pending
                # choice must never overwrite an authoritative poll result.
                self.async_write_ha_state()
                return

        if mode in _MODE_TO_OPTION:
            self._attr_current_option = _MODE_TO_OPTION[mode]
        else:
            _LOGGER.warning(
                "Unknown chargeMode %s for wallbox %s in coordinator update",
                mode,
                self.sn,
            )

        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Trigger coordinator refresh when entity is updated."""
        await self.coordinator.async_request_refresh()


# ---------------------------------------------------------------------------
# Cloud (SEMS) charge duration select — finish time for PV / PV+battery modes
# ---------------------------------------------------------------------------

_SEMS_PENDING_DURATION_TIMEOUT = 30.0


class SemsChargeDurationSelect(CoordinatorEntity, SelectEntity):
    """Cloud charge duration (finish time) select for PV priority and PV+battery modes.

    Maps to the ``finishTime`` field in the set-mode API call.
    0 = ASAP (no deadline), 1-6 = target hours to complete charging.
    Available only when chargeMode is 1 (PV priority) or 2 (PV+battery).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "charge_duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = _DURATION_OPTIONS

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        self.api = api
        self._pending_value: str | None = None
        self._pending_until: float = 0.0

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-select-charge-duration"

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
        return data.get("chargeMode") in (1, 2)

    @property
    def current_option(self) -> str | None:
        now = time.monotonic()
        if self._pending_value is not None:
            if now >= self._pending_until:
                self._pending_value = None
            else:
                data = self.coordinator.data.get(self.sn, {}) or {}
                raw = data.get("finish_time")
                if raw is not None:
                    try:
                        if _HOURS_TO_DURATION.get(int(raw)) == self._pending_value:
                            self._pending_value = None
                    except (TypeError, ValueError):
                        pass
                if self._pending_value is not None:
                    return self._pending_value

        data = self.coordinator.data.get(self.sn, {}) or {}
        raw = data.get("finish_time")
        if raw is None:
            return None
        try:
            return _HOURS_TO_DURATION.get(int(raw))
        except (TypeError, ValueError):
            return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @confirm_write("finish_time", value=lambda entity, value: _DURATION_TO_HOURS.get(value))
    @mode_setting_write
    @optimistic_write
    async def async_select_option(self, option: str) -> None:
        if option not in _DURATION_TO_HOURS:
            _LOGGER.warning("SemsChargeDurationSelect: unknown option %r", option)
            return
        hours = _DURATION_TO_HOURS[option]
        data = self.coordinator.data.get(self.sn, {}) or {}
        mode = data.get("chargeMode", 1)
        # Re-send full mode params so existing settings are preserved
        charge_power = data.get("set_charge_power") if mode == 0 else None
        from .mode_parameters import preserved_mode_parameters
        from .ui_errors import operation_error

        try:
            params = preserved_mode_parameters(data, mode)
        except (ValueError, RuntimeError) as error:
            raise operation_error(error) from error
        params["finish_time"] = str(hours)

        self._pending_value = option
        self._pending_until = time.monotonic() + _SEMS_PENDING_DURATION_TIMEOUT
        self.async_write_ha_state()

        ok = await async_execute(self.hass,
            lambda: self.api.set_charge_mode_gen2(
                self.sn, mode, charge_power, None,
                **params,
            )
        )
        if not ok:
            _LOGGER.warning("SemsChargeDurationSelect %s: set_charge_mode_gen2 failed", self.sn)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(5.0)


# ---------------------------------------------------------------------------
# Modbus-specific select entity (local Modbus TCP mode only)
# ---------------------------------------------------------------------------


class ModbusChargeModeSelect(CoordinatorEntity, SelectEntity):
    """Charge mode selector via Modbus (reg 10032: 0=fast, 1=PV, 2=PV+battery)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_charge_mode"
    _attr_entity_category = None

    _ALL_OPTIONS = list(_MODE_TO_OPTION.values())
    _FAST_ONLY = ["fast"]
    _PENDING_TIMEOUT = 30.0

    @property
    def extra_state_attributes(self):
        """Expose saved intent separately from the displayed device mode."""
        policy = getattr(self.coordinator, "charge_mode_policy", None)
        if policy is None or not policy.enabled:
            return {}
        return {"preferred_mode": _MODE_TO_OPTION.get(policy.desired_mode)}

    def __init__(self, coordinator, sn: str, client) -> None:
        super().__init__(coordinator)
        self.sn = sn
        self._client = client
        self._pending_mode: int | None = None
        self._pending_set_at: float = 0.0

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_charge_mode"

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
    def options(self) -> list[str]:
        """Return available options -- PV modes only when inverter is connected (bit 2)."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        comm_status = data.get("modbus_comm_status", 0) or 0
        if comm_status & 0x04:  # bit 2: inverter connected
            return self._ALL_OPTIONS
        return self._FAST_ONLY

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        mode = data.get("chargeMode")
        if self._pending_mode is not None:
            if time.monotonic() - self._pending_set_at >= self._PENDING_TIMEOUT:
                self._pending_mode = None
            elif mode == self._pending_mode:
                self._pending_mode = None
            else:
                return _MODE_TO_OPTION.get(self._pending_mode)
        return _MODE_TO_OPTION.get(mode)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @confirm_write("chargeMode", value=lambda entity, value: _OPTION_TO_MODE.get(value))
    @optimistic_write
    async def async_select_option(self, option: str) -> None:
        if option not in _OPTION_TO_MODE:
            _LOGGER.warning("Unknown charge mode option: %s", option)
            return
        mode = _OPTION_TO_MODE[option]
        if await async_apply_policy(self.coordinator, "select_mode", mode):
            self._pending_mode = None
            await self.coordinator.async_request_refresh()
            return
        self._pending_mode = mode
        self._pending_set_at = time.monotonic()
        self.async_write_ha_state()
        ok = await async_execute(self.hass, self._client.write_charge_mode, mode)
        if not ok:
            _LOGGER.warning("ModbusChargeModeSelect %s: write failed, reverting", self.sn)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(3.0)


# ---------------------------------------------------------------------------
# Modbus charge duration (completion time) select — reg 10031
# ---------------------------------------------------------------------------


class ModbusChargeDurationSelect(CoordinatorEntity, SelectEntity):
    """Completion time selector via Modbus (reg 10031: 0=ASAP, 1-6=hours).

    Active only in PV priority (mode 1) and PV+battery (mode 2).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_charge_duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = _DURATION_OPTIONS
    _PENDING_TIMEOUT = 30.0

    def __init__(self, coordinator, sn: str, client) -> None:
        super().__init__(coordinator)
        self.sn = sn
        self._client = client
        self._pending_value: str | None = None
        self._pending_until: float = 0.0

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_charge_duration"

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
        return data.get("chargeMode") in (1, 2)  # PV priority and PV+battery

    @property
    def current_option(self) -> str | None:
        now = time.monotonic()
        if self._pending_value is not None:
            if now >= self._pending_until:
                self._pending_value = None
            else:
                data = self.coordinator.data.get(self.sn, {}) or {}
                api_raw = data.get("modbus_completion_time")
                if api_raw is not None and _HOURS_TO_DURATION.get(int(api_raw)) == self._pending_value:
                    self._pending_value = None
                else:
                    return self._pending_value
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("modbus_completion_time")
        if v is None:
            return None
        return _HOURS_TO_DURATION.get(int(v))

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @confirm_write("modbus_completion_time", value=lambda entity, value: _DURATION_TO_HOURS.get(value))
    @mode_setting_write
    @optimistic_write
    async def async_select_option(self, option: str) -> None:
        if option not in _DURATION_TO_HOURS:
            _LOGGER.warning("Unknown charge duration option: %s", option)
            return
        hours = _DURATION_TO_HOURS[option]
        self._pending_value = option
        self._pending_until = time.monotonic() + self._PENDING_TIMEOUT
        self.async_write_ha_state()
        ok = await async_execute(self.hass, self._client.write_completion_time, hours)
        if not ok:
            _LOGGER.warning("ModbusChargeDurationSelect %s: write failed, reverting", self.sn)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(3.0)
