"""
Support for switch controlling an output of a GoodWe SEMS inverter.

For more details about this platform, please refer to the documentation at
https://github.com/TimSoethout/goodwe-sems-home-assistant
"""

from datetime import timedelta
import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_STATION_ID, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add switches for passed config_entry in HA."""
    semsApi = hass.data[DOMAIN][config_entry.entry_id]
    stationId = config_entry.data[CONF_STATION_ID]

    update_interval = timedelta(
        seconds=config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    set_charge_power = 0

    async def async_update_data():
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            # async with async_timeout.timeout(10):
            result = await hass.async_add_executor_job(semsApi.getData, stationId)
            _LOGGER.debug("Resulting result: %s", result)

            inverter = result

            data = {}
            if inverter is None:
                # something went wrong, probably token could not be fetched
                raise UpdateFailed(
                    "Error communicating with API, probably token could not be fetched, see debug logs"
                )

            name = inverter["name"]
            sn = inverter["sn"]
            nonlocal set_charge_power
            set_charge_power = inverter["set_charge_power"]
            _LOGGER.debug("Found wallbox attribute %s %s set_charge_power", name, sn)
            data[sn] = inverter

            # _LOGGER.debug("Resulting data: %s", data)
            return data
        # except ApiError as err:
        except Exception as err:
            # logging.exception("Something awful happened!")
            raise UpdateFailed(f"Error communicating with API: {err}")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name="SEMS API number",
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=update_interval,
    )

    #
    # Fetch initial data so we have data when entities subscribe
    #
    # If the refresh fails, async_config_entry_first_refresh will
    # raise ConfigEntryNotReady and setup will try again later
    #
    # If you do not want to retry setup on failure, use
    # coordinator.async_refresh() instead
    #
    await coordinator.async_config_entry_first_refresh()

    # _LOGGER.debug("Initial coordinator data: %s", coordinator.data)
    async_add_entities(
        SemsNumber(coordinator, ent, semsApi, set_charge_power)
        for idx, ent in enumerate(coordinator.data)
    )


class SemsNumber(CoordinatorEntity, NumberEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, sn, api, value: float):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.api = api
        self.sn = sn
        self._attr_native_value = float(value) if value is not None else None
        _LOGGER.debug(f"Creating SemsNumber for Wallbox {self.sn}")

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return f"Wallbox set charge power"

    @property
    def device_class(self):
        return NumberDeviceClass.POWER

    @property
    def native_step(self):
        return 0.1

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.data[self.sn]["sn"]}_number_set_charge_power"

    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.sn)
            },
            "name": self.name,
            "manufacturer": "GoodWe",
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        set_charge_power = self.coordinator.data[self.sn]["set_charge_power"]
        self._attr_native_value = float(set_charge_power)
        _LOGGER.debug("Handling coordinator update set_charge_power")
        self.async_write_ha_state()

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()
        set_charge_power = self.coordinator.data[self.sn]["set_charge_power"]
        self._attr_native_value = float(set_charge_power)
        _LOGGER.debug(f"Updating SemsNumber for Wallbox state to {set_charge_power}")
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        active_mode = self.coordinator.data[self.sn]["chargeMode"]
        _LOGGER.debug(f"Setting set_charge_power to {float(set_charge_power)}")
        await self.hass.async_add_executor_job(
            self.api.set_charge_mode, self.sn, active_mode, value
        )
        await self.coordinator.async_request_refresh()
        set_charge_power = self.coordinator.data[self.sn]["set_charge_power"]
        self._attr_native_value = float(set_charge_power)
        self.async_write_ha_state()

    async def async_set_value(self, value: float) -> None:
        active_mode = self.coordinator.data[self.sn]["chargeMode"]
        _LOGGER.debug(f"Setting set_charge_power to {float(set_charge_power)}")
        await self.hass.async_add_executor_job(
            self.api.set_charge_mode, self.sn, active_mode, value
        )
        await self.coordinator.async_request_refresh()
        set_charge_power = self.coordinator.data[self.sn]["set_charge_power"]
        self._attr_native_value = float(set_charge_power)
        self.async_write_ha_state()
