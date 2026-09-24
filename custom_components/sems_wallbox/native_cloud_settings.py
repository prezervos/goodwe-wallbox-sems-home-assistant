"""Cloud configuration retained when native Socket A support is enabled.

Configuration is separate from timestamped telemetry: a detail response must not
make an old device report appear fresh. Verified minimum-power and original-HCA Auto start controls
can also use native TCP.
"""

from __future__ import annotations

from .cloud_current_limit import (
    current_attributes, current_bounds, current_writable, observed_current, validate_current_write,
)
from .operation_budget import async_execute

from .mode_parameters import preserved_mode_parameters

import asyncio
from dataclasses import dataclass
from functools import partial
import logging
from math import isfinite
import time

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .charge_mode_policy import ModeVerificationError
from .native_entities import NativeEntity
from .minimum_power import write_minimum_power
from .ui_errors import operation_error

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    """Describe an existing cloud control without changing its identity."""

    platform: str
    suffix: str
    translation: str
    field: str
    parameter: str
    modes: tuple[int, ...] = (0, 1, 2)
    unit: str | None = None
    device_class: str | None = None
    minimum: float = 0
    maximum: float = 200
    step: float = 1
    mode_parameter: bool = False
    on_value: int = 1


SETTINGS = (
    Setting(
        "number",
        "number-current-limit",
        "current_limit",
        "currentLimit",
        "currentLimit",
        unit="A",
        device_class="current",
        maximum=2000,
        step=0.01,
    ),
    Setting(
        "number",
        "number-output-power-limit",
        "output_power_limit",
        "rated_max_charge_power",
        "ratedMaxiChargePower",
        unit="kW",
        device_class="power",
        minimum=1.4,
        maximum=22,
        step=0.1,
    ),
    Setting(
        "number",
        "number-max-energy",
        "max_session_energy",
        "max_energy",
        "max_energy",
        unit="kWh",
        device_class="energy",
        mode_parameter=True,
    ),
    Setting(
        "number",
        "number-min-energy",
        "min_session_energy",
        "min_energy",
        "min_energy",
        modes=(1, 2),
        unit="kWh",
        device_class="energy",
        mode_parameter=True,
    ),
    Setting(
        "number",
        "number-target-soc",
        "charge_target_soc",
        "charge_target_soc",
        "soc_target",
        modes=(0, 2),
        unit="%",
        maximum=100,
        mode_parameter=True,
    ),
    Setting(
        "select",
        "select-charge-duration",
        "charge_duration",
        "finish_time",
        "finish_time",
        modes=(1, 2),
        mode_parameter=True,
    ),
    Setting(
        "switch",
        "switch-dynamic-load",
        "dynamic_load_control",
        "dynamicLoad",
        "dynamicLoad",
    ),
    Setting(
        "switch", "switch-phase-switch", "phase_switch", "phaseSwitch", "phaseSwitch"
    ),
    Setting(
        "switch",
        "switch-ensure-minimum-power",
        "ensure_minimum_charging_power",
        "ensure_minimum_charging_power",
        "ensureMinimumChargingPower",
        on_value=170,
    ),
    Setting(
        "switch",
        "switch-plug-and-charge",
        "plug_and_charge",
        "plug_and_charge",
        "chargedNow",
    ),
)


