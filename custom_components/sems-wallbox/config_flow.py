"""Config flow for sems integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_SCAN_INTERVAL

from .const import (
    DOMAIN,
    SEMS_CONFIG_SCHEMA,
    CONF_STATION_ID,
    CONF_SCAN_INTERVAL_CHARGING,
    CONF_PLANT_ID,
    CONF_PRODUCT_MODEL,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DEFAULT_SCAN_INTERVAL_CHARGING,
)
from .sems_api import SemsApi

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    _LOGGER.debug("SEMS - Start validation config flow user input")
    api = SemsApi(hass, data[CONF_USERNAME], data[CONF_PASSWORD])

    authenticated = await hass.async_add_executor_job(api.test_authentication)
    if not authenticated:
        raise InvalidAuth

    # If you cannot connect:
    # throw CannotConnect
    # If the authentication is wrong:
    # InvalidAuth

    # Return info that you want to store in the config entry.
    return data


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for sems."""

    _LOGGER.debug("SEMS - new config flow")

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "OptionsFlowHandler":
        """Return the options flow handler."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=SEMS_CONFIG_SCHEMA)

        errors = {}

        try:
            info = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(title=info[CONF_STATION_ID], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=SEMS_CONFIG_SCHEMA, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options (polling intervals) for SEMS Wallbox."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_idle = int(self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_IDLE),
        ))
        current_charging = int(self.config_entry.options.get(
            CONF_SCAN_INTERVAL_CHARGING,
            DEFAULT_SCAN_INTERVAL_CHARGING,
        ))

        current_plant_id = self.config_entry.options.get(CONF_PLANT_ID, "")
        current_model = self.config_entry.options.get(CONF_PRODUCT_MODEL, "")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=current_idle): vol.All(
                    int, vol.Range(min=10, max=300)
                ),
                vol.Required(CONF_SCAN_INTERVAL_CHARGING, default=current_charging): vol.All(
                    int, vol.Range(min=5, max=120)
                ),
                vol.Optional(CONF_PLANT_ID, default=current_plant_id): str,
                vol.Optional(CONF_PRODUCT_MODEL, default=current_model): str,
            }),
        )
