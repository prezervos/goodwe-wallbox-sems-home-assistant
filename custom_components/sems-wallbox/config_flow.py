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
    CONF_STATION_ID,
    CONF_SCAN_INTERVAL_CHARGING,
    CONF_PLANT_ID,
    CONF_PRODUCT_MODEL,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DEFAULT_SCAN_INTERVAL_CHARGING,
)
from .sems_api import SemsApi

_LOGGER = logging.getLogger(__name__)

_STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for sems."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialise flow state."""
        self._username: str = ""
        self._password: str = ""
        self._api: SemsApi | None = None
        self._plant_id: str | None = None
        self._plant_options: dict[str, str] = {}   # {id: display_name}
        self._charger_sn_to_model: dict[str, str] = {}  # {sn: model}
        self._charger_manual_error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "OptionsFlowHandler":
        """Return the options flow handler."""
        return OptionsFlowHandler()

    # ------------------------------------------------------------------
    # Step 1: credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the initial step — just username + password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api = SemsApi(self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                authenticated = await self.hass.async_add_executor_job(api.test_authentication)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("SEMS config: auth check raised")
                errors["base"] = "cannot_connect"
            else:
                if not authenticated:
                    errors["base"] = "invalid_auth"
                else:
                    self._username = user_input[CONF_USERNAME]
                    self._password = user_input[CONF_PASSWORD]
                    self._api = api
                    # Pre-fetch web token so discovery calls work immediately
                    await self.hass.async_add_executor_job(api._ensure_web_token)
                    return await self.async_step_plant()

        return self.async_show_form(
            step_id="user",
            data_schema=_STEP_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: plant / station selection
    # ------------------------------------------------------------------

    async def async_step_plant(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pick a plant/station (skipped automatically when there is only one)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._plant_id = user_input[CONF_PLANT_ID]
            return await self.async_step_charger()

        if not self._plant_options:
            # First visit — fetch from EU gateway
            assert self._api is not None
            stations = await self.hass.async_add_executor_job(self._api.fetch_stations)
            _LOGGER.debug("SEMS config: discovered %d stations", len(stations))
            for s in stations:
                sid = (
                    s.get("id")
                    or s.get("stationId")
                    or s.get("plantId")
                    or s.get("powerStationId")
                )
                name = (
                    s.get("name")
                    or s.get("stationName")
                    or s.get("plantName")
                    or str(sid)
                )
                if sid:
                    self._plant_options[str(sid)] = str(name)

        if len(self._plant_options) == 0:
            # EU gateway not available or no plants — skip to manual SN entry
            _LOGGER.info("SEMS config: no stations discovered, using manual entry")
            return await self.async_step_charger_manual()

        if len(self._plant_options) == 1:
            # Auto-select the only plant
            self._plant_id = next(iter(self._plant_options))
            _LOGGER.debug("SEMS config: auto-selected plant %s", self._plant_id)
            return await self.async_step_charger()

        return self.async_show_form(
            step_id="plant",
            data_schema=vol.Schema(
                {vol.Required(CONF_PLANT_ID): vol.In(self._plant_options)}
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3a: charger selection (dropdown, when multiple found)
    # ------------------------------------------------------------------

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pick an EV charger from the discovered list."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sn = user_input[CONF_STATION_ID]
            return self.async_create_entry(title=sn, data=self._build_entry_data(sn))

        assert self._api is not None
        chargers = await self.hass.async_add_executor_job(
            self._api.fetch_ev_chargers, self._plant_id
        )
        _LOGGER.debug("SEMS config: discovered %d EV chargers", len(chargers))

        charger_options: dict[str, str] = {}
        for c in chargers:
            sn = (
                c.get("sn")
                or c.get("serialNumber")
                or c.get("deviceSn")
                or c.get("sno")
                or ""
            ).strip()
            model = (
                c.get("model")
                or c.get("deviceModel")
                or c.get("productModel")
                or c.get("type")
                or ""
            ).strip()
            name = (c.get("name") or c.get("deviceName") or sn).strip()
            if sn:
                label = f"{name} ({model})" if model else name
                charger_options[sn] = label
                self._charger_sn_to_model[sn] = model

        if len(charger_options) == 0:
            _LOGGER.info("SEMS config: no EV chargers discovered, using manual entry")
            self._charger_manual_error = "no_chargers_found"
            return await self.async_step_charger_manual()

        if len(charger_options) == 1:
            sn = next(iter(charger_options))
            _LOGGER.debug("SEMS config: auto-selected charger %s", sn)
            return self.async_create_entry(title=sn, data=self._build_entry_data(sn))

        return self.async_show_form(
            step_id="charger",
            data_schema=vol.Schema(
                {vol.Required(CONF_STATION_ID): vol.In(charger_options)}
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3b: manual SN entry (fallback when discovery finds nothing)
    # ------------------------------------------------------------------

    async def async_step_charger_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Manual wallbox serial-number entry (fallback when discovery fails)."""
        errors: dict[str, str] = {}
        if self._charger_manual_error:
            errors["base"] = self._charger_manual_error
            self._charger_manual_error = None

        if user_input is not None:
            sn = user_input[CONF_STATION_ID].strip()
            return self.async_create_entry(title=sn, data=self._build_entry_data(sn))

        return self.async_show_form(
            step_id="charger_manual",
            data_schema=vol.Schema({vol.Required(CONF_STATION_ID): str}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_entry_data(self, sn: str) -> dict:
        """Build the config entry data dict from discovered (or manual) values."""
        data: dict[str, Any] = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_STATION_ID: sn,
        }
        if self._plant_id:
            data[CONF_PLANT_ID] = self._plant_id
        model = self._charger_sn_to_model.get(sn, "")
        if model:
            data[CONF_PRODUCT_MODEL] = model
        return data


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate there is invalid auth."""


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options (polling intervals) for SEMS Wallbox."""

    async def async_step_init(
        self,
        user_input=None,
    ):
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
