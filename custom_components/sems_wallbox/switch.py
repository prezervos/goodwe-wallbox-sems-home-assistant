"""Support for switch controlling an output of a GoodWe SEMS wallbox."""

from __future__ import annotations

import logging
import time

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .operation_budget import async_execute
from .const import (
    DOMAIN,
    CONN_TYPE_MODBUS,
    CAP_PLUG_AND_CHARGE,
    CAP_DYNAMIC_LOAD_CONTROL,
    CAP_PHASE_SWITCH,
    CAP_ENSURE_MIN_CHARGING_POWER,
)
from .charge_mode_policy import async_apply_policy, mode_setting_write
from .coordinator import SemsUpdateCoordinator
from .minimum_power import write_minimum_power
from .ui_errors import operation_error

_LOGGER = logging.getLogger(__name__)


# How long after an ON command to ignore "Waiting/power=0" and keep optimistic ON (seconds)
GRACE_ON_SECONDS = 130

# How long after an OFF command to tolerate API still briefly showing power>0
GRACE_OFF_SECONDS = 130


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add switches for passed config_entry in HA."""
    runtime = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = runtime["coordinator"]
    conn_type = runtime.get("connection_type", "cloud")
    if conn_type == "native_tcp":
        from .native_entities import setup_platform
        setup_platform("switch", runtime["coordinator"], async_add_entities)
        return

    if conn_type == CONN_TYPE_MODBUS:
        client = runtime["modbus_client"]
        entities = []
        for sn in coordinator.data:
            entities.append(ModbusStartStopSwitch(coordinator, sn, client))
            entities.append(ModbusMaintainMinPowerSwitch(coordinator, sn, client))
            entities.append(ModbusPlugChargeSwitch(coordinator, sn, client))
            entities.append(ModbusDynamicLoadMgmtSwitch(coordinator, sn, client))
            entities.append(ModbusEmsDispatchSwitch(coordinator, sn, client))
            entities.append(ModbusPhaseSwitchSwitch(coordinator, sn, client))
        async_add_entities(entities)
        return

    api = runtime["api"]
    caps = runtime.get("capabilities", {})
    dashboard = caps.get("dashboard_functions", [])
    more_controls = caps.get("more_device_controls", [])

    _LOGGER.debug(
        "Setting up SemsSwitch entities for entry %s",

        config_entry.entry_id,
    )

    entities: list[SemsSwitch] = []
    minimum_registered = set()
    for sn, data in coordinator.data.items():
        start_status = data.get("startStatus")
        current_is_on = bool(start_status) if start_status is not None else False
        entities.append(SemsSwitch(coordinator, sn, api, current_is_on))
        # Retain explicit/legacy capabilities and accept an actual boolean report
        # when the device omits this feature from its capability list.
        if (not more_controls or CAP_ENSURE_MIN_CHARGING_POWER in more_controls
                or (coordinator.last_update_success
                    and type(data.get("ensure_minimum_charging_power")) is bool)):
            entities.append(SemsMinimumPowerSwitch(
                coordinator, sn, api, generation=caps.get("pile_generation")))
            minimum_registered.add(sn)
        if CAP_PLUG_AND_CHARGE in dashboard:
            entities.append(SemsPlugAndChargeSwitch(coordinator, sn, api))
        if CAP_DYNAMIC_LOAD_CONTROL in more_controls:
            entities.append(SemsDynamicLoadSwitch(coordinator, sn, api))
        if CAP_PHASE_SWITCH in more_controls:
            entities.append(SemsPhaseSwitchSwitch(coordinator, sn, api))

    async_add_entities(entities)

    @callback
    def add_reported_minimum_power():
        if not coordinator.last_update_success:
            return
        additions = []
        for sn, data in coordinator.data.items():
            if (sn not in minimum_registered
                    and type(data.get("ensure_minimum_charging_power")) is bool):
                additions.append(SemsMinimumPowerSwitch(
                    coordinator, sn, api, generation=caps.get("pile_generation")))
                minimum_registered.add(sn)
        if additions:
            async_add_entities(additions)

    config_entry.async_on_unload(
        coordinator.async_add_listener(add_reported_minimum_power))


class SemsSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to start/stop charging."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "start_charging"

    def __init__(
        self,
        coordinator: SemsUpdateCoordinator,
        sn: str,
        api,
        current_is_on: bool,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.api = api
        self.sn = sn
        self._attr_is_on = current_is_on

        # Grace period tracking
        self._last_command_ts: float | None = None
        self._last_command_target: bool | None = None

        _LOGGER.debug(
            "Creating SemsSwitch for Wallbox %s, initial is_on=%s",

            self.sn,
            self._attr_is_on,
        )

    @property
    def device_class(self):
        """Return the device class."""
        return SwitchDeviceClass.SWITCH

    @property
    def unique_id(self) -> str:
        """Return unique id."""
        return f"{self.coordinator.data[self.sn]['sn']}-switch-start-charging"

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": (self.coordinator.data.get(self.sn, {}) or {}).get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    def _compute_is_on_from_data(self, data: dict) -> bool:
        """Compute is_on from API data, respecting the grace period after commands."""
        # Primary signal: workStu=6 from getLastCharge (merged into coordinator data).
        # startStatus in /detail is unreliable -- always False in PV mode even when charging.
        work_status = data.get("last_charge_work_status")
        if work_status is not None:
            api_is_on = work_status == 6
        else:
            # Fallback: startStatus for Gen1 or when getLastCharge failed
            start_status = data.get("startStatus")
            if start_status is not None:
                api_is_on = bool(start_status)
            else:
                status = data.get("status")
                api_is_on = status == "EVDetail_Status_Title_Charging"
        status = data.get("status")

        now = self.hass.loop.time()
        target = self._last_command_target
        ts = self._last_command_ts

        # Within ON grace: keep optimistic ON even if API still shows not charging
        if (
            target is True
            and ts is not None
            and now - ts < GRACE_ON_SECONDS
            and not api_is_on
        ):
            _LOGGER.debug(
                "SemsSwitch %s: within ON grace (%.1fs < %.1fs), "
                "API status=%s, startStatus=%s -> holding is_on=True",
                self.sn,
                now - ts,
                GRACE_ON_SECONDS,
                status,
                data.get("startStatus"),
            )
            return True

        # Within OFF grace: keep optimistic OFF even if API briefly shows charging
        if (
            target is False
            and ts is not None
            and now - ts < GRACE_OFF_SECONDS
            and api_is_on
        ):
            _LOGGER.debug(
                "SemsSwitch %s: within OFF grace (%.1fs < %.1fs), "
                "API status=%s, startStatus=%s -> holding is_on=False",
                self.sn,
                now - ts,
                GRACE_OFF_SECONDS,
                status,
                data.get("startStatus"),
            )
            return False

        # Outside grace period or state already matches command
        if target is not None and api_is_on == target:
            self._last_command_target = None
            self._last_command_ts = None

        _LOGGER.debug(
            "SemsSwitch %s: API status=%s, startStatus=%s -> is_on=%s (no grace override)",
            self.sn,
            status,
            data.get("startStatus"),
            api_is_on,
        )
        return api_is_on

    async def async_turn_off(self, **kwargs):
        """Stop charging and expose a rejected command to HA callers."""
        if not await async_apply_policy(self.coordinator, "stop"):
            await self._async_command(False)

    async def async_turn_on(self, **kwargs):
        """Start once; do not replay a command with an uncertain outcome."""
        if not await async_apply_policy(self.coordinator, "start"):
            await self._async_command(True)

    async def _async_command(self, enabled: bool) -> None:
        """Keep optimistic state only while an accepted command is pending."""
        from .ui_errors import operation_error

        action = "start" if enabled else "stop"
        self._last_command_target = enabled
        self._last_command_ts = self.hass.loop.time()
        self._attr_is_on = enabled
        self.async_write_ha_state()
        try:
            accepted = await async_execute(self.hass,
                self.api.change_status_gen2, self.sn, action
            )
            if accepted is not True:
                raise ValueError(
                    "Start was not acknowledged; it was not retried" if enabled
                    else "Stop was not acknowledged; check actual device state"
                )
        except (OSError, ValueError, RuntimeError) as error:
            self._last_command_target = None
            self._last_command_ts = None
            self._attr_is_on = self._compute_is_on_from_data(
                self.coordinator.data.get(self.sn, {}) or {}
            )
            self.async_write_ha_state()
            raise operation_error(error) from error
        finally:
            self.coordinator.schedule_delayed_refresh(5)

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        _LOGGER.debug("SemsSwitch added to hass for wallbox %s", self.sn)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        self._attr_is_on = self._compute_is_on_from_data(data)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Manual update from HA."""
        await self.coordinator.async_request_refresh()
        data = self.coordinator.data.get(self.sn, {}) or {}
        self._attr_is_on = self._compute_is_on_from_data(data)
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Base class for cloud set-config switches (plug & charge, dynamic load, phase)
# ---------------------------------------------------------------------------

