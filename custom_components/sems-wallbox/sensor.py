"""
Support for power production statistics from GoodWe SEMS API.

For more details about this platform, please refer to the documentation at
https://github.com/TimSoethout/goodwe-sems-home-assistant
"""
from decimal import Decimal
from typing import Coroutine
from homeassistant.core import HomeAssistant
import homeassistant
import logging

from datetime import timedelta

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    UnitOfPower,
    UnitOfEnergy,
)
from .const import DOMAIN, CONF_STATION_ID, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    # _LOGGER.debug("hass.data[DOMAIN] %s", hass.data[DOMAIN])
    semsApi = hass.data[DOMAIN][config_entry.entry_id]
    stationId = config_entry.data[CONF_STATION_ID]

    # _LOGGER.debug("config_entry %s", config_entry.data)
    update_interval = timedelta(
        seconds=config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

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
        name="SEMS API",
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
        SemsSensor(coordinator, ent) for idx, ent in enumerate(coordinator.data)
    )
    async_add_entities(
        SemsStatisticsSensor(coordinator, ent)
        for idx, ent in enumerate(coordinator.data)
    )


class SemsSensor(CoordinatorEntity, SensorEntity):
    """SemsSensor using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available
    """

    def __init__(self, coordinator, sn):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        _LOGGER.debug("Creating SemsSensor with id %s", self.sn)

    @property
    def device_class(self):
        return SensorDeviceClass.ENUM

    @property
    def options(self):
        return ["Charging", "Standby", "Offline", "Unknown"]

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"Wallbox {self.coordinator.data[self.sn]['model']}"

    @property
    def unique_id(self) -> str:
        return self.coordinator.data[self.sn]["sn"]

    @property
    def state(self):
        """Return the state of the device."""
        # _LOGGER.debug("state, coordinator data: %s", self.coordinator.data)
        # _LOGGER.debug("self.sn: %s", self.sn)
        # _LOGGER.debug(
        #     "state, self data: %s", self.coordinator.data[self.sn]
        # )
        data = self.coordinator.data[self.sn]
        if data["status"] == 'EVDetail_Status_Title_Charging':
            return "Charging"
        elif data["status"] == 'EVDetail_Status_Title_Waiting':
            return "Standby"
        elif data["status"] == 'EVDetail_Status_Title_Offline':
            return "Offline"
        else:
            return "Unknown"

    # For backwards compatibility
    @property
    def extra_state_attributes(self):
        """Return the state attributes of the monitored installation."""
        data = self.coordinator.data[self.sn]
        # _LOGGER.debug("state, self data: %s", data.items())
        attributes = {k: v for k, v in data.items() if k is not None and v is not None}
        attributes["statusText"] = data["status"]
        return attributes

    @property
    def is_on(self) -> bool:
        """Return entity status."""
        return self.coordinator.data[self.sn]["status"] is not None

    @property
    def should_poll(self) -> bool:
        """No need to poll. Coordinator notifies entity of updates."""
        return False

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def device_info(self):
        # _LOGGER.debug("self.device_state_attributes: %s", self.device_state_attributes)
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.sn)
            },
            "name": self.name,
            "manufacturer": "GoodWe",
            "model": self.extra_state_attributes.get("model", "unknown"),
            "sw_version": self.extra_state_attributes.get("fireware", "unknown"),
            # "via_device": (DOMAIN, self.api.bridgeid),
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self):
        """Update the entity.

        Only used by the generic entity update service.
        """
        await self.coordinator.async_request_refresh()


class SemsStatisticsSensor(CoordinatorEntity, SensorEntity):
    """Sensor in kWh to enable HA statistics, in the end usable in the power component."""

    def __init__(self, coordinator, sn):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.sn = sn
        _LOGGER.debug("Creating SemsStatisticsSensor with id %s", self.sn)

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def native_unit_of_measurement(self):
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def state_class(self):
        return SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> Decimal:
        """Return the value reported by the sensor."""
        return self.coordinator.data[self.sn]["chargeEnergy"]

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return f"Wallbox Energy"

    @property
    def unique_id(self) -> str:
        return f"{self.coordinator.data[self.sn]['sn']}-energy"

    @property
    def should_poll(self) -> bool:
        """No need to poll. Coordinator notifies entity of updates."""
        return False

    @property
    def device_info(self):
        # _LOGGER.debug("self.device_state_attributes: %s", self.device_state_attributes)
        data = self.coordinator.data[self.sn]
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.sn)
            },
            # "name": self.name,
            "manufacturer": "GoodWe",
            "model": data.get("model", "unknown"),
            "sw_version": data.get("fireware", "unknown"),
            # "via_device": (DOMAIN, self.api.bridgeid),
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self):
        """Update the entity.

        Only used by the generic entity update service.
        """
        await self.coordinator.async_request_refresh()


