"""Normalize only verified cloud/native observations; never infer missing values."""

from __future__ import annotations

_CLOUD_VEHICLE = {
    "EVDetail_Status_Waiting_Stat00": "not_plugged_in",
    "EVDetail_Status_Waiting_Stat01": "connected",
    "EVDetail_Status_Waiting_Stat02": "finished_charging",
    "available_gun_no_insered": "not_plugged_in",
    "available_gun_no_inserted": "not_plugged_in",
    "available_gun_insered": "connected",
    "available_gun_inserted": "connected",
    "prepare": "connected",
    "finishing": "finished_charging",
    "finish": "finished_charging",
}


def charging_active(values: dict, *, local: bool) -> bool | None:
    """Return observed activity or None when the report is inconclusive.

    Args:
        values: Current transport observations.
        local: Whether the report came from native TCP.

    Returns:
        Confirmed charging, confirmed idle, or an unknown observation.
    """
    if local:
        state = values.get("raw_state")
        power = values.get("power")
        currents = values.get("currents_a")
        if state == 2 and power is not None and currents is not None:
            return True if power > 0 and any(currents) else None
        if state in (0, 3) and power == 0 and currents is not None:
            return False if not any(currents) else None
        return None
    status = str(values.get("status", "")).lower()
    if status in ("charging", "evdetail_status_title_charging"):
        return True
    if status in ("waiting", "available", "standby", "evdetail_status_title_waiting"):
        return False
    return None


def vehicle_state(values: dict, *, local: bool) -> str | None:
    """Return a vehicle state from verified cloud or native observations.

    Args:
        values: Current transport observations.
        local: Whether the report came from native TCP.

    Returns:
        Known vehicle state, or None if its meaning is not established.
    """
    if charging_active(values, local=local) is True:
        return "connected"
    if local:
        # Two owner-confirmed idle cable cycles on original HCA FW1010 showed
        # status body byte 45 switching 1 -> 0 -> 1 in both 104 and 2104 frames.
        # Other codes/states remain unverified; zero power alone proves nothing.
        connection = values.get("connection")
        if (values.get("raw_state") == 0
                and charging_active(values, local=True) is False
                and type(connection) is int):
            return {0: "not_plugged_in", 1: "connected"}.get(connection)
        return None
    # Last-session codes 6/8 do not establish the current cable/completion state.
    return _CLOUD_VEHICLE.get(values.get("workstate"))