def number(value):
    """Return a finite reported number, preserving missing data."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if isfinite(result) else None


class CloudSettings:
    """Read configuration through the coordinator's existing SEMS session."""

    def __init__(self, owner):
        self.owner = owner
        self.values = {}
        self.valid = False
        self.next_refresh = 0.0
        self._task = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._observed_mode = None

    async def discover_capabilities(self):
        """Fill missing legacy metadata using the existing cloud session.

        Returns:
            Device metadata, or an empty mapping when discovery is unavailable.
        """
        owner = self.owner
        fetch = getattr(owner.cloud, "fetch_device_info", None)
        if not callable(fetch):
            return {}
        info = await owner.hass.async_add_executor_job(fetch, owner.serial)
        if not isinstance(info, dict) or not info:
            return {}
        self.values["controlItemRanges"] = info.get("controlItemRanges")
        owner.async_update_listeners()
        data = dict(owner.entry.data)
        for stored, remote in (
            ("dashboard_functions", "dashboardFunctions"),
            ("more_device_controls", "moreDeviceControls"),
        ):
            value = info.get(remote)
            if not data.get(stored) and isinstance(value, list) and all(
                isinstance(item, str) for item in value
            ):
                data[stored] = list(value)
        if not data.get("pile_generation") and info.get("pileGeneration") is not None:
            data["pile_generation"] = str(info["pileGeneration"])
        rated = number(info.get("ratedPower"))
        if data.get("rated_power") is None and rated is not None and rated > 0:
            data["rated_power"] = rated
        if data != dict(owner.entry.data):
            owner.hass.config_entries.async_update_entry(owner.entry, data=data)
        return info

    @property
    def cloud_ready(self):
        """Reject local, closing and unverified transport states."""
        owner = self.owner
        return not (
            self._closed
            or owner._closed
            or owner.local
            or owner.transitioning
            or owner.cloud_restored_at is not None
            or owner.cloud is None
        )

    @property
    def minimum_power_is_mode_setting(self):
        """Use the original-HCA SEMS+ PV-mode form for minimum grid support."""
        return str(self.owner.entry.data.get("pile_generation")) == "1"

    def supports_mode(self, setting):
        """Check whether the current reported mode supports a setting.

        Args:
            setting: Cloud setting descriptor.

        Returns:
            Whether the setting can be used without changing the charging mode.
        """
        mode = self.values.get("_reported_charge_mode")
        if (setting.field == "ensure_minimum_charging_power"
                and self.minimum_power_is_mode_setting):
            return type(mode) is int and mode in (0, 1, 2)
        return not setting.mode_parameter or mode in setting.modes

    def invalidate(self):
        """Require a fresh configuration read after a transport or mode change."""
        self.valid = False
        self.next_refresh = 0

    def observe_mode(self, mode):
        """Refresh mode-specific configuration after an observed mode change."""
        if mode is not None and mode != self._observed_mode:
            self._observed_mode = mode
            self.invalidate()
            self.request_refresh()

    def request_refresh(self):
        """Schedule bounded-frequency reads without delaying core telemetry."""
        if not self.cloud_ready:
            self.valid = False
            self.next_refresh = 0
            return
        if time.monotonic() < self.next_refresh or (
            self._task and not self._task.done()
        ):
            return
        self._task = self.owner.hass.async_create_task(self.refresh())

    async def refresh(self):
        """Read only configuration; discard responses crossing a handover."""
        async with self._lock:
            if not self.cloud_ready:
                self.valid = False
                return
            epoch = self.owner.routing_epoch
            self.next_refresh = time.monotonic() + 300
            try:
                data = await async_execute(self.owner.hass,
                    self.owner.cloud.get_data_gen2, self.owner.serial
                )
                # Complete range discovery even when model/capabilities were saved
                # previously or startup was local. Retry failures only at the
                # existing configuration refresh cadence, not telemetry frequency.
                if (isinstance(data, dict) and data.get("sn") == self.owner.serial
                        and data.get("controlItemRanges") is False):
                    info = await async_execute(
                        self.owner.hass, self.owner.cloud.fetch_device_info, self.owner.serial
                    )
                    data["controlItemRanges"] = (
                        info.get("controlItemRanges") if isinstance(info, dict) and info
                        and info.get("sn", self.owner.serial) == self.owner.serial else False
                    )
            except (OSError, ValueError, RuntimeError) as error:
                _LOGGER.debug("Cloud settings read failed: %s", error)
                data = None
            if not self.cloud_ready or epoch != self.owner.routing_epoch:
                self.valid = False
                self.next_refresh = 0
                return
            self.valid = isinstance(data, dict) and data.get("sn") == self.owner.serial
            self.values = dict(data) if self.valid else {}
            self.owner.async_update_listeners()

    async def write(self, setting, value):
        """Serialize a checked write with Start, mode changes and handovers.

        Args:
            setting: Cloud setting descriptor.
            value: Validated API value.

        Raises:
            ModeVerificationError: Transport, readback or acknowledgement is invalid.
        """

        async def operation():
            if not self.cloud_ready or not self.owner.last_update_success:
                raise ModeVerificationError(
                    "Cloud configuration is unavailable on this transport"
                )
            epoch = self.owner.routing_epoch
            await self.refresh()
            if not self.valid or self.values.get(setting.field) is None:
                raise ModeVerificationError(
                    "The wallbox did not report this setting; no write was sent"
                )
            mode = self.values.get("_reported_charge_mode")
            if not self.supports_mode(setting):
                raise ModeVerificationError(
                    "The setting is unavailable in the reported charging mode"
                )
            if setting.field == "currentLimit":
                try:
                    info = await async_execute(
                        self.owner.hass, self.owner.cloud.fetch_device_info, self.owner.serial
                    )
                    if not isinstance(info, dict) or not info or info.get("sn", self.owner.serial) != self.owner.serial:
                        raise ValueError("Missing or mismatched cloud configuration")
                    self.values["controlItemRanges"] = info.get("controlItemRanges")
                    self.owner.async_update_listeners()
                    validate_current_write(self.values.get(setting.field), value, self.values["controlItemRanges"])
                except ValueError as error:
                    raise ModeVerificationError(str(error)) from error
            if not self.cloud_ready or epoch != self.owner.routing_epoch:
                raise ModeVerificationError("Cloud configuration is unavailable on this transport")
            api = self.owner.cloud
            if setting.field == "ensure_minimum_charging_power":
                send = partial(
                    write_minimum_power, api, self.owner.serial,
                    self.owner.entry.data.get("pile_generation"), self.values, bool(value),
                    observation=dict(self.owner.data.get(self.owner.serial) or {}),
                )
            elif setting.mode_parameter:
                params = preserved_mode_parameters(self.values, mode)
                params[setting.parameter] = value
                power = None
                if mode == 0:
                    power = self.owner.charge_mode_policy.desired_power
                    if power is None:
                        power = number(self.values.get("set_charge_power"))
                    if power is None:
                        raise ModeVerificationError(
                            "Cannot preserve unreported charging power"
                        )
                send = partial(
                    api.set_charge_mode_gen2,
                    self.owner.serial,
                    mode,
                    power,
                    None,
                    **params,
                )
            else:
                send = partial(
                    api.set_config_gen2, self.owner.serial, **{setting.parameter: value}
                )
            try:
                accepted = await async_execute(self.owner.hass, send)
                if not accepted:
                    raise ModeVerificationError(
                        "Cloud did not acknowledge the configuration write"
                    )
            finally:
                # An ACK is not a measurement; also refresh after uncertain writes.
                self.valid = False
                self.next_refresh = 0
                await self.refresh()

        await self.owner.charge_mode_policy.async_setting_write(operation)

    async def close(self):
        """Await any executor-backed read before disposing the cloud session."""
        self._closed = True
        if self._task is not None:
            await self._task


