"""DataUpdateCoordinator for the GoodWe SEMS Wallbox integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_STATION_ID, DEFAULT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_IDLE, DEFAULT_SCAN_INTERVAL_CHARGING, CONF_SCAN_INTERVAL_CHARGING
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
        self._interval_idle = int(entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_IDLE),
        ))
        self._interval_charging = int(entry.options.get(
            CONF_SCAN_INTERVAL_CHARGING,
            DEFAULT_SCAN_INTERVAL_CHARGING,
        ))

        _LOGGER.debug(
            "SEMS coordinator init for station %s with idle_interval=%ss, charging_interval=%ss",
            self._station_id,
            self._interval_idle,
            self._interval_charging,
        )

        self._pending_refresh_cancel = None

        super().__init__(
            hass,
            _LOGGER,
            name="SEMS API wallbox",
            update_interval=timedelta(seconds=self._interval_idle),
        )

    def schedule_delayed_refresh(self, delay: float = 5.0) -> None:
        """Schedule a one-shot refresh after `delay` seconds.

        Cancels any previously pending delayed refresh so rapid actions
        (e.g. slider dragging) don't pile up.
        """
        if self._pending_refresh_cancel is not None:
            self._pending_refresh_cancel()
            self._pending_refresh_cancel = None

        @callback
        def _do_refresh(_now):
            self._pending_refresh_cancel = None
            self.hass.async_create_task(self.async_request_refresh())

        self._pending_refresh_cancel = async_call_later(self.hass, delay, _do_refresh)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the SEMS API."""
        try:
            # Use EU gateway for Gen2 (plant_id configured), legacy API otherwise.
            if self._api._plant_id:
                result = await self._hass.async_add_executor_job(
                    self._api.get_data_gen2,
                    self._station_id,
                )
            else:
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

        # Dynamic polling: faster while actively charging (power > 0)
        is_charging = float(result.get("power", 0) or 0) > 0
        new_interval = timedelta(
            seconds=self._interval_charging if is_charging else self._interval_idle
        )
        if new_interval != self.update_interval:
            self.update_interval = new_interval
            _LOGGER.debug(
                "Coordinator polling interval -> %ss (charging=%s)",
                int(new_interval.total_seconds()),
                is_charging,
            )

        return data
