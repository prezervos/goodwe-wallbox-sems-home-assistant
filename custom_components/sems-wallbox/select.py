"""Support for select entity controlling GoodWe SEMS Wallbox charge mode."""

import logging

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SemsUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

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

        # Optimistic UI update
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

        await self.hass.async_add_executor_job(
            self.api.set_charge_mode,
            self.sn,
            mode,
            charge_power,
        )

        # Optimistically propagate the new chargeMode to coordinator data so
        # dependent entities (e.g. the charge power number slider) update their
        # available state immediately instead of waiting for the next poll.
        current_device = self.coordinator.data.get(self.sn, {}) or {}
        self.coordinator.async_set_updated_data(
            {**self.coordinator.data, self.sn: {**current_device, "chargeMode": mode}}
        )

        # Schedule a full refresh to sync any other changes from the API
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        inverter = self.coordinator.data.get(self.sn, {}) or {}
        mode = inverter.get("chargeMode")
        _LOGGER.debug(
            "Coordinator update for wallbox %s: chargeMode=%s",
            self.sn,
            mode,
        )

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
