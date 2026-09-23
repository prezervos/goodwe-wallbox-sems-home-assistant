"""DataUpdateCoordinator for the GoodWe SEMS Wallbox integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_STATION_ID, DEFAULT_SCAN_INTERVAL_IDLE, DEFAULT_SCAN_INTERVAL_CHARGING, CONF_SCAN_INTERVAL_CHARGING
from .sems_api import SemsApi, OutOfRetries
from .cloud_observation import CloudAuthenticationError

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
            entry.data.get(CONF_SCAN_INTERVAL_CHARGING, DEFAULT_SCAN_INTERVAL_CHARGING),
        ))

        _LOGGER.debug(
            "SEMS coordinator init for station %s with idle_interval=%ss, charging_interval=%ss",
            self._station_id,
            self._interval_idle,
            self._interval_charging,
        )

        self._pending_refresh_cancel = None
        self._closed = False
        entry.async_on_unload(self._cancel_delayed_refresh)

        super().__init__(
            hass,
            _LOGGER,
            name="SEMS API wallbox",
            config_entry=entry,
            update_interval=timedelta(seconds=self._interval_idle),
        )

    @property
    def resolved_device_identity(self):
        """Expose cached detection results for editable configuration defaults."""
        return self._api.cached_device_identity if self._api is not None else {}

    @callback
    def _cancel_delayed_refresh(self) -> None:
        """Cancel integration-owned timers when the config entry unloads."""
        self._closed = True
        if self._pending_refresh_cancel is not None:
            self._pending_refresh_cancel()
            self._pending_refresh_cancel = None

    def schedule_delayed_refresh(self, delay: float = 5.0) -> None:
        """Schedule a one-shot refresh after `delay` seconds.

        Cancels any previously pending delayed refresh so rapid actions
        (e.g. slider dragging) don't pile up.
        """
        if self._closed:
            return
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
            # SEMS+ supplies configuration here; failure remains an unavailable
            # update. The native cloud/TCP path uses separate timestamped v3 reads.
            result = await self._hass.async_add_executor_job(
                self._api.get_data_gen2,
                self._station_id,
            )
        except CloudAuthenticationError as err:
            raise ConfigEntryAuthFailed("SEMS credentials were rejected") from err
        except OutOfRetries as err:
            if (push := getattr(self, "cloud_push", None)) is not None:
                push.polling.failed()
            raise UpdateFailed(
                f"Too many retries talking to SEMS API: {err}"
            ) from err
        except Exception as err:  # noqa: BLE001
            if (push := getattr(self, "cloud_push", None)) is not None:
                push.polling.failed()
            raise UpdateFailed(
                f"Error communicating with SEMS API: {err}"
            ) from err

        if result is None:
            if (push := getattr(self, "cloud_push", None)) is not None:
                push.polling.failed()
            raise UpdateFailed(
                "No data received from SEMS API, token might be invalid. See debug logs."
            )

        sn = result.get("sn")
        if sn != self._station_id:
            if (push := getattr(self, "cloud_push", None)) is not None:
                push.polling.failed()
            raise UpdateFailed("Missing or mismatched wallbox identity in SEMS API data")

        # Also poll getLastCharge to determine real-time charging state.
        # workStu=6 means actively charging (startStatus in /detail is unreliable).
        try:
            last_charge = await self._hass.async_add_executor_job(
                self._api.fetch_last_charge,
                self._station_id,
            )
        except Exception:  # noqa: BLE001
            # Optional history must not discard an otherwise valid device read.
            _LOGGER.debug("Optional charging history read failed", exc_info=True)
            last_charge = None
        if last_charge:
            result.update(last_charge)

        data: dict[str, Any] = {sn: result}
        _LOGGER.debug(
            "Coordinator fetched data for wallbox %s: %s",
            sn,
            result,
        )

        # Dynamic polling: faster while actively charging.
        # Use workStu=6 from getLastCharge (startStatus in /detail is always False in PV mode).
        is_charging = result.get("last_charge_work_status") == 6
        seconds = self._interval_charging if is_charging else self._interval_idle
        push = getattr(self, "cloud_push", None)
        new_interval = (
            push.polling.observed(result, seconds) if push is not None
            else timedelta(seconds=seconds)
        )
        if new_interval != self.update_interval:
            self.update_interval = new_interval
            _LOGGER.debug(
                "Coordinator polling interval -> %ss (charging=%s)",
                int(new_interval.total_seconds()),
                is_charging,
            )

        return data
