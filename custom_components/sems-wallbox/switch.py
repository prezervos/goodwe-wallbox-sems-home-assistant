"""
Support for switch controlling an output of a GoodWe SEMS inverter.

For more details about this platform, please refer to the documentation at
https://github.com/TimSoethout/goodwe-sems-home-assistant
"""

import logging

from homeassistant.const import (
    CONF_SCAN_INTERVAL,
)

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from .const import DOMAIN, CONF_STATION_ID, DEFAULT_SCAN_INTERVAL
from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
)
from homeassistant.config_entries import ConfigEntry

from datetime import timedelta


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add switches for passed config_entry in HA."""
    semsApi = hass.data[DOMAIN][config_entry.entry_id]
    stationId = config_entry.data[CONF_STATION_ID]

    update_interval = timedelta(
        seconds=config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    current_state = 0
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
            nonlocal current_state
            current_state = inverter["startStatus"]
            _LOGGER.debug("Found wallbox attribute %s %s", name, sn)
            data[sn] = inverter

            #_LOGGER.debug("Resulting data: %s", data)
            return data
        # except ApiError as err:
        except Exception as err:
            # logging.exception("Something awful happened!")
            raise UpdateFailed(f"Error communicating with API: {err}")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name="SEMS API switch",
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
        SemsSwitch(coordinator, ent, semsApi, current_state == 0) for idx, ent in enumerate(coordinator.data)
    )


class SemsSwitch(CoordinatorEntity, SwitchEntity):

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, sn, api, current_is_on: bool):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.api = api
        self.sn = sn
        self._attr_is_on = current_is_on
        _LOGGER.debug(f"Creating SemsSwitch for Wallbox {self.sn}")

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return f"Start charging"

    @property
    def device_class(self):
        return SwitchDeviceClass.SWITCH

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.data[self.sn]["sn"]}-switch-start-charging"

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

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        """Return entity status."""
        return self.coordinator.data[self.sn]["startStatus"] == 0

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug(f"Wallbox {self.sn} set to Off")
        await self.hass.async_add_executor_job(self.api.change_status, self.sn, 2)
        await self.coordinator.async_request_refresh()
        startStatus = self.coordinator.data[self.sn]["startStatus"]
        self._attr_is_on = startStatus == 0
        _LOGGER.debug(f"Setting switch is_on to {startStatus == 0}")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug(f"Wallbox {self.sn} set to On")
        await self.hass.async_add_executor_job(self.api.change_status, self.sn, 1)
        await self.coordinator.async_request_refresh()
        startStatus = self.coordinator.data[self.sn]["startStatus"]
        self._attr_is_on = startStatus == 0
        _LOGGER.debug(f"Setting switch is_on to {startStatus == 0}")
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        startStatus = self.coordinator.data[self.sn]["startStatus"]
        _LOGGER.debug(f"Handling coordinator update {startStatus == 0}")
        self._attr_is_on = startStatus == 0
        self.async_write_ha_state()

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()
        startStatus = self.coordinator.data[self.sn]["startStatus"]
        self._attr_is_on = startStatus == 0
        _LOGGER.debug(f"Updating SemsSwitch for Wallbox state to {startStatus == 0}")
        self.async_write_ha_state()

