"""The sems-wallbox integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, CONF_PLANT_ID, CONF_PRODUCT_MODEL
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
from .sems_api import SemsApi
from .coordinator import SemsUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the sems component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up sems from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    api = SemsApi(hass, username, password)

    # Configure gen2 (SEMS Plus) plant info from options/data if provided.
    # strip() + or None so empty-string values from the OptionsFlow are treated as unset.
    plant_id = (entry.options.get(CONF_PLANT_ID) or entry.data.get(CONF_PLANT_ID) or "").strip() or None
    product_model = (entry.options.get(CONF_PRODUCT_MODEL) or entry.data.get(CONF_PRODUCT_MODEL) or "").strip() or None
    _LOGGER.debug(
        "SEMS setup: plant_id=%r product_model=%r (from options=%r data=%r)",
        plant_id,
        product_model,
        entry.options,
        {k: v for k, v in entry.data.items() if k not in (CONF_PASSWORD,)},
    )
    api.configure_gen2(plant_id, product_model)

    coordinator = SemsUpdateCoordinator(hass, entry, api)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    # Reload on options change (e.g. scan_interval)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update (e.g. scan_interval change)."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
