"""DataUpdateCoordinator for the GoodWe SEMS Wallbox integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_STATION_ID, DEFAULT_SCAN_INTERVAL
from .sems_api import SemsApi, OutOfRetries

_LOGGER = logging.getLogger(__name__)


class SemsUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate fetching data from the SEMS Wallbox API."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: SemsApi,
    ) -> None:
        """Initialize the coordinator."""
        self._hass = hass
        self._api = api
        self._station_id: str = entry.data[CONF_STATION_ID]

        # Options take precedence over data, then fall back to default
        interval_seconds = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        _LOGGER.debug(
            "SEMS coordinator init for station %s with scan_interval=%s s",
            self._station_id,
            interval_seconds,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="SEMS API wallbox",
            update_interval=timedelta(seconds=interval_seconds),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the SEMS API."""
        try:
            result = await self._hass.async_add_executor_job(
                self._api.getData,
                self._station_id,
            )
        except OutOfRetries as err:
            raise UpdateFailed(
                f"Too many retries talking to SEMS API: {err}"
            ) from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(
                f"Error communicating with SEMS API: {err}"
            ) from err

        if result is None:
            raise UpdateFailed(
                "No data received from SEMS API, token might be invalid. See debug logs."
            )

        sn = result.get("sn")
        if not sn:
            raise UpdateFailed("Missing 'sn' in SEMS API data")

        data: dict[str, Any] = {sn: result}
        _LOGGER.debug(
            "Coordinator fetched data for wallbox %s: %s",
            sn,
            result,
        )
        return data
