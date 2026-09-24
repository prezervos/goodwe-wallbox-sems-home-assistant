"""Core wallbox controls sharing entity identities across cloud and native TCP."""

from __future__ import annotations

from dataclasses import replace
from math import isfinite
from typing import ClassVar

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .charge_mode_policy import ModeVerificationError
from .native_power_limits import power_bounds, power_tenths
from .observed_state import vehicle_state
from .ui_errors import operation_error

MODES = {0: "fast", 1: "pv_priority", 2: "pv_and_battery"}


class NativeEntity(CoordinatorEntity):
    """Expose observed state and keep requested settings separate."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, unique_id, translation_key):
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._attr_translation_key = translation_key
        self._attr_device_info = {
            "identifiers": {("sems_wallbox", coordinator.serial)},
            "manufacturer": "GoodWe",
            "name": f"GoodWe Wallbox {coordinator.serial}",
        }

    @property
    def device_info(self):
        """Expose known identity metadata without inventing missing versions."""
        info = dict(self._attr_device_info)
        settings = getattr(self.coordinator, "cloud_settings", None)
        sources = (self.values, getattr(settings, "values", {}))
        for output, field in (("model", "model"), ("sw_version", "fireware")):
            for source in sources:
                value = source.get(field)
                if isinstance(value, str) and value.strip() and value.strip().lower() not in (
                    "unknown", "unavailable",
                ):
                    info[output] = value.strip()
                    break
        if "model" not in info:
            identity = getattr(self.coordinator, "resolved_device_identity", {})
            model = identity.get("product_model")
            if (isinstance(model, str) and model.strip()
                    and model.strip().lower() not in ("unknown", "unavailable")):
                info["model"] = model.strip()
        return info

    @property
    def values(self):
        return (self.coordinator.data or {}).get(self.coordinator.serial, {})

    @property
    def reported_power_limit(self):
        """Read the transport's observed configuration, never the HA preference."""
        owner = self.coordinator
        if not owner.last_update_success or getattr(owner, "transitioning", False):
            return None
        if getattr(owner, "_closed", False):
            return None
        if owner.local:
            value = self.values.get("set_charge_power")
        else:
            # V3 telemetry can retain an old ceiling after a successful SEMS+
            # write. Reuse the bounded configuration poll and shared session.
            settings = getattr(owner, "cloud_settings", None)
            if settings is None or not settings.cloud_ready or not settings.valid:
                return None
            value = settings.values.get("set_charge_power")
        if isinstance(value, bool) or value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if isfinite(value) and value >= 0 else None

    async def invoke(self, operation, *, refresh=True):
        try:
            await operation()
        except (
            ModeVerificationError,
            ConnectionError,
            TimeoutError,
            ValueError,
        ) as exc:
            raise operation_error(exc) from exc
        finally:
            if refresh:
                await self.coordinator.async_refresh()


class ControlEntity(NativeEntity):
    """Accept latest user choices during handover without faking sensor telemetry."""

    @property
    def available(self):
        pending = getattr(self.coordinator, "pending_intent", None)
        return super().available or bool(
            pending is not None and not pending.closed
            and (pending.blackout or pending.pending or pending.executing is not None)
        )

    async def submit(self, key, value, operation):
        if key in ("power", "mode"):
            original_operation = operation

            async def update_setting():
                try:
                    await original_operation()
                finally:
                    # Refresh even after an uncertain write, including deferred
                    # commands. An ACK must not become a reported configuration.
                    settings = getattr(self.coordinator, "cloud_settings", None)
                    if settings is not None and not self.coordinator.local:
                        settings.invalidate()

            operation = update_setting
        control = getattr(self.coordinator, "control_fallback", None)
        if key == "charging" and control is not None and control.submit(
            value, lambda: self.invoke(operation)
        ):
            return
        pending = getattr(self.coordinator, "pending_intent", None)
        if pending is not None and pending.submit(
            key, value, lambda: self.invoke(operation)
        ):
            return
        await self.invoke(operation)


class ChargingSwitch(ControlEntity, SwitchEntity):
    """Expose charging or an enabled PV request; measured power remains separate."""

    @property
    def is_on(self):
        guard = self.coordinator.transport.session_guard
        if self.coordinator.local and guard.mode != 0 and guard.phase == "waiting":
            # An enabled PV session must remain switchable off while drawing zero.
            return True
        return self.values.get("last_charge_work_status") == 6 or str(
            self.values.get("status", "")
        ).lower() in ("charging", "evdetail_status_title_charging")

    @property
    def extra_state_attributes(self):
        guard = self.coordinator.transport.session_guard
        return {
            "tcp_operation": guard.phase if self.coordinator.local else None,
            "protection_error": guard.error,
            "power_guard_basis": "requested_limit" if self.coordinator.local else None,
        }

    async def async_turn_on(self, **kwargs):
        await self.submit("charging", True, self.coordinator.charge_mode_policy.async_start)

    async def async_turn_off(self, **kwargs):
        await self.submit("charging", False, self.coordinator.charge_mode_policy.async_stop)


