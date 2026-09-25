"""DataUpdateCoordinator for the GoodWe Wallbox Gen2 via local Modbus TCP."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .write_confirmation import WriteConfirmation, confirmation_read, readback_debouncer
from .const import (
    CONF_STATION_ID,
    DEFAULT_SCAN_INTERVAL_IDLE,
    DEFAULT_SCAN_INTERVAL_CHARGING,
    CONF_SCAN_INTERVAL_CHARGING,
)
from .wallbox_modbus import WallboxModbusClient

_LOGGER = logging.getLogger(__name__)

# How quickly to retry after a Modbus communication failure (seconds).
_RETRY_AFTER_ERROR_SECONDS = 30

class ModbusUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate fetching data from the wallbox directly via Modbus TCP."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WallboxModbusClient,
    ) -> None:
        self._client = client
        self._station_id: str = entry.data[CONF_STATION_ID]

        self._interval_idle = int(entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_IDLE),
        ))
        self._interval_charging = int(entry.options.get(
            CONF_SCAN_INTERVAL_CHARGING,
            entry.data.get(CONF_SCAN_INTERVAL_CHARGING, DEFAULT_SCAN_INTERVAL_CHARGING),
        ))

        self._pending_refresh_cancel = None
        self.write_confirmation = WriteConfirmation(self, modbus=True)
        self._closed = False
        entry.async_on_unload(self._cancel_delayed_refresh)
        super().__init__(
            hass,
            _LOGGER,
            name="Modbus wallbox",
            config_entry=entry,
            request_refresh_debouncer=readback_debouncer(hass, _LOGGER),
            update_interval=timedelta(seconds=self._interval_idle),
        )

    @callback
    def _cancel_delayed_refresh(self) -> None:
        """Cancel the custom timer as well as HA's coordinator-owned timers."""
        self.write_confirmation.close()
        self._closed = True
        if self._pending_refresh_cancel is not None:
            self._pending_refresh_cancel()
            self._pending_refresh_cancel = None

    def schedule_delayed_refresh(self, delay: float = 3.0) -> None:
        """Schedule a one-shot coordinator refresh after `delay` seconds."""
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

    @confirmation_read
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the wallbox via Modbus TCP."""
        try:
            result = await self.hass.async_add_executor_job(self._client.read_all)
        except Exception as err:  # noqa: BLE001
            self.schedule_delayed_refresh(_RETRY_AFTER_ERROR_SECONDS)
            raise UpdateFailed(f"Modbus read error: {err}") from err

        if result is None:
            self.schedule_delayed_refresh(_RETRY_AFTER_ERROR_SECONDS)
            raise UpdateFailed("No data received from Modbus -- check wallbox connectivity")

        if self._closed:
            raise UpdateFailed("Modbus coordinator is closed")
        sn = result.get("sn")
        if sn != self._station_id:
            raise UpdateFailed("Missing or mismatched Modbus device identity")

        data: dict[str, Any] = {sn: result}
        _LOGGER.debug(
            "Modbus %s: status=%s power=%.1f on_off=%s car=%s cp=%s start=%s",
            sn,
            result.get("modbus_status_name"),
            result.get("modbus_power") or 0.0,
            result.get("modbus_charging_on_off"),
            result.get("modbus_car_connected"),
            result.get("modbus_cp_state_name"),
            result.get("modbus_start_mode"),
        )

        # Dynamic polling: faster while actively charging
        is_charging = result.get("modbus_status_raw") == 3
        new_interval = timedelta(
            seconds=self._interval_charging if is_charging else self._interval_idle
        )
        if new_interval != self.update_interval:
            self.update_interval = new_interval
            _LOGGER.debug("Modbus coordinator polling interval -> %ss (charging=%s)",
                          int(new_interval.total_seconds()), is_charging)

        # Polling must not infer a Stop from contradictory status/CP registers.
        # They can describe a paused or non-atomically sampled charging session;
        # preserve the report and leave controls to explicit user/automation intent.
        return data
