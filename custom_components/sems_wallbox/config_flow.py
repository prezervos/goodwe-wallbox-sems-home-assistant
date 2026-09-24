"""Config flow for sems integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
    SelectSelector, SelectSelectorConfig,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, CONF_SCAN_INTERVAL

from .const import (
    DOMAIN,
    CONF_STATION_ID,
    CONF_SCAN_INTERVAL_CHARGING,
    CONF_PLANT_ID,
    CONF_PRODUCT_MODEL,
    CONF_CONNECTION_TYPE,
    CONN_TYPE_CLOUD,
    CONN_TYPE_MODBUS,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODBUS_DEVICE_ID,
    DEFAULT_MODBUS_PORT,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DEFAULT_SCAN_INTERVAL_CHARGING,
    CONF_PILE_GENERATION,
    CONF_RATED_POWER,
    CONF_DASHBOARD_FUNCTIONS,
    CONF_MORE_DEVICE_CONTROLS,
)
from .config_values import configured_identity
from .charge_mode_policy import CONF_REMEMBER_MODE, ModeVerificationError
from .sems_api import SemsApi
from .cloud_observation import CloudAuthenticationError

_LOGGER = logging.getLogger(__name__)

_STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_STEP_CONN_TYPE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_REMEMBER_MODE, default=False): bool,
        vol.Required(CONF_CONNECTION_TYPE, default=CONN_TYPE_CLOUD): SelectSelector(SelectSelectorConfig(
            options=[CONN_TYPE_CLOUD, CONN_TYPE_MODBUS, "native_tcp"],
            translation_key="connection_type",
        )),
    }
)

# Keep UI schemas serializable; normalize and reject blank hosts in the handlers.
_MODBUS_HOST = str
_MODBUS_PORT = vol.All(int, vol.Range(min=1, max=65535))
_MODBUS_DEVICE_ID = vol.All(int, vol.Range(min=0, max=255))

_STEP_MODBUS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODBUS_HOST): _MODBUS_HOST,
        vol.Optional(CONF_MODBUS_PORT, default=DEFAULT_MODBUS_PORT): _MODBUS_PORT,
        vol.Optional(CONF_MODBUS_DEVICE_ID, default=0): _MODBUS_DEVICE_ID,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for sems."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialise flow state."""
        self._connection_type: str = CONN_TYPE_CLOUD
        self._username: str = ""
        self._password: str = ""
        self._api: SemsApi | None = None
        self._plant_id: str | None = None
        self._plant_options: dict[str, str] = {}   # {id: display_name}
        self._charger_sn_to_model: dict[str, str] = {}  # {sn: model}
        self._charger_capabilities: dict[str, dict] = {}  # {sn: raw device info dict}
        self._charger_manual_error: str | None = None
        self._mode_settings = {CONF_REMEMBER_MODE: False}
        self._pending_sn: str = ""  # SN waiting for model confirmation

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "OptionsFlowHandler":
        """Return the options flow handler."""
        return OptionsFlowHandler()

    def _connection_change_busy(self, entry):
        """Prevent changing endpoint ownership while TCP or recovery is active."""
        runtime = self.hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        owner = runtime.get("coordinator")
        return owner is not None and (
            getattr(owner, "local", False)
            or getattr(owner, "transitioning", False)
            or getattr(getattr(owner, "endpoint", None), "journal", None) is not None
        )

    async def async_step_reauth(self, entry_data):
        """Reauthenticate the existing entry without changing its identity."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Validate replacement credentials before updating the existing entry."""
        entry = self._get_reauth_entry()
        return await self._async_connection_change(entry, user_input, reauth=True)

    async def async_step_reconfigure(self, user_input=None):
        """Change connection settings while keeping entity IDs and saved intent."""
        entry = self._get_reconfigure_entry()
        return await self._async_connection_change(entry, user_input, reauth=False)

    async def _async_connection_change(self, entry, user_input, *, reauth):
        """Validate connection settings with read-only calls before saving."""
        if not reauth and self._connection_change_busy(entry):
            return self.async_abort(reason="connection_change_busy")
        current = {**entry.data, **entry.options}
        native = current.get("native_enabled", False)
        modbus = current.get(CONF_CONNECTION_TYPE) == CONN_TYPE_MODBUS and not native
        if reauth and (modbus or not entry.data.get(CONF_USERNAME)):
            return self.async_abort(reason="cloud_credentials_required")
        errors = {}
        if user_input is not None and modbus and not reauth:
            if not user_input[CONF_MODBUS_HOST].strip():
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self._connection_schema(current, reauth=False),
                    errors={CONF_MODBUS_HOST: "connection_validation_failed"},
                )
        if user_input is not None:
            updates = {}
            try:
                if not reauth and native:
                    validated = await validate_native({
                        **current, **user_input,
                        CONF_STATION_ID: entry.data[CONF_STATION_ID],
                        "native_discover": True,
                    })
                    for key in ("native_host", "native_advertised_host",
                                "native_port", "native_ingress_peer"):
                        updates[key] = validated[key]
                elif not reauth and modbus:
                    from .wallbox_modbus import WallboxModbusClient

                    host = user_input[CONF_MODBUS_HOST].strip()
                    port = int(user_input[CONF_MODBUS_PORT])
                    device_id = int(user_input[CONF_MODBUS_DEVICE_ID])
                    if device_id == 0:
                        device_id = await self.hass.async_add_executor_job(
                            WallboxModbusClient.detect_device_id, host, port
                        )
                        if device_id is None:
                            raise CannotConnect
                    client = WallboxModbusClient(host, port, device_id)
                    try:
                        report = await self.hass.async_add_executor_job(client.read_all)
                    finally:
                        await self.hass.async_add_executor_job(client.close)
                    if not report or report.get("sn") != entry.data[CONF_STATION_ID]:
                        return self.async_show_form(
                            step_id="reconfigure",
                            data_schema=self._connection_schema(current, reauth=False),
                            errors={"base": "wrong_device"},
                        )
                    updates.update({
                        CONF_MODBUS_HOST: host, CONF_MODBUS_PORT: port,
                        CONF_MODBUS_DEVICE_ID: device_id,
                    })
                if entry.data.get(CONF_USERNAME) or reauth:
                    username = user_input.get(CONF_USERNAME, entry.data.get(CONF_USERNAME, "")).strip()
                    password = user_input.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD, "")
                    api = SemsApi(self.hass, username, password)
                    try:
                        if not await self.hass.async_add_executor_job(api.test_authentication):
                            raise InvalidAuth
                        # Login alone does not prove access to this same wallbox.
                        report = await self.hass.async_add_executor_job(
                            api.fetch_status_observation, entry.data[CONF_STATION_ID]
                        )
                    finally:
                        await self.hass.async_add_executor_job(api.close)
                    if not report or report.get("sn") != entry.data[CONF_STATION_ID]:
                        errors["base"] = "wrong_device"
                    else:
                        updates.update({CONF_USERNAME: username, CONF_PASSWORD: password})
                if not errors:
                    # Recheck after network awaits: a user may have enabled TCP meanwhile.
                    if not reauth and self._connection_change_busy(entry):
                        return self.async_abort(reason="connection_change_busy")
                    options = dict(entry.options)
                    for key, value in updates.items():
                        if key in options:
                            options[key] = value
                    changed = any(entry.data.get(k) != v for k, v in updates.items()) or options != entry.options
                    result = self.async_update_and_abort(
                        entry, data_updates=updates, options=options,
                    )
                    # Loaded entries already reload through their update listener.
                    # Retry unchanged credentials and unloaded entries exactly once.
                    runtime = self.hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
                    owner = runtime.get("coordinator")
                    if (reauth and not changed and runtime.get("connection_type") == "native_tcp"
                            and owner is not None):
                        await owner.async_reauthenticate(username, password)
                    elif not changed or not entry.update_listeners:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return result
            except (InvalidAuth, CloudAuthenticationError):
                errors["base"] = "invalid_auth"
            except (OSError, ValueError, CannotConnect):
                errors["base"] = "connection_validation_failed"
        return self.async_show_form(
            step_id="reauth_confirm" if reauth else "reconfigure",
            data_schema=self._connection_schema(current, reauth=reauth),
            errors=errors,
        )

    @staticmethod
    def _connection_schema(current, *, reauth):
        """Build connection-only fields; never prefill or echo a password."""
        fields = {}
        if not reauth and current.get("native_enabled", False):
            fields.update({
                vol.Required("native_host", default=current["native_host"]): str,
                vol.Required("native_advertised_host", default=current["native_advertised_host"]): str,
                vol.Required("native_port", default=current.get("native_port", 18899)):
                    vol.All(int, vol.Range(min=1024, max=65535)),
                vol.Optional("native_ingress_peer", default=current.get("native_ingress_peer", "")): str,
            })
        elif not reauth and current.get(CONF_CONNECTION_TYPE) == CONN_TYPE_MODBUS:
            fields.update({
                vol.Required(CONF_MODBUS_HOST, default=current[CONF_MODBUS_HOST]): _MODBUS_HOST,
                vol.Required(CONF_MODBUS_PORT, default=current.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT)):
                    _MODBUS_PORT,
                vol.Required(CONF_MODBUS_DEVICE_ID, default=current.get(CONF_MODBUS_DEVICE_ID, 0)):
                    _MODBUS_DEVICE_ID,
            })
        if current.get(CONF_USERNAME) or reauth:
            fields[vol.Required(CONF_USERNAME, default=current.get(CONF_USERNAME, ""))] = str
            fields[vol.Optional(CONF_PASSWORD)] = str
        return vol.Schema(fields)

    # ------------------------------------------------------------------
    # Step 0: choose connection type (Cloud or Local Modbus)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Select cloud, Modbus or native Socket A TCP."""
        if user_input is not None:
            self._mode_settings = {
                CONF_REMEMBER_MODE: user_input.get(CONF_REMEMBER_MODE, False),
            }
            self._connection_type = user_input[CONF_CONNECTION_TYPE]
            if self._connection_type == CONN_TYPE_MODBUS:
                return await self.async_step_modbus()
            if self._connection_type == "native_tcp":
                return await self.async_step_native()
            return await self.async_step_cloud_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=_STEP_CONN_TYPE_SCHEMA,
            errors={},
        )

    async def async_step_native(self, user_input=None):
        """Enroll a local device; configuration never redirects it automatically."""
        errors = {}
        if user_input is not None:
            try:
                if user_input.get("native_auto_fallback"):
                    return self.async_abort(reason="cloud_credentials_required")
                data = await validate_native(user_input)
                serial = data[CONF_STATION_ID]
                return await self._async_create_serial_entry(serial, {
                    **self._mode_settings, **data, "native_enabled": True})
            except (ValueError, OSError):
                errors["base"] = "native_configuration_failed"
        return self.async_show_form(step_id="native", data_schema=native_schema(user_input or {}), errors=errors)

    # ------------------------------------------------------------------
    # Step 1a: Local Modbus configuration
    # ------------------------------------------------------------------

    async def async_step_modbus(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Configure local Modbus TCP connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_MODBUS_HOST].strip()
            if not host:
                return self.async_show_form(
                    step_id="modbus",
                    data_schema=_STEP_MODBUS_SCHEMA,
                    errors={CONF_MODBUS_HOST: "connection_validation_failed"},
                )
            port = int(user_input.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT))
            device_id = int(user_input.get(CONF_MODBUS_DEVICE_ID, 0))

            from .wallbox_modbus import WallboxModbusClient

            # Auto-detect device ID when 0 is entered
            if device_id == 0:
                device_id = await self.hass.async_add_executor_job(
                    WallboxModbusClient.detect_device_id, host, port
                )
                if device_id is None:
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="modbus",
                        data_schema=_STEP_MODBUS_SCHEMA,
                        errors=errors,
                    )

            # Read all data to verify connection and get SN from device
            client = WallboxModbusClient(host, port, device_id)
            try:
                result = await self.hass.async_add_executor_job(client.read_all)
            except Exception:  # noqa: BLE001
                result = None

            if not result:
                errors["base"] = "cannot_connect"
            else:
                sn = result.get("sn") or host
                return await self._async_create_serial_entry(
                    sn,
                    {
                        **self._mode_settings,
                        CONF_CONNECTION_TYPE: CONN_TYPE_MODBUS,
                        CONF_MODBUS_HOST: host,
                        CONF_MODBUS_PORT: port,
                        CONF_MODBUS_DEVICE_ID: device_id,
                        CONF_STATION_ID: sn,
                    },
                )

        return self.async_show_form(
            step_id="modbus",
            data_schema=_STEP_MODBUS_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 1b: Cloud credentials (original first step, now step 1b)
    # ------------------------------------------------------------------

    async def async_step_cloud_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle cloud username + password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._api is not None:
                await self.hass.async_add_executor_job(self._api.close)
                self._api = None
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
            finally:
                if self._api is not api:
                    await self.hass.async_add_executor_job(api.close)

        return self.async_show_form(
            step_id="cloud_credentials",
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
            # First visit -- fetch from EU gateway.
            # Try centralized/page (EV_CHARGER) first -- works for both owners and
            # visitor/shared accounts.  Fall back to stations/page if needed.
            assert self._api is not None
            # Fetch all EV chargers across all plants (no stationId filter)
            all_chargers = await self.hass.async_add_executor_job(
                self._api.fetch_ev_chargers, None
            )
            _LOGGER.debug("SEMS config: discovered %d EV chargers (all plants)", len(all_chargers))
            for c in all_chargers:
                sid = c.get("stationId") or c.get("plantId")
                name = c.get("stationName") or c.get("name") or str(sid)
                if sid:
                    self._plant_options[str(sid)] = str(name)

            if not self._plant_options:
                # Fallback: try stations/page
                stations = await self.hass.async_add_executor_job(self._api.fetch_stations)
                _LOGGER.debug("SEMS config: discovered %d stations", len(stations))
                for s in stations:
                    sid = s.get("id") or s.get("stationId")
                    name = s.get("name") or s.get("stationName") or str(sid)
                    if sid:
                        self._plant_options[str(sid)] = str(name)

        if len(self._plant_options) == 0:
            # EU gateway not available or no plants -- skip to manual SN entry
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
            return await self._finish_or_model_step(sn)

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
                # Capture plant_id from charger record if not yet set (visitor accounts)
                if not self._plant_id:
                    self._plant_id = c.get("stationId") or None

        if len(charger_options) == 0:
            _LOGGER.info("SEMS config: no EV chargers discovered, using manual entry")
            self._charger_manual_error = "no_chargers_found"
            return await self.async_step_charger_manual()

        if len(charger_options) == 1:
            sn = next(iter(charger_options))
            _LOGGER.debug("SEMS config: auto-selected charger %s", sn)
            return await self._finish_or_model_step(sn)

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
            plant_id = (user_input.get(CONF_PLANT_ID) or "").strip()
            if not sn:
                errors[CONF_STATION_ID] = "invalid_serial"
            else:
                if plant_id:
                    self._plant_id = plant_id
                return await self._finish_or_model_step(sn)

        return self.async_show_form(
            step_id="charger_manual",
            data_schema=vol.Schema({
                vol.Required(CONF_STATION_ID): str,
                vol.Optional(CONF_PLANT_ID, default=""): str,
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 4: model entry (when not auto-discovered)
    # ------------------------------------------------------------------

    async def _finish_or_model_step(self, sn: str):
        """Create entry directly when model is known, otherwise fetch it, then ask."""
        self._pending_sn = sn
        # Always fetch device info to capture capabilities (pileGeneration, dashboardFunctions,
        # moreDeviceControls, ratedPower) regardless of whether the model is already known.
        if not self._charger_capabilities.get(sn):
            assert self._api is not None
            info = await self.hass.async_add_executor_job(self._api.fetch_device_info, sn)
            if info:
                self._charger_capabilities[sn] = info
                model = (info.get("productModel") or "").strip()
                if model and not self._charger_sn_to_model.get(sn):
                    _LOGGER.debug("SEMS config: auto-discovered model %s for %s", model, sn)
                    self._charger_sn_to_model[sn] = model
        if self._charger_sn_to_model.get(sn):
            return await self._async_create_serial_entry(sn, self._build_entry_data(sn))
        return await self.async_step_model()

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask for product model when it could not be auto-discovered."""
        if user_input is not None:
            model = (user_input.get(CONF_PRODUCT_MODEL) or "").strip()
            if model:
                self._charger_sn_to_model[self._pending_sn] = model
            return await self._async_create_serial_entry(
                self._pending_sn, self._build_entry_data(self._pending_sn)
            )

        return self.async_show_form(
            step_id="model",
            data_schema=vol.Schema({
                vol.Optional(CONF_PRODUCT_MODEL, default=""): str,
            }),
            errors={},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _async_create_serial_entry(self, serial, data):
        """Protect all transports, including entries created before unique IDs."""
        if not serial.strip():
            return self.async_abort(reason="invalid_serial")
        if any(entry.data.get(CONF_STATION_ID) == serial
               for entry in self._async_current_entries()):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=serial, data=data)

    @callback
    def async_remove(self):
        """Release discovery resources when HA completes or aborts this flow."""
        if self._api is not None:
            api, self._api = self._api, None
            self.hass.async_add_executor_job(api.close)

    def _build_entry_data(self, sn: str) -> dict:
        """Build the config entry data dict from discovered (or manual) values."""
        data: dict[str, Any] = {
            **self._mode_settings,
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_STATION_ID: sn,
        }
        if self._plant_id:
            data[CONF_PLANT_ID] = self._plant_id
        model = self._charger_sn_to_model.get(sn, "")
        if model:
            data[CONF_PRODUCT_MODEL] = model
        # Store device capabilities (pileGeneration, ratedPower, dashboardFunctions, moreDeviceControls)
        info = self._charger_capabilities.get(sn, {})
        if info:
            generation = str(info.get("pileGeneration") or "")
            if generation:
                data[CONF_PILE_GENERATION] = generation
            rated = info.get("ratedPower")
            if rated is not None:
                data[CONF_RATED_POWER] = rated
            dashboard = info.get("dashboardFunctions")
            if isinstance(dashboard, list):
                data[CONF_DASHBOARD_FUNCTIONS] = dashboard
            more = info.get("moreDeviceControls")
            if isinstance(more, list):
                data[CONF_MORE_DEVICE_CONTROLS] = more
        return data


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate there is invalid auth."""


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options (polling intervals) for SEMS Wallbox."""

    async def _async_save_options(self, data, *, step_id):
        """Adopt the current mode only when restoration is explicitly enabled."""
        previously_enabled = self.config_entry.options.get(
            CONF_REMEMBER_MODE, self.config_entry.data.get(CONF_REMEMBER_MODE, False)
        )
        if data.get(CONF_REMEMBER_MODE, previously_enabled) and not previously_enabled:
            runtime = getattr(getattr(self, "hass", None), "data", {}).get(DOMAIN, {}).get(
                getattr(self.config_entry, "entry_id", ""), {}
            )
            owner = runtime.get("coordinator")
            policy = getattr(owner, "charge_mode_policy", None)
            try:
                if policy is None or not owner.last_update_success:
                    raise ModeVerificationError("Current wallbox mode is unavailable")
                await policy.async_adopt_current_mode()
            except (OSError, ValueError, RuntimeError):
                form = await getattr(self, "async_step_" + step_id)()
                form["errors"] = {"base": "mode_capture_failed"}
                return form
        return self.async_create_entry(title="", data=data)

    async def async_step_init(
        self,
        user_input=None,
    ):
        """Manage the options."""
        if user_input is not None:
            user_input = dict(user_input)
            for key in (CONF_PLANT_ID, CONF_PRODUCT_MODEL):
                if key in user_input:
                    user_input[key] = user_input[key].strip()
            if not user_input.get("native_enabled") and self.config_entry.data.get("native_enabled") and not self.config_entry.data.get(CONF_USERNAME):
                return self.async_abort(reason="cloud_credentials_required")
            if user_input.get("native_enabled"):
                self._pending_native_options = {**self.config_entry.options, **user_input}
                return await self.async_step_native()
            return await self._async_save_options(
                {**self.config_entry.options, **user_input}, step_id="init"
            )

        current_idle = int(self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_IDLE),
        ))
        current_charging = int(self.config_entry.options.get(
            CONF_SCAN_INTERVAL_CHARGING,
            self.config_entry.data.get(CONF_SCAN_INTERVAL_CHARGING, DEFAULT_SCAN_INTERVAL_CHARGING),
        ))

        runtime = getattr(getattr(self, "hass", None), "data", {}).get(DOMAIN, {}).get(
            getattr(self.config_entry, "entry_id", ""), {}
        )
        coordinator = runtime.get("coordinator")
        detected = getattr(coordinator, "resolved_device_identity", {})

        def current_identity(key):
            if key in self.config_entry.options:
                # An empty override requests detection; keep that choice visible.
                return configured_identity(self.config_entry, key) or ""
            return configured_identity(self.config_entry, key) or (detected.get(key) or "").strip()

        current_plant_id = current_identity(CONF_PLANT_ID)
        current_model = current_identity(CONF_PRODUCT_MODEL)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional("native_enabled", default=self.config_entry.options.get(
                    "native_enabled", self.config_entry.data.get("native_enabled", False))): bool,
                vol.Optional(CONF_REMEMBER_MODE, default=self.config_entry.options.get(
                    CONF_REMEMBER_MODE, self.config_entry.data.get(CONF_REMEMBER_MODE, False))): bool,
                vol.Required(CONF_SCAN_INTERVAL, default=current_idle): NumberSelector(
                    NumberSelectorConfig(min=10, max=300, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_SCAN_INTERVAL_CHARGING, default=current_charging): NumberSelector(
                    NumberSelectorConfig(min=5, max=120, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_PLANT_ID, default=current_plant_id): str,
                vol.Optional(CONF_PRODUCT_MODEL, default=current_model): str,
            }),
        )


    async def async_step_native(self, user_input=None):
        """Add native settings to the existing entry and keep its entity registry."""
        errors = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            try:
                if user_input.get("native_auto_fallback") and not current.get(CONF_USERNAME):
                    return self.async_abort(reason="cloud_credentials_required")
                data = await validate_native({**user_input, CONF_STATION_ID: self.config_entry.data[CONF_STATION_ID]})
                data.pop(CONF_STATION_ID)
                return await self._async_save_options(
                    {**self._pending_native_options, **data}, step_id="native"
                )
            except (ValueError, OSError):
                errors["base"] = "native_configuration_failed"
        return self.async_show_form(step_id="native", data_schema=native_schema(
            {**current, **(user_input or {})}, include_serial=False), errors=errors)


def native_schema(current, *, include_serial=True):
    """Build explicit LAN settings without site-specific defaults."""
    fields = {
        vol.Optional("native_auto_fallback", default=current.get("native_auto_fallback", False)): bool,
        vol.Required("native_host", default=current.get("native_host", "")): str,
        vol.Required("native_advertised_host", default=current.get("native_advertised_host", "")): str,
        vol.Required("native_port", default=current.get("native_port", 18899)): vol.All(int, vol.Range(min=1024, max=65535)),
        vol.Optional("native_discover", default=True): bool,
        vol.Optional("native_ingress_peer", default=current.get("native_ingress_peer", "")): str,
    }
    if include_serial:
        fields[vol.Optional(CONF_STATION_ID, default=current.get(CONF_STATION_ID, ""))] = str
    return vol.Schema(fields)


async def validate_native(user_input):
    """Validate manual configuration and optionally enroll through read-only discovery."""
    import ipaddress
    from .native_discovery import async_discover
    from .native_endpoint import local_endpoint
    from .native_protocol import check_serial
    data = dict(user_input)
    peer = ipaddress.IPv4Address(data["native_host"].strip())
    if peer.is_unspecified or peer.is_multicast or peer.is_loopback:
        raise ValueError("A unicast wallbox LAN address is required")
    data["native_host"] = str(peer)
    ingress = data.get("native_ingress_peer", "").strip()
    data["native_ingress_peer"] = ingress
    if ingress:
        proxy = ipaddress.IPv4Address(ingress)
        if proxy.is_unspecified or proxy.is_multicast:
            raise ValueError("A specific trusted TCP proxy address is required")
        data["native_ingress_peer"] = str(proxy)
    data["native_advertised_host"] = data["native_advertised_host"].strip()
    local_endpoint(data["native_advertised_host"], data["native_port"])
    serial = data.get(CONF_STATION_ID, "").strip()
    if data.pop("native_discover", True):
        found = await async_discover(data["native_host"], timeout=2)
        if len(found) != 1 or (serial and found[0].serial != serial):
            raise ValueError("Discovery did not identify the configured wallbox")
        serial = found[0].serial
    check_serial(serial)
    from .native_power_limits import power_bounds
    power_bounds(serial)  # Reject unsupported profiles before creating the entry.
    data.pop("native_initial_power", None)
    data[CONF_STATION_ID] = serial
    return data