class ConnectionSwitch(NativeEntity, SwitchEntity):
    """Own/release Socket A without changing the charging plan or replaying Start."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self):
        return not self.coordinator._closed

    @property
    def is_on(self):
        return self.coordinator.local

    @property
    def extra_state_attributes(self):
        return {
            "transitioning": self.coordinator.transitioning,
            "transition_target": self.coordinator.transition_target,
            "recovery_pending": self.coordinator.endpoint.journal is not None,
            "cloud_account_configured": self.coordinator.cloud is not None,
            "power_guard_error": self.coordinator.transport.session_guard.error,
            "power_guard_active": self.coordinator.transport.session_guard.limit
            is not None,
        }

    async def async_turn_on(self, **kwargs):
        await self.invoke(lambda: self.coordinator.async_set_local(True), refresh=False)

    async def async_turn_off(self, **kwargs):
        await self.invoke(
            lambda: self.coordinator.async_set_local(False), refresh=False
        )


class TransportStatusSensor(NativeEntity, SensorEntity):
    """Keep connection progress visible while measurements are unavailable."""

    _attr_device_class = "enum"
    _attr_options: ClassVar[list[str]] = [
        "cloud",
        "tcp",
        "switching_to_tcp",
        "switching_to_cloud",
        "waiting_for_cloud",
        "recovery_required",
        "connection_unavailable",
    ]

    @property
    def available(self):
        return not self.coordinator._closed

    @property
    def native_value(self):
        owner = self.coordinator
        if owner.transitioning:
            return f"switching_to_{owner.transition_target}"
        if owner.endpoint.journal is not None and not owner.local:
            return "recovery_required"
        if not owner.local and owner.cloud_restored_at is not None:
            return "waiting_for_cloud"
        if not owner.last_update_success:
            return "connection_unavailable"
        return "tcp" if owner.local else "cloud"

    @property
    def extra_state_attributes(self):
        fallback = getattr(self.coordinator, "automatic_fallback", None)
        values = fallback.diagnostics() if fallback is not None else {}
        control = getattr(self.coordinator, "control_fallback", None)
        if control is not None:
            values.update(control_preflight=control.preparing, control_error=control.error)
        pending = getattr(self.coordinator, "pending_intent", None)
        if pending is not None:
            values.update(pending.diagnostics())
        return values

    @property
    def icon(self):
        if self.coordinator.transitioning:
            return "mdi:swap-horizontal"
        if self.native_value == "waiting_for_cloud":
            return "mdi:cloud-clock-outline"
        if self.native_value in ("recovery_required", "connection_unavailable"):
            return "mdi:alert-circle-outline"
        return (
            "mdi:lan-connect" if self.coordinator.local else "mdi:cloud-check-outline"
        )


class ChargeModeSelect(ControlEntity, SelectEntity):
    """Use the existing charging mode identity with independently verified writes."""

    _attr_entity_category = None
    _attr_options: ClassVar[list[str]] = list(MODES.values())

    @property
    def current_option(self):
        return MODES.get(
            self.values.get("_reported_charge_mode", self.values.get("chargeMode"))
        )

    @property
    def extra_state_attributes(self):
        return {
            "preferred_mode": MODES.get(self.coordinator.charge_mode_policy.desired_mode),
            "restore_mode_before_start": self.coordinator.charge_mode_policy.remember_mode,
        }

    async def async_select_option(self, option):
        mode = next(key for key, value in MODES.items() if value == option)
        await self.submit(
            "mode", mode, lambda: self.coordinator.charge_mode_policy.async_select_mode(mode)
        )


class ChargePowerNumber(ControlEntity, NumberEntity):
    """Keep HA intent durable and validate power against the enrolled model."""

    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = "power"
    _attr_native_step = 0.1
    _attr_mode = "box"

    @property
    def native_min_value(self):
        return (
            power_bounds(self.coordinator.serial)[0]
            if self.coordinator.local
            else float(
                self.values.get("min_charge_power")
                or power_bounds(self.coordinator.serial)[0]
            )
        )

    @property
    def native_max_value(self):
        return (
            power_bounds(self.coordinator.serial)[1]
            if self.coordinator.local
            else float(
                self.values.get("max_charge_power")
                or power_bounds(self.coordinator.serial)[1]
            )
        )

    @property
    def native_value(self):
        return self.coordinator.charge_mode_policy.desired_power

    @property
    def extra_state_attributes(self):
        return {
            "reported_power_limit": self.reported_power_limit,
            "native_power_min": power_bounds(self.coordinator.serial)[0],
            "native_power_max": power_bounds(self.coordinator.serial)[1],
        }

    async def async_set_native_value(self, value):
        if self.coordinator.local or (
            getattr(self.coordinator, "pending_intent", None) is not None
            and self.coordinator.pending_intent.blackout
        ):
            try:
                power_tenths(value, self.coordinator.serial)
            except ValueError as exc:
                minimum, maximum = power_bounds(self.coordinator.serial)
                raise HomeAssistantError(
                    translation_domain="sems_wallbox",
                    translation_key="invalid_power",
                    translation_placeholders={
                        "minimum": f"{minimum:g}",
                        "maximum": f"{maximum:g}",
                    },
                ) from exc
        owner = self.coordinator

        async def write():
            policy = owner.charge_mode_policy
            version = policy._version
            before = await policy.adapter.read()
            policy._check(version)
            if owner.local:
                await owner.transport.async_command(
                    "power",
                    tenths_kw=power_tenths(value, owner.serial),
                    intent_allowed=lambda: (
                        not policy._closed and policy._version == version
                    ),
                )
            elif (
                await owner.charge_mode_policy.adapter.write_mode(
                    0, replace(before, power=value)
                )
                is not True
            ):
                raise ModeVerificationError("Power limit was not confirmed")

        await self.submit(
            "power", value, lambda: owner.charge_mode_policy.async_setting_write(
                write, desired_mode=None if owner.local else 0, desired_power=value
            )
        )


class SessionEnergySensor(NativeEntity, SensorEntity):
    """Expose observed session energy with the original registry identity.

    Timestamped SEMS v3 reports use chargeEnergy in kWh; SEMS+ normalized
    reports use last_charge_energy. Native HCA status supplies session_energy_kwh
    and resets it after Stop. Never reuse values from the other transport.
    """

    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = "energy"
    _attr_state_class = "total_increasing"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self):
        # Select the current transport/schema, never a stale fallback value.
        if self.coordinator.local:
            field = "session_energy_kwh"
        else:
            field = "chargeEnergy" if "chargeEnergy" in self.values else "last_charge_energy"
        raw = self.values.get(field)
        if isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(value) and value >= 0 else None


class TotalEnergySensor(NativeEntity, SensorEntity):
    """Expose the native cumulative counter independently of cloud session energy."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = "energy"
    _attr_state_class = "total_increasing"
    _attr_suggested_display_precision = 2

    @property
    def available(self):
        return self.coordinator.energy_polling.available

    @property
    def native_value(self):
        value = self.coordinator.energy_polling.value
        return value.energy_kwh if self.available else None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.coordinator.energy_polling.start()

    async def async_will_remove_from_hass(self):
        await self.coordinator.energy_polling.close()
        await super().async_will_remove_from_hass()


