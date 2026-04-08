"""Support for switch controlling an output of a GoodWe SEMS wallbox."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SemsUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SWITCH_VERSION = "0.3.4"

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
    coordinator: SemsUpdateCoordinator = runtime["coordinator"]
    api = runtime["api"]

    _LOGGER.debug(
        "Setting up SemsSwitch entities (version %s) for entry %s",
        SWITCH_VERSION,
        config_entry.entry_id,
    )

    entities: list[SemsSwitch] = []
    for sn, data in coordinator.data.items():
        start_status = data.get("startStatus")
        current_is_on = bool(start_status) if start_status is not None else False
        entities.append(SemsSwitch(coordinator, sn, api, current_is_on))
        entities.append(SemsMinimumPowerSwitch(coordinator, sn, api))

    async_add_entities(entities)


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
            "Creating SemsSwitch (v%s) for Wallbox %s, initial is_on=%s",
            SWITCH_VERSION,
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
        # startStatus is an explicit API boolean (True = charging active).
        # Fall back to old-API status string for Gen1 without startStatus.
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
        """Turn off charging."""
        _LOGGER.debug("Wallbox %s set to Off (optimistic UI + OFF grace)", self.sn)

        self._last_command_target = False
        self._last_command_ts = self.hass.loop.time()

        # Optimistic state update
        self._attr_is_on = False
        self.async_write_ha_state()

        # Optimistic immediate refresh, then a confirmed one 5 s after the command
        self.hass.async_create_task(self.coordinator.async_request_refresh())

        # Send command to SEMS API
        await self.hass.async_add_executor_job(self.api.change_status_gen2, self.sn, "stop")
        self.coordinator.schedule_delayed_refresh(5)

    async def async_turn_on(self, **kwargs):
        """Turn on charging."""
        _LOGGER.debug("Wallbox %s set to On (optimistic UI + ON grace)", self.sn)

        self._last_command_target = True
        self._last_command_ts = self.hass.loop.time()

        # Optimistic state update
        self._attr_is_on = True
        self.async_write_ha_state()

        # Optimistic immediate refresh, then a confirmed one 5 s after the command
        self.hass.async_create_task(self.coordinator.async_request_refresh())

        # Send command to SEMS API
        await self.hass.async_add_executor_job(self.api.change_status_gen2, self.sn, "start")
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


class SemsMinimumPowerSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable 'ensure minimum charging power' in PV priority mode.

    When enabled, the wallbox guarantees a minimum charge current from the grid
    even when PV production is insufficient. Only relevant in PV priority mode (mode=1);
    the entity is unavailable in other modes.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "ensure_minimum_charging_power"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SemsUpdateCoordinator, sn: str, api) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.api = api
        self.sn = sn
        _LOGGER.debug("Creating SemsMinimumPowerSwitch for wallbox %s", self.sn)

    @property
    def unique_id(self) -> str:
        return f"{self.sn}-switch-ensure-minimum-power"

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
        """Available in PV priority (mode 1) and PV & battery (mode 2)."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode") in (1, 2)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data.get(self.sn, {}) or {}
        return bool(data.get("ensure_minimum_charging_power", False))

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable minimum charging power guarantee."""
        _LOGGER.debug("SemsMinimumPowerSwitch %s: turning ON", self.sn)
        # Optimistic update
        data = self.coordinator.data.get(self.sn, {}) or {}
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, self.sn: {**data, "ensure_minimum_charging_power": True}}
        )
        ok = await self.hass.async_add_executor_job(
            self.api.set_charge_mode_gen2,
            self.sn,
            1,  # PV priority
            None,
            True,  # ensure_minimum_charging_power
        )
        if not ok:
            _LOGGER.warning("SemsMinimumPowerSwitch %s: turn ON failed, refreshing", self.sn)
        self.coordinator.schedule_delayed_refresh(5.0)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable minimum charging power guarantee."""
        _LOGGER.debug("SemsMinimumPowerSwitch %s: turning OFF", self.sn)
        # Optimistic update
        data = self.coordinator.data.get(self.sn, {}) or {}
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, self.sn: {**data, "ensure_minimum_charging_power": False}}
        )
        ok = await self.hass.async_add_executor_job(
            self.api.set_charge_mode_gen2,
            self.sn,
            1,  # PV priority
            None,
            False,  # ensure_minimum_charging_power
        )
        if not ok:
            _LOGGER.warning("SemsMinimumPowerSwitch %s: turn OFF failed, refreshing", self.sn)
        self.coordinator.schedule_delayed_refresh(5.0)
