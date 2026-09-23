"""The sems_wallbox integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_PLANT_ID,
    CONF_PRODUCT_MODEL,
    CONF_STATION_ID,
    CONF_CONNECTION_TYPE,
    CONN_TYPE_MODBUS,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODBUS_DEVICE_ID,
    DEFAULT_MODBUS_PORT,
    DEFAULT_MODBUS_DEVICE_ID,
    CONF_PILE_GENERATION,
    CONF_RATED_POWER,
    CONF_DASHBOARD_FUNCTIONS,
    CONF_MORE_DEVICE_CONTROLS,
)
from homeassistant.helpers.storage import Store
from .charge_mode_policy import ChargeModePolicy, CONF_REMEMBER_MODE
from .config_values import configured_identity
from .charge_mode_adapter import ModeTransportAdapter
from .sems_api import SemsApi
from .coordinator import SemsUpdateCoordinator
from .modbus_coordinator import ModbusUpdateCoordinator
from .wallbox_modbus import WallboxModbusClient

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)


PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
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
    conn_type = entry.data.get(CONF_CONNECTION_TYPE, "cloud")
    if entry.options.get("native_enabled", entry.data.get("native_enabled", False)):
        return await _async_setup_native(hass, entry)

    if conn_type == CONN_TYPE_MODBUS:
        return await _async_setup_modbus(hass, entry)
    return await _async_setup_cloud(hass, entry)


async def _async_setup_modbus(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration using local Modbus TCP."""
    host = entry.data[CONF_MODBUS_HOST]
    port = int(entry.data.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT))
    device_id = int(entry.data.get(CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID))

    client = WallboxModbusClient(
        host, port, device_id, expected_serial=entry.data[CONF_STATION_ID]
    )
    coordinator = ModbusUpdateCoordinator(hass, entry, client)
    await _async_setup_mode_policy(hass, entry, coordinator, client, modbus=True)

    await coordinator.async_config_entry_first_refresh()
    if coordinator.charge_mode_policy.enabled:
        data = coordinator.data.get(entry.data[CONF_STATION_ID], {}) or {}
        await coordinator.charge_mode_policy.async_seed_power(data.get("set_charge_power"))

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "modbus_client": client,
        "connection_type": CONN_TYPE_MODBUS,
    }

    entry.async_on_unload(entry.add_update_listener(update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_setup_cloud(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration using the SEMS cloud API (original path)."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    api = _create_cloud_api(hass, entry, username, password)

    # Configure gen2 (SEMS Plus) plant info from options/data if provided.
    # strip() + or None so empty-string values from the OptionsFlow are treated as unset.
    plant_id = configured_identity(entry, CONF_PLANT_ID)
    product_model = configured_identity(entry, CONF_PRODUCT_MODEL)
    _LOGGER.debug(
        "SEMS setup: plant configured=%s product_model=%r",
        bool(plant_id),
        product_model,
    )
    api.configure_gen2(plant_id, product_model)

    # If model wasn't known at config time, try to fetch it from the EU gateway.
    if plant_id and not product_model:
        station_id = entry.data.get(CONF_STATION_ID) or entry.options.get(CONF_STATION_ID) or ""
        if station_id:
            info = await hass.async_add_executor_job(api.fetch_device_info, station_id)
            discovered_model = (info.get("productModel") or "").strip() or None
            if discovered_model:
                _LOGGER.debug("SEMS setup: auto-discovered model=%r for sn=%r", discovered_model, station_id)
                api.configure_gen2(plant_id, discovered_model)

    coordinator = SemsUpdateCoordinator(hass, entry, api)
    await _async_setup_mode_policy(hass, entry, coordinator, api, modbus=False)

    await coordinator.async_config_entry_first_refresh()
    if coordinator.charge_mode_policy.enabled:
        data = coordinator.data.get(entry.data[CONF_STATION_ID], {}) or {}
        await coordinator.charge_mode_policy.async_seed_power(data.get("set_charge_power"))

    # Build capabilities dict from stored config entry data
    capabilities: dict = {
        "pile_generation": entry.data.get(CONF_PILE_GENERATION, ""),
        "rated_power": entry.data.get(CONF_RATED_POWER),
        "dashboard_functions": list(entry.data.get(CONF_DASHBOARD_FUNCTIONS) or []),
        "more_device_controls": list(entry.data.get(CONF_MORE_DEVICE_CONTROLS) or []),
    }

    # Backward compatibility: if capabilities weren't stored at config time (old entry),
    # fetch them from the API now. The web token is already valid from the first refresh.
    if (not capabilities["pile_generation"]
            or (not capabilities["dashboard_functions"]
                and not capabilities["more_device_controls"])):
        station_id = entry.data.get(CONF_STATION_ID, "")
        if station_id:
            _LOGGER.debug(
                "SEMS setup: capabilities not in entry data, fetching from API for %s",
                station_id,
            )
            info = await hass.async_add_executor_job(api.fetch_device_info, station_id)
            if info:
                capabilities["pile_generation"] = (
                    capabilities["pile_generation"] or str(info.get("pileGeneration") or "")
                )
                capabilities["rated_power"] = (
                    capabilities["rated_power"] or info.get("ratedPower")
                )
                capabilities["dashboard_functions"] = (
                    capabilities["dashboard_functions"] or list(info.get("dashboardFunctions") or [])
                )
                capabilities["more_device_controls"] = (
                    capabilities["more_device_controls"] or list(info.get("moreDeviceControls") or [])
                )

    _LOGGER.debug(
        "SEMS setup: capabilities for %s: gen=%s rated=%s dashboard=%s more=%s",
        entry.data.get(CONF_STATION_ID),
        capabilities["pile_generation"],
        capabilities["rated_power"],
        capabilities["dashboard_functions"],
        capabilities["more_device_controls"],
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "capabilities": capabilities,
    }

    # Reload on options change (e.g. scan_interval)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _start_cloud_push(hass, entry, coordinator, api)
    return True


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update (e.g. scan_interval change)."""
    runtime = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
    previous = runtime.get("config_snapshot")
    current = (dict(config_entry.data), dict(config_entry.options))
    if runtime.get("connection_type") == "native_tcp" and previous is not None:
        old_data, old_options = previous
        credentials = {CONF_USERNAME, CONF_PASSWORD}
        changed = {key for key in old_data.keys() | current[0].keys()
                   if old_data.get(key) != current[0].get(key)}
        if changed and changed <= credentials and old_options == current[1]:
            await runtime["coordinator"].async_reauthenticate(
                config_entry.data[CONF_USERNAME], config_entry.data[CONF_PASSWORD]
            )
            runtime["config_snapshot"] = current
            return
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = hass.data[DOMAIN].get(entry.entry_id, {})
    push = getattr(runtime.get("coordinator"), "cloud_push", None)
    if push is not None:
        await push.close()
    if runtime.get("connection_type") == "native_tcp":
        await runtime["coordinator"].async_shutdown()
    policy = getattr(runtime.get("coordinator"), "charge_mode_policy", None)
    if policy is not None:
        await policy.async_close()
    # A disabled entry can skip some/all platforms during setup. Native runtime
    # records only platforms created by that setup, excluding prior HA objects.
    loaded_platforms = runtime.get("loaded_platforms", PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, loaded_platforms)
    if unload_ok:
        runtime = hass.data[DOMAIN].pop(entry.entry_id, {})
        modbus_client = runtime.get("modbus_client")
        if modbus_client is not None:
            await hass.async_add_executor_job(modbus_client.close)

    return unload_ok


async def _async_setup_mode_policy(hass, entry, coordinator, client, *, modbus):
    """Load durable user intent without writing any wallbox settings."""
    enabled = entry.options.get(CONF_REMEMBER_MODE, entry.data.get(CONF_REMEMBER_MODE, False))
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.charge_mode")
    policy = ChargeModePolicy(
        ModeTransportAdapter(hass, entry.data[CONF_STATION_ID], client, modbus=modbus),
        store, enabled=enabled)
    await policy.async_load()
    coordinator.charge_mode_policy = policy


async def _async_setup_native(hass, entry):
    """Enable supported cloud/TCP entities without replacing the config entry."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from .native_coordinator import NativeCoordinator
    cloud = None
    if entry.data.get(CONF_USERNAME) and entry.data.get(CONF_PASSWORD):
        cloud = _create_cloud_api(hass, entry, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
        plant = configured_identity(entry, CONF_PLANT_ID)
        model = configured_identity(entry, CONF_PRODUCT_MODEL)
        cloud.configure_gen2(plant, model)
    coordinator = NativeCoordinator(hass, entry, cloud)
    try:
        await coordinator.connection_intent.async_load()
        missing_capabilities = (
            not entry.data.get(CONF_DASHBOARD_FUNCTIONS)
            or not entry.data.get(CONF_MORE_DEVICE_CONTROLS)
        )
        if (cloud is not None and (not model or missing_capabilities)
                and not coordinator.connection_intent.manual_tcp
                and not coordinator.connection_intent.automatic_tcp):
            # Reuse one discovery response for model and legacy capabilities.
            # A saved TCP startup must not depend on a cloud metadata request.
            info = await coordinator.cloud_settings.discover_capabilities()
            if not model:
                cloud.configure_gen2(plant, info.get("productModel"))
        await coordinator.async_initialize()
        if entry.disabled_by is not None:
            await coordinator.async_shutdown()
            return False
        from homeassistant.helpers.entity_platform import async_get_platforms

        runtime = {"coordinator": coordinator, "connection_type": "native_tcp",
                   "loaded_platforms": set(),
                   "config_snapshot": (dict(entry.data), dict(entry.options))}
        hass.data[DOMAIN][entry.entry_id] = runtime
        existing = {id(platform) for platform in async_get_platforms(hass, DOMAIN)}
        try:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        finally:
            # HA retains old EntityPlatform objects after reset; identity matters.
            runtime["loaded_platforms"] = {
                platform.domain for platform in async_get_platforms(hass, DOMAIN)
                if platform.config_entry is entry and id(platform) not in existing
            }
        if entry.disabled_by is not None:
            await async_unload_entry(hass, entry)
            return False
    except BaseException:
        # Setup failure must close its listener and retain/recover endpoint ownership.
        try:
            await coordinator.async_shutdown()
        except BaseException:
            # Preserve the original setup error and the durable recovery journal.
            _LOGGER.exception("Endpoint restoration failed after native setup failure")
        finally:
            await coordinator.async_abort_setup()
            hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    async def shutdown(event):
        await coordinator.async_shutdown()
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    entry.async_on_unload(entry.add_update_listener(update_listener))
    await coordinator.async_refresh()
    if cloud is not None:
        _start_cloud_push(hass, entry, coordinator, cloud)
    return True


def _start_cloud_push(hass, entry, coordinator, api):
    """Reuse the entry's SEMS+ session; MQTT reconnect must not log controls out."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    from .cloud_push import CloudPush

    # The API serializes web operations, including credential renewal. A separate
    # MQTT client login raced with Stop during physical cloud return (A0201/C0602).
    # Alternate/test API implementations need not provide optional cloud events.
    if not callable(getattr(api, "fetch_mqtt_settings", None)):
        return
    push = CloudPush(coordinator, entry.data[CONF_STATION_ID], api.fetch_mqtt_settings)
    coordinator.cloud_push = push

    async def shutdown(event):
        await coordinator.cloud_push.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    push.start()


def _create_cloud_api(hass, entry, username, password):
    """Register cleanup immediately, including failed setup and HA shutdown."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    api = SemsApi(hass, username, password)

    async def shutdown(event):
        await hass.async_add_executor_job(api.close)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    # HA unload callbacks are synchronous. Offload the idempotent close; its
    # locks wait for active requests and reject later requests on this client.
    @callback
    def close():
        hass.async_add_executor_job(api.close)

    entry.async_on_unload(close)
    return api