class ValueSensor(NativeEntity, SensorEntity):
    """Publish one observed field without filling missing data with zero."""

    def __init__(
        self,
        coordinator,
        unique_id,
        translation_key,
        field,
        unit=None,
        device_class=None,
        index=None,
    ):
        super().__init__(coordinator, unique_id, translation_key)
        self.field = field
        if field == "status":
            device_class = "enum"
            self._attr_options = [
                "charging", "standby", "offline", "unknown", "starting", "waiting",
            ]
        if field == "workstate":
            self._attr_options = ["not_plugged_in", "connected", "finished_charging"]
        self.index = index
        if field == "set_charge_power":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if field in ("currents_a", "voltages_v"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        if unit in ("kW", "A", "V", "min"):
            self._attr_state_class = "measurement"

    @property
    def available(self):
        if not super().available:
            return False
        if not self.coordinator.local and self.field in (
            "currents_a",
            "voltages_v",
            "fault_code",
        ):
            return self.values.get(self.field) is not None
        return True

    @property
    def extra_state_attributes(self):
        """Preserve upstream status attributes when the current report has them."""
        if self.field != "status":
            return {}
        attributes = {}
        if self.values.get("status"):
            attributes["statusText"] = self.values["status"]
        for key in (
            "chargeMode", "scheduleMode", "schedule_total_minute",
            "set_charge_power", "charge_from_grid", "ensure_minimum_charging_power",
            "last_charge_work_status", "last_charge_power",
            "last_charge_duration_minutes",
        ):
            value = self.reported_power_limit if key == "set_charge_power" else self.values.get(key)
            if value is not None:
                attributes[key] = value
        return attributes

    @property
    def native_value(self):
        value = self.values.get(self.field)
        if self.field == "set_charge_power":
            return self.reported_power_limit
        if self.field == "workstate":
            return vehicle_state(self.values, local=self.coordinator.local)
        if self.field == "session_seconds":
            # Preserve the upstream entity's minute unit across both transports.
            if self.coordinator.local:
                raw = value
                divisor = 60
            else:
                raw = self.values.get("last_charge_duration_minutes")
                divisor = 1
                # The separate SEMS `time` field has unverified units. Only its
                # explicit zero is unambiguous; do not reuse a stale TCP duration.
                if raw is None and self.values.get("time") in (0, "0"):
                    raw = 0
            if raw is None or isinstance(raw, bool):
                return None
            try:
                minutes = float(raw) / divisor
            except (TypeError, ValueError, OverflowError):
                return None
            return minutes if isfinite(minutes) and minutes >= 0 else None

        if self.field == "status":
            if (
                self.coordinator.local
                and self.coordinator.transport.session_guard.phase
                in ("starting", "waiting")
            ):
                return self.coordinator.transport.session_guard.phase
            if self.values.get("last_charge_work_status") == 6 or str(
                value
            ).lower() in ("charging", "evdetail_status_title_charging"):
                return "charging"
            if str(value).lower() in (
                "waiting",
                "available",
                "standby",
                "evdetail_status_title_waiting",
            ):
                return "standby"
            if str(value).lower() in (
                "offline", "unavailable", "evdetail_status_title_offline",
            ):
                return "offline"
            return "unknown"
        if self.index is not None:
            return value[self.index] if value is not None else None
        if self._attr_native_unit_of_measurement and value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return value


def setup_platform(platform, coordinator, add_entities):
    """Install supported controls while retaining existing core unique IDs."""
    sn = coordinator.serial
    entities = []
    if platform == "switch":
        entities = [
            ChargingSwitch(
                coordinator, f"{sn}-switch-start-charging", "start_charging"
            ),
            ConnectionSwitch(
                coordinator, f"{sn}-native-connection", "native_connection"
            ),
        ]
    elif platform == "select":
        entities = [
            ChargeModeSelect(coordinator, f"{sn}-select-charge-mode", "charge_mode")
        ]
    elif platform == "number":
        entities = [
            ChargePowerNumber(
                coordinator, f"{sn}_number_set_charge_power", "charge_power"
            )
        ]
    elif platform == "sensor":
        entities = [
            ValueSensor(coordinator, sn, "status", "status"),
            SessionEnergySensor(coordinator, f"{sn}-energy", "energy"),
            ValueSensor(
                coordinator,
                f"{sn}_workstate",
                "workstate",
                "workstate",
                device_class="enum",
            ),
            ValueSensor(coordinator, f"{sn}_power", "power", "power", "kW", "power"),
            ValueSensor(
                coordinator,
                f"{sn}_set_charge_power_limit",
                "set_charge_power_limit",
                "set_charge_power",
                "kW",
                "power",
            ),
            ValueSensor(
                coordinator,
                f"{sn}_charge_duration",
                "charge_duration",
                "session_seconds",
                "min",
                "duration",
            ),
            ValueSensor(
                coordinator, f"{sn}_native_fault", "native_fault", "fault_code"
            ),
            TransportStatusSensor(
                coordinator, f"{sn}_active_transport", "active_transport"
            ),
            TotalEnergySensor(coordinator, f"{sn}_native_total_energy", "native_total_energy"),
        ]
        for index, phase in enumerate("abc"):
            entities.append(
                ValueSensor(
                    coordinator,
                    f"{sn}_native_current_{phase}",
                    f"current_{phase}",
                    "currents_a",
                    "A",
                    "current",
                    index,
                )
            )
            entities.append(
                ValueSensor(
                    coordinator,
                    f"{sn}_native_voltage_{phase}",
                    f"voltage_{phase}",
                    "voltages_v",
                    "V",
                    "voltage",
                    index,
                )
            )
    if getattr(coordinator, "cloud_settings", None) is not None:
        from .native_cloud_settings import (
            setup_cloud_settings, watch_reported_cloud_settings,
        )
        cloud_entities = setup_cloud_settings(platform, coordinator)
        entities.extend(cloud_entities)
        watch_reported_cloud_settings(platform, coordinator, add_entities, cloud_entities)
    add_entities(entities)