_CLOUD_CONFIG_PENDING_TIMEOUT = 60.0


class _SemsConfigSwitch(CoordinatorEntity, SwitchEntity):
    """Base for simple cloud switches that toggle via set_config_gen2.

    Subclasses must implement:
      _attr_translation_key
      unique_id property
      _data_key: str          -- key in coordinator data dict
      _on_value: any          -- expected coordinator value when ON
      _set_config_on: dict    -- kwargs passed to set_config_gen2 when turning ON
      _set_config_off: dict   -- kwargs passed to set_config_gen2 when turning OFF
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    _data_key: str = ""
    _on_value: object = True
    _set_config_on: dict = {}
    _set_config_off: dict = {}

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        self.api = api
        self._pending_state: bool | None = None
        self._pending_set_at: float = 0.0

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
    def is_on(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        reported = data.get(self._data_key)
        api_val = reported if type(reported) is bool else None
        if self._pending_state is not None:
            if time.monotonic() - self._pending_set_at >= _CLOUD_CONFIG_PENDING_TIMEOUT:
                self._pending_state = None
            elif api_val == self._pending_state:
                self._pending_state = None
            else:
                return self._pending_state
        return api_val

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @mode_setting_write
    async def _async_set(self, state: bool) -> None:
        self._pending_state = state
        self._pending_set_at = time.monotonic()
        self.async_write_ha_state()
        kwargs = self._set_config_on if state else self._set_config_off
        ok = await async_execute(self.hass,
            lambda: self.api.set_config_gen2(self.sn, **kwargs)
        )
        if not ok:
            _LOGGER.warning("%s: set_config failed, reverting optimistic state", self.unique_id)
            self._pending_state = None
            self.async_write_ha_state()
            self.coordinator.schedule_delayed_refresh(3.0)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(5.0)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class SemsPlugAndChargeSwitch(_SemsConfigSwitch):
    """Capability-gated Plug and Charge using SEMS+ 0/1 writes and readback."""

    _attr_translation_key = "plug_and_charge"
    _data_key = "plug_and_charge"
    _on_value = True
    _set_config_on = {"chargedNow": 1}
    _set_config_off = {"chargedNow": 0}

    @mode_setting_write
    async def _async_set(self, state: bool) -> None:
        """Send one configuration write and retain only device-reported state."""
        from .ui_errors import operation_error

        try:
            accepted = await async_execute(self.hass,
                lambda: self.api.set_config_gen2(self.sn, chargedNow=int(state))
            )
            if not accepted:
                raise ValueError("Plug and Charge setting was not acknowledged")
        except (ConnectionError, TimeoutError, ValueError) as error:
            raise operation_error(error) from error
        finally:
            # A timeout can leave an uncertain result. Read back without replaying
            # the write or substituting the requested value for measured state.
            self.coordinator.schedule_delayed_refresh(5.0)

    @property
    def is_on(self) -> bool | None:
        """Show reported state only; an accepted command is not device readback."""
        value = (self.coordinator.data.get(self.sn) or {}).get(self._data_key)
        return value if isinstance(value, bool) else None

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-switch-plug-and-charge"


class SemsDynamicLoadSwitch(_SemsConfigSwitch):
    """Dynamic Load Control switch (dynamicLoad: 1=enabled, 0=disabled)."""

    _attr_translation_key = "dynamic_load_control"
    _data_key = "dynamicLoad"
    _on_value = True
    _set_config_on = {"dynamicLoad": 1}
    _set_config_off = {"dynamicLoad": 0}

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-switch-dynamic-load"


class SemsPhaseSwitchSwitch(_SemsConfigSwitch):
    """Phase Switch (phaseSwitch: 1=single-phase, 0=three-phase).

    Note: the 'on' state means single-phase mode is forced.
    """

    _attr_translation_key = "phase_switch"
    _data_key = "phaseSwitch"
    _on_value = True
    _set_config_on = {"phaseSwitch": 1}
    _set_config_off = {"phaseSwitch": 0}

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-switch-phase-switch"


class SemsMinimumPowerSwitch(_SemsConfigSwitch):
    """Control minimum grid support using the shared, generation-aware encoder."""

    _attr_translation_key = "ensure_minimum_charging_power"
    _data_key = "ensure_minimum_charging_power"

    def __init__(self, coordinator, sn, api, *, generation=None):
        super().__init__(coordinator, sn, api)
        self.generation = generation

    @property
    def available(self) -> bool:
        return super().available

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data.get(self.sn) or {}).get(self._data_key)
        return value if type(value) is bool else None

    @mode_setting_write
    async def _async_set(self, state: bool) -> None:
        """Read before writing and keep only reported state after acknowledgement."""
        from .ui_errors import operation_error

        try:
            if not self.coordinator.last_update_success:
                raise ValueError("Cloud configuration is unavailable")
            reported = await async_execute(self.hass,
                lambda: self.api.get_data_gen2(self.sn))
            accepted = await async_execute(self.hass,
                lambda: write_minimum_power(
                    self.api, self.sn, self.generation, reported, state,
                    observation=dict(self.coordinator.data.get(self.sn) or {})))
            if not accepted:
                raise ValueError("Minimum-power setting was not acknowledged")
        except (ConnectionError, TimeoutError, ValueError) as error:
            raise operation_error(error) from error
        finally:
            # Acknowledgement is not a measurement; do not replay uncertain writes.
            self.coordinator.schedule_delayed_refresh(5.0)

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-switch-ensure-minimum-power"


# ---------------------------------------------------------------------------

_MODBUS_PENDING_TIMEOUT = 90.0


# Status codes that unambiguously mean "not charging and not about to charge".
# If pending_state is True but the wallbox is in one of these states, the
# optimistic ON must be abandoned immediately rather than waiting 90 s.
_MODBUS_TERMINAL_STOPPED = frozenset({0, 1, 4, 5, 7, 8, 10})


class _ModbusSwitch(CoordinatorEntity, SwitchEntity):
    """Base class for Modbus-backed boolean controls.

    Subclasses provide unique_id, translation_key, _api_state(), _do_write().
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, sn: str, client) -> None:
        super().__init__(coordinator)
        self.sn = sn
        self._client = client
        self._pending_state: bool | None = None
        self._pending_set_at: float = 0.0

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

    def _api_state(self) -> bool | None:
        raise NotImplementedError

    def _do_write(self, state: bool) -> bool:
        raise NotImplementedError

    @property
    def is_on(self) -> bool | None:
        api_val = self._api_state()
        if self._pending_state is not None:
            if time.monotonic() - self._pending_set_at >= _MODBUS_PENDING_TIMEOUT:
                self._pending_state = None
            elif api_val == self._pending_state:
                self._pending_state = None
            else:
                return self._pending_state
        return api_val

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @mode_setting_write
    async def _async_set(self, state: bool) -> None:
        self._pending_state = state
        self._pending_set_at = time.monotonic()
        self.async_write_ha_state()
        ok = await async_execute(self.hass, self._do_write, state)
        if not ok:
            _LOGGER.warning("%s: write failed, reverting optimistic state", self.unique_id)
            self._pending_state = None
            self.async_write_ha_state()
            self.coordinator.schedule_delayed_refresh(3.0)
            raise operation_error(RuntimeError("Device write was not confirmed"))
        else:
            self.coordinator.schedule_delayed_refresh(3.0)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class ModbusStartStopSwitch(_ModbusSwitch):
    """Start / stop charging via Modbus (reg 10060: 2=on, 1=off).

    State is derived from reg 10075 (car_connected):
      car=2 → CP at 6V → actively charging → switch ON
      car=1 → CP at 9V → connected but idle  → switch OFF
      car=0 → no car                          → unavailable
    reg 10017 (status_raw) is NOT used because the wallbox firmware keeps reporting
    status=charging (3) even after CP drops back to 9V, causing false ON readings.
    """

    _attr_translation_key = "modbus_start_charging"

    async def async_turn_on(self, **kwargs):
        """Apply the optional verified mode policy before Modbus Start."""
        if not await async_apply_policy(self.coordinator, "start"):
            await super().async_turn_on(**kwargs)

    async def async_turn_off(self, **kwargs):
        """Invalidate pending preparation before Modbus Stop."""
        if not await async_apply_policy(self.coordinator, "stop"):
            await super().async_turn_off(**kwargs)

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_start_stop"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return bool(data.get("modbus_car_connected", 0))

    def _api_state(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        # car_connected=2 means the CP signal is at 6V (car actively drawing power).
        # car_connected=1 means CP is at 9V -- car is plugged in but not charging.
        # The pending_state mechanism covers the handshaking window (status=2) where
        # car is still 1 but the user just pressed ON.
        return data.get("modbus_car_connected") == 2

    @property
    def is_on(self) -> bool | None:
        """Like base class, but clears a stale optimistic ON on terminal states."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        raw_status = data.get("modbus_status_raw")
        # If we're waiting for charging to confirm (pending=True) but the wallbox
        # is already in a clearly-stopped state (completed, idle, failed …), there
        # is no point keeping the optimistic ON for 90 s -- drop it immediately.
        if self._pending_state is True and raw_status in _MODBUS_TERMINAL_STOPPED:
            _LOGGER.debug(
                "%s: clearing optimistic ON -- wallbox in terminal state %s",
                self.unique_id, raw_status,
            )
            self._pending_state = None
        return super().is_on

    def _do_write(self, state: bool) -> bool:
        return self._client.write_start_stop(state)


class ModbusMaintainMinPowerSwitch(_ModbusSwitch):
    """Enable / disable maintain minimum charging power (reg 10024)."""

    _attr_translation_key = "modbus_maintain_min_power"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_maintain_min_power"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in (1, 2)

    def _api_state(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        value = data.get("ensure_minimum_charging_power")
        return value if type(value) is bool else None

    def _do_write(self, state: bool) -> bool:
        return self._client.write_maintain_min_power(state)


class ModbusPlugChargeSwitch(_ModbusSwitch):
    """Enable / disable Plug & Charge function (reg 10019)."""

    _attr_translation_key = "modbus_plug_charge"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_plug_charge"

    def _api_state(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        value = data.get("modbus_plug_charge_enabled")
        return value if type(value) is bool else None

    def _do_write(self, state: bool) -> bool:
        return self._client.write_plug_charge(state)


class ModbusDynamicLoadMgmtSwitch(_ModbusSwitch):
    """Enable / disable dynamic load management (reg 10025)."""

    _attr_translation_key = "modbus_dynamic_load"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_dynamic_load"

    def _api_state(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        value = data.get("modbus_dynamic_load")
        return value if type(value) is bool else None

    def _do_write(self, state: bool) -> bool:
        return self._client.write_dynamic_load_mgmt(state)


class ModbusEmsDispatchSwitch(_ModbusSwitch):
    """EMS minimum power dispatch mode (reg 10000: 0=normal, 1=min-power)."""

    _attr_translation_key = "modbus_ems_dispatch"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_ems_dispatch"

    def _api_state(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        value = data.get("modbus_ems_dispatch")
        return value == 1 if type(value) is int and value in (0, 1) else None

    def _do_write(self, state: bool) -> bool:
        return self._client.write_ems_dispatch(state)


class ModbusPhaseSwitchSwitch(_ModbusSwitch):
    """Enable / disable automatic single/three-phase switching (reg 10023).

    When enabled, the wallbox firmware automatically switches between single and
    three-phase operation based on available power. Only available on three-phase
    units (11 kW and 22 kW). The minimum settable charge power stays at 4.2 kW
    per the protocol spec; the firmware handles phase selection internally.
    """

    _attr_translation_key = "modbus_phase_switch"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_phase_switch"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        # pile_type 0 = three-phase; 1 = single-phase (switch meaningless on single-phase)
        pile_type = data.get("modbus_pile_type")
        return pile_type == 0

    def _api_state(self) -> bool | None:
        data = self.coordinator.data.get(self.sn, {}) or {}
        value = data.get("modbus_phase_switch_enabled")
        return value if type(value) is bool else None

    def _do_write(self, state: bool) -> bool:
        return self._client.write_phase_switch(state)
