"""Transport-specific, independent mode reads for the optional Start guard."""

from __future__ import annotations

from .operation_budget import async_execute

import asyncio
import math
from datetime import datetime, timezone

from .charge_mode_policy import ModeObservation, ModeVerificationError
from .observed_state import charging_active


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _marker(value):
    """Normalize timestamps; keep naive local time distinct from UTC time."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value <= 0:
                return None
            # Both Unix seconds and milliseconds occur in gateway responses.
            seconds = value / 1000 if value >= 1e12 else value
            stamp = datetime.fromtimestamp(seconds, timezone.utc)
        elif isinstance(value, str) and value.strip():
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if stamp.tzinfo is None:
            return "local:" + stamp.isoformat(timespec="microseconds")
        return "utc:" + stamp.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        )
    except (ValueError, OverflowError, OSError):
        return None


class ModeTransportAdapter:
    """Read transport data directly, bypassing optimistic HA entity state."""

    def __init__(self, hass, serial, client, *, modbus=False):
        self.hass = hass
        self.serial = serial
        self.client = client
        self.modbus = modbus

    async def read(self):
        """Read an explicit reported mode and reject absent/offline data."""
        if self.modbus:
            data = await async_execute(self.hass, self.client.read_all)
        else:
            reader = (
                self.client.fetch_status_observation
                if getattr(self.client, "supports_timestamped_observation", False)
                is True
                else self.client.get_data_gen2
            )
            data = await async_execute(self.hass, reader, self.serial)
        if not isinstance(data, dict) or data.get("sn") != self.serial:
            raise ModeVerificationError("Missing or mismatched device identity")
        mode = (
            data.get("chargeMode")
            if self.modbus
            else data.get("_reported_charge_mode", data.get("chargeMode"))
        )
        if type(mode) is not int or mode not in (0, 1, 2):
            raise ModeVerificationError("Device did not report a valid charging mode")
        status = str(data.get("status", "")).lower()
        if "offline" in status:
            raise ModeVerificationError("Wallbox is offline")
        marker = None if self.modbus else _marker(data.get("lastUpdate"))
        if not self.modbus and marker is None:
            raise ModeVerificationError(
                "Cloud report has no usable freshness timestamp"
            )
        active = (
            data.get("modbus_status_raw") in (2, 3)
            if self.modbus
            else status in ("charging", "preparing", "evdetail_status_title_charging")
            or data.get("startStatus") is True
        )
        if (
            not self.modbus
            and getattr(self.client, "supports_timestamped_observation", False) is True
        ):
            # SEMS v3 returns startStatus=True even for verified Waiting/0 kW.
            # It is not a charging observation on this endpoint.
            measured_power = _number(data.get("power"))
            if measured_power is None or measured_power < 0:
                raise ModeVerificationError("Cloud report has no valid measured power")
            active = measured_power > 0 or status not in (
                "waiting",
                "standby",
                "available",
                "evdetail_status_title_waiting",
            )
        if (
            not self.modbus
            and getattr(self.client, "supports_timestamped_observation", False)
            is not True
        ):
            last_charge = await async_execute(self.hass,
                self.client.fetch_last_charge, self.serial
            )
            if isinstance(last_charge, dict):
                active = active or last_charge.get("last_charge_work_status") == 6
        configured_power = _number(data.get("set_charge_power"))
        if (
            not self.modbus
            and getattr(self.client, "supports_timestamped_observation", False) is True
        ):
            # V3's reported allocation can remain 4.2 kW while the configured
            # ceiling changes. Read the ceiling from the same SEMS+ API that
            # owns writes; retain V3 for actual activity and device freshness.
            settings = await async_execute(self.hass,
                self.client.get_data_gen2, self.serial
            )
            if not isinstance(settings, dict) or settings.get("sn") != self.serial:
                raise ModeVerificationError("Missing or mismatched cloud configuration")
            settings_mode = settings.get("_reported_charge_mode")
            limit = settings.get("set_charge_power")
            configured_power = (
                _number(limit)
                if type(settings_mode) is int and settings_mode == mode
                and not isinstance(limit, bool)
                else None
            )
        return ModeObservation(
            mode,
            configured_power,
            _number(data.get("min_charge_power")),
            _number(data.get("max_charge_power")),
            marker,
            not self.modbus,
            active,
        )

    async def write_mode(self, mode, before):
        """Use the selected transport's encoder without increasing the ceiling."""
        if self.modbus:
            if mode == 0 and (
                before.power is None
                or not math.isfinite(before.power)
                or before.power <= 0
            ):
                raise ModeVerificationError(
                    "Set a valid power limit before selecting Fast mode"
                )
            if (
                await async_execute(self.hass,
                    self.client.write_charge_mode, mode
                )
                is not True
            ):
                return False
            if mode == 0:
                return await async_execute(self.hass,
                    self.client.write_max_charge_power, before.power
                )
            return True
        power = None
        if mode == 0:
            power = before.power
            if (
                power is None
                or not math.isfinite(power)
                or power <= 0
                or (before.minimum_power is not None and power < before.minimum_power)
                or (before.maximum_power is not None and power > before.maximum_power)
            ):
                raise ModeVerificationError(
                    "Set a valid power limit before selecting Fast mode"
                )
        return await async_execute(self.hass,
            self.client.set_charge_mode_gen2, self.serial, mode, power
        )

    async def start(self):
        """Send one transport-specific Start; never replay uncertain writes."""
        if self.modbus:
            return await async_execute(self.hass,
                self.client.write_start_stop, True
            )
        return await async_execute(self.hass,
            self.client.change_status_gen2, self.serial, "start"
        )

    async def stop(self):
        """Send Stop through the selected transport."""
        if self.modbus:
            return await async_execute(self.hass,
                self.client.write_start_stop, False
            )
        acknowledged = await async_execute(self.hass,
            self.client.change_status_gen2, self.serial, "stop"
        )
        if acknowledged is True:
            return True
        if getattr(self.client, "supports_timestamped_observation", False) is not True:
            return False
        return await self._confirm_stopped()

    async def _confirm_stopped(self, *, timeout=60.0, interval=5.0):
        """Confirm a rejected Stop from advancing telemetry, never session history.

        Args:
            timeout: Maximum seconds to wait for independent confirmation.
            interval: Seconds between read-only observations.

        Returns:
            True only for a newer idle report with zero measured power.

        Raises:
            ConnectionError: The independent observation could not be read.
        """
        baseline = None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    data = await async_execute(self.hass,
                        self.client.fetch_status_observation, self.serial
                    )
                    if not isinstance(data, dict) or data.get("sn") != self.serial:
                        return False
                    marker = _marker(data.get("lastUpdate"))
                    if marker is None:
                        return False
                    if baseline is None:
                        # Baseline is read AFTER Stop: a cached idle response alone
                        # cannot confirm success while cloud telemetry is lagging.
                        baseline = marker
                    elif (
                        marker.split(":", 1)[0] == baseline.split(":", 1)[0]
                        and marker > baseline
                        and charging_active(data, local=False) is False
                        and not isinstance(data.get("power"), bool)
                        and _number(data.get("power")) == 0
                    ):
                        return True
                    await asyncio.sleep(interval)
        except TimeoutError:
            return False
