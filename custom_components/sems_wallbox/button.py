"""Button entities for the GoodWe Wallbox integration."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONN_TYPE_MODBUS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities from a config entry."""
    runtime = hass.data[DOMAIN][config_entry.entry_id]
    conn_type = runtime.get("connection_type", "cloud")
    if conn_type == "native_tcp":
        from .native_entities import setup_platform
        setup_platform("button", runtime["coordinator"], async_add_entities)
        return

    if conn_type == CONN_TYPE_MODBUS:
        coordinator = runtime["coordinator"]
        entities = [
            ModbusRefreshButton(coordinator, sn)
            for sn in coordinator.data
        ]
        async_add_entities(entities)


class ModbusRefreshButton(CoordinatorEntity, ButtonEntity):
    """Button that triggers an immediate Modbus data refresh."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "modbus_refresh"

    def __init__(self, coordinator, sn: str) -> None:
        super().__init__(coordinator)
        self.sn = sn

    @property
    def unique_id(self) -> str:
        return f"{self.sn}_modbus_refresh"

    @property
    def device_info(self):
        data = self.coordinator.data.get(self.sn, {}) or {}
        return {
            "identifiers": {(DOMAIN, self.sn)},
            "name": data.get("name") or f"GoodWe Wallbox {self.sn}",
            "manufacturer": "GoodWe",
        }

    async def async_press(self) -> None:
        """Trigger an immediate coordinator refresh."""
        _LOGGER.debug("Modbus refresh button pressed for %s", self.sn)
        await self.coordinator.async_request_refresh()
