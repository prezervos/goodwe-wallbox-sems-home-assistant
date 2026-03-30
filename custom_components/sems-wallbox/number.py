"""Support for number entity controlling GoodWe SEMS Wallbox charge power."""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SemsUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

NUMBER_VERSION = "0.3.2"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add numbers for passed config_entry in HA."""
    runtime = hass.data[DOMAIN][config_entry.entry_id]
    coordinator: SemsUpdateCoordinator = runtime["coordinator"]
    api = runtime["api"]

    _LOGGER.debug(
        "Setting up SemsNumber entities (version %s) for entry %s",
        NUMBER_VERSION,
        config_entry.entry_id,
    )

    entities: list[SemsNumber] = []
    for sn, data in coordinator.data.items():
        set_charge_power = data.get("set_charge_power")
        entities.append(SemsNumber(coordinator, sn, api, set_charge_power))

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
        _LOGGER.debug(
            "Creating SemsNumber (v%s) for Wallbox %s, initial value=%s",
            NUMBER_VERSION,
            self.sn,
            self._attr_native_value,
        )

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

    _DEFAULT_MIN = 4.2
    _DEFAULT_MAX = 11.0

    @property
    def native_min_value(self) -> float:
        """Return the minimum value, read from API data when available."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("min_charge_power")
        try:
            return float(v) if v is not None else self._DEFAULT_MIN
        except (TypeError, ValueError):
            return self._DEFAULT_MIN

    @property
    def native_max_value(self) -> float:
        """Return the maximum value, read from API data when available."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        v = data.get("max_charge_power")
        try:
            return float(v) if v is not None else self._DEFAULT_MAX
        except (TypeError, ValueError):
            return self._DEFAULT_MAX

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
        """Only available when chargeMode is Fast (0); disabled in PV modes."""
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data.get(self.sn, {}) or {}
        return data.get("chargeMode", 0) == 0

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data.get(self.sn, {}) or {}
        set_charge_power = data.get("set_charge_power")
        charge_mode = data.get("chargeMode")

        if charge_mode == 0:
            # Fast mode — accept the value the API reports.
            if set_charge_power is not None:
                try:
                    self._attr_native_value = float(set_charge_power)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "SemsNumber %s: invalid set_charge_power value %r from API",
                        self.sn,
                        set_charge_power,
                    )
        else:
            # PV mode — the device may report a stale / default set_charge_power.
            # Preserve the last user-set value so that switching back to Fast
            # restores it correctly.  Patch coordinator.data directly (no listeners
            # triggered) so select.py also reads the preserved value when it builds
            # the API payload for the next Fast-mode switch.
            if self._attr_native_value is not None:
                device = self.coordinator.data.get(self.sn)
                if device is not None:
                    device["set_charge_power"] = self._attr_native_value

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

    async def async_set_native_value(self, value: float) -> None:
        """Handle change from UI slider (only reachable in Fast mode)."""
        _LOGGER.debug(
            "Setting set_charge_power for SN=%s to %s",
            self.sn,
            value,
        )

        # 1) Optimistic UI update — also write the new power directly into
        # coordinator.data (without going through async_set_updated_data) so
        # that an in-flight select.py mode-switch call can detect it after its
        # own API call finishes and re-send with the correct power.
        # We deliberately avoid async_set_updated_data here: calling it would
        # trigger _handle_coordinator_update on the select entity, which could
        # see the optimistically-written chargeMode and prematurely clear
        # _pending_mode — causing the very revert we are trying to prevent.
        old_value = self._attr_native_value  # save before optimistic write for failure revert
        self._attr_native_value = float(value)
        device = self.coordinator.data.get(self.sn)
        if device is not None:
            device["set_charge_power"] = float(value)
        self.async_write_ha_state()

        # 2) Call SEMS API — always Fast mode (0), since entity is unavailable otherwise
        ok = await self.hass.async_add_executor_job(
            self.api.set_charge_mode,
            self.sn,
            0,
            value,
        )

        if not ok:
            # API call failed — revert optimistic value and coordinator.data
            # so the slider goes back to whatever the device actually has.
            _LOGGER.warning(
                "set_charge_mode failed for %s (power=%s), reverting optimistic value",
                self.sn,
                value,
            )
            if old_value is not None:
                self._attr_native_value = old_value
                device = self.coordinator.data.get(self.sn)
                if device is not None:
                    device["set_charge_power"] = old_value
            self.async_write_ha_state()

        # 3) Schedule refresh (non-blocking)
        self.hass.async_create_task(self.coordinator.async_request_refresh())
