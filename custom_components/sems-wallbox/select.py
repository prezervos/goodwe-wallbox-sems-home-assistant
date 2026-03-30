"""Support for select entity controlling GoodWe SEMS Wallbox charge mode."""

import logging
import time

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SemsUpdateCoordinator

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
    entity_category=EntityCategory.CONFIG,
    translation_key="charge_mode",
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the inverter select entities from a config entry."""
    runtime = hass.data[DOMAIN][config_entry.entry_id]
    coordinator: SemsUpdateCoordinator = runtime["coordinator"]
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

    async_add_entities(entities)


class InverterOperationModeEntity(CoordinatorEntity, SelectEntity):
    """Entity representing the wallbox charge mode."""

    _attr_should_poll = False
    _attr_has_entity_name = True

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
        # Guard against re-entrant async_set_updated_data calls.
        self._restoring: bool = False
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

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option not in _OPTION_TO_MODE:
            _LOGGER.warning(
                "Unknown operation mode option %s for wallbox %s",
                option,
                self.sn,
            )
            return

        mode = _OPTION_TO_MODE[option]

        _LOGGER.debug(
            "Setting operation mode for wallbox %s to %s (mode=%s)",
            self.sn,
            option,
            mode,
        )

        # Optimistic UI update for select entity
        old_option = self._attr_current_option  # save before optimistic write for failure revert
        self._attr_current_option = option
        self.async_write_ha_state()

        # When switching TO Fast mode (0) the API requires charge_power in the
        # payload, otherwise it silently ignores the command.
        # For PV modes (1, 2) we must NOT send charge_power — doing so causes
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

        # Immediately propagate new chargeMode (and the actual charge_power
        # we are about to send) into coordinator.data so that:
        #   a) dependent entities (number slider) react before the API call finishes.
        #   b) the clamped / resolved charge_power is visible, so a later write
        #      by number.py can be distinguished from a clamping artefact.
        current_device = self.coordinator.data.get(self.sn, {}) or {}
        updated_device = {**current_device, "chargeMode": mode}
        if mode == 0:
            updated_device["set_charge_power"] = charge_power
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, self.sn: updated_device}
        )
        # Set pending AFTER async_set_updated_data so the synchronous
        # _handle_coordinator_update call inside it doesn't clear the flag.
        self._pending_mode = mode
        self._pending_mode_set_at = time.monotonic()

        ok = await self.hass.async_add_executor_job(
            self.api.set_charge_mode,
            self.sn,
            mode,
            charge_power,
        )

        if not ok:
            # API call failed (timeout, network error, auth failure).
            # Cancel the pending guard and revert the optimistic UI state so
            # the select shows whatever the coordinator last reported.
            _LOGGER.warning(
                "set_charge_mode failed for %s (mode=%s), reverting optimistic UI state",
                self.sn,
                mode,
            )
            self._pending_mode = None
            self._attr_current_option = old_option
            self.async_write_ha_state()
            self.hass.async_create_task(self.coordinator.async_request_refresh())
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"set_charge_mode_failed_{option}",
            )

        # Superseded-call guard: discard this call's result if a newer
        # dispatch has taken over.  Two cases:
        #
        #   a) _pending_mode is set to a *different* mode: a newer dispatch
        #      started while we were awaiting the API and hasn't finished yet.
        #
        #   b) coordinator.data["chargeMode"] != mode: a poll (or optimistic
        #      update from a newer dispatch) has already confirmed a different
        #      mode.  This catches the case where _pending_mode was already
        #      cleared by a poll confirmation BEFORE a long (e.g. 30 s
        #      timed-out) call finally returned.
        current_device_supersede = self.coordinator.data.get(self.sn, {}) or {}
        current_chargemode = current_device_supersede.get("chargeMode")
        if (self._pending_mode is not None and self._pending_mode != mode) or current_chargemode != mode:
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
        # (last write wins — both calls use the same set_charge_mode endpoint).
        if mode == 0:
            current_data = self.coordinator.data.get(self.sn, {}) or {}
            latest_raw = current_data.get("set_charge_power")
            try:
                latest_power = float(latest_raw) if latest_raw is not None else None
            except (TypeError, ValueError):
                latest_power = None
            if latest_power is not None and latest_power != charge_power:
                _LOGGER.debug(
                    "Power changed during mode switch for %s (%.2f → %.2f kW), re-sending",
                    self.sn,
                    charge_power,
                    latest_power,
                )
                await self.hass.async_add_executor_job(
                    self.api.set_charge_mode,
                    self.sn,
                    0,
                    latest_power,
                )

        # Schedule a full refresh to confirm state from the API.
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Guard: skip processing when we ourselves triggered async_set_updated_data
        # to restore the pending mode (prevents re-entrant recursion).
        if self._restoring:
            return

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
                # API confirmed the change — stop guarding
                _LOGGER.debug(
                    "Pending mode %s confirmed by API for wallbox %s",
                    self._pending_mode,
                    self.sn,
                )
                self._pending_mode = None
            else:
                # Poll returned the old mode — API hasn't applied the change yet.
                # Restore the pending chargeMode in coordinator.data so that ALL
                # dependent entities (number slider, etc.) keep the correct state.
                _LOGGER.debug(
                    "Ignoring poll chargeMode=%s for wallbox %s while pending mode=%s",
                    mode,
                    self.sn,
                    self._pending_mode,
                )
                self._restoring = True
                current = dict(self.coordinator.data.get(self.sn, {}))
                current["chargeMode"] = self._pending_mode
                self.coordinator.async_set_updated_data(
                    {**self.coordinator.data, self.sn: current}
                )
                self._restoring = False
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