class CloudSettingEntity(NativeEntity):
    """Retain cloud identities without showing cached values as local readings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, setting):
        super().__init__(
            coordinator, f"{coordinator.serial}-{setting.suffix}", setting.translation
        )
        self.setting = setting
        self.settings = coordinator.cloud_settings

    @property
    def available(self):
        return (
            super().available
            and self.settings.cloud_ready
            and self.settings.valid
            and (self.setting.field == "ensure_minimum_charging_power"
                 or self.settings.supports_mode(self.setting))
        )

    @property
    def reported(self):
        return self.settings.values.get(self.setting.field) if self.available else None

    @property
    def extra_state_attributes(self):
        return {"supported_transport": "cloud", "tcp_support": "unverified"}

    async def async_update(self):
        """Refresh configuration explicitly without writing any device settings."""
        await self.settings.refresh()

    async def write(self, value):
        await self.invoke(
            lambda: self.settings.write(self.setting, value), refresh=False
        )


class CloudSettingNumber(CloudSettingEntity, NumberEntity):
    """Expose the original numeric control with observed values only."""

    def __init__(self, coordinator, setting):
        super().__init__(coordinator, setting)
        self._attr_native_unit_of_measurement = setting.unit
        self._attr_device_class = setting.device_class
        self._attr_native_step = setting.step
        self._attr_mode = "box" if setting.field == "currentLimit" else "slider"

    @property
    def range_metadata(self):
        return self.settings.values.get("controlItemRanges")

    @property
    def available(self):
        return super().available and (
            self.setting.field != "currentLimit"
            or current_writable(self.settings.values.get("currentLimit"), self.range_metadata)
        )

    @property
    def native_min_value(self):
        if self.setting.field == "currentLimit":
            try:
                return current_bounds(self.range_metadata)[0]
            except ValueError:
                return 0.0
        return self.setting.minimum

    @property
    def extra_state_attributes(self):
        attributes = super().extra_state_attributes
        if self.setting.field == "currentLimit":
            attributes.update(current_attributes(
                self.settings.values.get("currentLimit"), self.range_metadata
            ))
        return attributes

    @property
    def native_max_value(self):
        if self.setting.field == "currentLimit":
            try:
                return current_bounds(self.range_metadata)[1]
            except ValueError:
                return 2000.0
        if self.setting.field == "rated_max_charge_power":
            for key in ("hw_max_charge_power", "max_charge_power"):
                value = number(self.settings.values.get(key))
                if value is not None and value > 0:
                    return value
        return self.setting.maximum

    @property
    def native_value(self):
        if self.setting.field == "currentLimit":
            return observed_current(self.reported)
        return number(self.reported)

    async def async_set_native_value(self, value):
        if self.setting.field == "currentLimit":
            try:
                value = validate_current_write(self.settings.values.get("currentLimit"), value, self.range_metadata)
            except ValueError as error:
                raise operation_error(error) from error
            await self.write(value)
            return
        parsed = number(value)
        if (
            parsed is None
            or not self.setting.minimum <= parsed <= self.native_max_value
        ):
            raise ValueError("Configuration value is outside its supported range")
        if self.setting.step == 1 and not parsed.is_integer():
            raise ValueError("This setting requires a whole number")
        await self.write(int(parsed) if self.setting.step == 1 else parsed)


class CloudSettingSwitch(CloudSettingEntity, SwitchEntity):
    """Distinguish a reported disabled feature from absent data."""

    @property
    def is_on(self):
        value = self.reported
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs):
        await self.write(self.setting.on_value)

    async def async_turn_off(self, **kwargs):
        await self.write(0)


class MinimumPowerSwitch(CloudSettingSwitch):
    """Use fresh native minimum-power reports while retaining the cloud identity.

    Original HCA writes require idle in both cloud and native modes; active
    writes did not change the device flag. Unknown generations retain the
    existing cloud behavior.
    """

    @property
    def native_supported(self):
        """Restrict the verified layout to discovered original HCA devices."""
        return self.settings.minimum_power_is_mode_setting

    @property
    def available(self):
        owner = self.coordinator
        if not owner.local:
            return super().available
        if (not self.native_supported or owner._closed or owner.transitioning
                or not owner.transport.available):
            return False
        status = owner.transport.latest
        return status.minimum_power is not None

    @property
    def reported(self):
        if self.coordinator.local:
            return self.coordinator.transport.latest.minimum_power if self.available else None
        return super().reported

    @property
    def extra_state_attributes(self):
        if not self.native_supported:
            return super().extra_state_attributes
        return {"supported_transport": "cloud,tcp" if self.coordinator.cloud else "tcp",
                "tcp_support": "idle_write"}

    async def async_update(self):
        if self.coordinator.local:
            await self.coordinator.async_request_refresh()
        else:
            await super().async_update()

    async def write(self, value):
        owner = self.coordinator
        if not owner.local:
            return await super().write(value)
        epoch = owner.routing_epoch

        async def operation():
            if not owner.local or epoch != owner.routing_epoch or not self.available:
                raise ModeVerificationError("Minimum-power setting is unavailable on this transport")
            status = owner.transport.latest
            if (status.mode not in (0, 1, 2) or status.state != 0 or not status.stopped
                    or owner.transport.session_guard.phase in
                    ("starting", "waiting", "charging")):
                raise ModeVerificationError("Minimum-power write requires an idle wallbox")
            if value not in (0, 170):
                raise ValueError("Invalid minimum-power setting")
            await owner.transport.async_command(
                "minimum_power", minimum_power=bool(value), timeout=15)

        await self.invoke(lambda: owner.charge_mode_policy.async_setting_write(operation))


class AutoStartSwitch(CloudSettingSwitch):
    """Preserve the Plug and Charge identity with verified native Auto start."""

    @property
    def native_supported(self):
        """Restrict local envelopes and the fixed snapshot to original HCA."""
        return (self.settings.minimum_power_is_mode_setting
                and len(self.coordinator.serial) == 16)

    @property
    def cloud_supported(self):
        """Do not reuse the ineffective original-HCA cloud write without capability."""
        return "plugAndCharge" in (self.coordinator.entry.data.get("dashboard_functions") or [])

    @property
    def available(self):
        if self.coordinator.local:
            return self.native_supported and self.coordinator.configuration_polling.available
        return (self.cloud_supported and super().available
                and type(self.settings.values.get("plug_and_charge")) is bool)

    @property
    def reported(self):
        if self.coordinator.local:
            return self.coordinator.configuration_polling.value.auto_start if self.available else None
        return super().reported

    @property
    def extra_state_attributes(self):
        if not self.native_supported:
            return super().extra_state_attributes
        return {"supported_transport": "cloud,tcp" if self.cloud_supported else "tcp",
                "tcp_support": "verified_write"}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.native_supported:
            self.coordinator.configuration_polling.start()

    async def async_will_remove_from_hass(self):
        if self.native_supported:
            await self.coordinator.configuration_polling.close()
        await super().async_will_remove_from_hass()

    async def async_update(self):
        if self.coordinator.local:
            await self.coordinator.configuration_polling.tick()
        else:
            await super().async_update()

    async def write(self, value):
        owner = self.coordinator
        if not owner.local:
            if not self.cloud_supported:
                raise operation_error(ModeVerificationError(
                    "Auto start is available only over TCP on this wallbox"
                ))
            return await super().write(value)
        epoch = owner.routing_epoch

        async def operation():
            if value not in (0, 1):
                raise ValueError("Invalid Auto start value")
            polling = owner.configuration_polling
            async with polling.lock:
                if (not self.native_supported or not owner.local or owner.transitioning
                        or epoch != owner.routing_epoch or owner._closed):
                    raise ModeVerificationError("Auto start is unavailable on this transport")
                polling.value = None
                polling.next_read = 0
                try:
                    result = await owner.transport.async_set_auto_start(bool(value))
                    if epoch != owner.routing_epoch or not owner.local:
                        raise ConnectionError("Auto start transport changed")
                    polling.value = result
                    polling.error = None
                    polling.epoch = owner.transport.epoch
                    polling.next_read = time.monotonic() + polling.INTERVAL
                finally:
                    # Cancellation/timeout must never leave a requested value cached.
                    owner.async_update_listeners()

        await self.invoke(lambda: owner.charge_mode_policy.async_setting_write(operation), refresh=False)


class CloudSettingSelect(CloudSettingEntity, SelectEntity):
    """Select a reported completion target, not elapsed session duration."""

    _attr_options = ["asap", "1h", "2h", "3h", "4h", "5h", "6h"]

    @property
    def current_option(self):
        value = number(self.reported)
        if value is None or not value.is_integer() or not 0 <= value <= 6:
            return None
        return self._attr_options[int(value)]

    async def async_select_option(self, option):
        if option not in self._attr_options:
            raise ValueError("Invalid completion time")
        await self.write(str(self._attr_options.index(option)))


def setup_cloud_settings(platform, coordinator):
    """Keep upstream controls registered across cloud/TCP transport changes."""
    native_auto_start = (coordinator.cloud_settings.minimum_power_is_mode_setting
                         and len(coordinator.serial) == 16)
    if coordinator.cloud is None:
        if platform != "switch":
            return []
        transport = getattr(coordinator, "transport", None)
        entities = []
        for setting in SETTINGS:
            if setting.field == "plug_and_charge" and native_auto_start:
                entities.append(AutoStartSwitch(coordinator, setting))
            elif (setting.field == "ensure_minimum_charging_power"
                  and coordinator.cloud_settings.minimum_power_is_mode_setting
                  and transport is not None and transport.available
                  and transport.latest.minimum_power is not None):
                entities.append(MinimumPowerSwitch(coordinator, setting))
        return entities
    classes = {
        "number": CloudSettingNumber,
        "switch": CloudSettingSwitch,
        "select": CloudSettingSelect,
    }
    if platform not in classes:
        return []
    # Keep the seven existing configuration controls. Optional feature controls
    # retain upstream capability gates; registry identities do not depend on mode.
    data = coordinator.entry.data
    more = data.get("more_device_controls") or []
    dashboard = data.get("dashboard_functions") or []
    gates = {
        "dynamicLoad": not more or "Dynamic_Load_Control" in more,
        "rated_max_charge_power": not more or "Dynamic_Load_Control" in more,
        "phaseSwitch": "Phase_Switch" in more,
        "plug_and_charge": "plugAndCharge" in dashboard or native_auto_start,
        "ensure_minimum_charging_power": not more
        or "Ensure_minimum_Charging_Power" in more
        or (coordinator.cloud_settings.minimum_power_is_mode_setting
            and getattr(coordinator, "transport", None) is not None
            and coordinator.transport.available
            and coordinator.transport.latest.minimum_power is not None)
        or (coordinator.cloud_settings.valid
            and coordinator.cloud_settings.cloud_ready
            and isinstance(coordinator.cloud_settings.values.get(
                "ensure_minimum_charging_power"), bool)),
    }
    return [
        (MinimumPowerSwitch if setting.field == "ensure_minimum_charging_power"
         else AutoStartSwitch if setting.field == "plug_and_charge"
         else classes[platform])(coordinator, setting)
        for setting in SETTINGS
        if setting.platform == platform and gates.get(setting.field, True)
    ]


def watch_reported_cloud_settings(platform, coordinator, add_entities, existing):
    """Register newly reported controls without guessing capability or reloading.

    Args:
        platform: HA entity platform currently being set up.
        coordinator: Shared native/cloud coordinator.
        add_entities: Platform entity registration callback.
        existing: Entities already registered during this setup.
    """
    if platform != "switch":
        return
    registered = {entity.setting.field for entity in existing}

    def add_reported():
        entities = [entity for entity in setup_cloud_settings(platform, coordinator)
                    if entity.setting.field not in registered]
        if entities:
            registered.update(entity.setting.field for entity in entities)
            add_entities(entities)

    coordinator.entry.async_on_unload(coordinator.async_add_listener(add_reported))
