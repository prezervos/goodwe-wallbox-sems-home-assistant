"""Normalize only verified cloud/native observations; never infer missing values."""

from __future__ import annotations

from math import isfinite

# Suspended EV/EVSE describes interruption, not session completion.
# Modbus compatibility also uses these names for start failure / PV shortage.
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
    """Return conservative session activity for control safety checks.

    Zero load does not confirm Stop or authorize idle-only settings. Use
    energy_flow_active for the read-only actual-energy-flow entity.

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


def energy_flow_active(values: dict, *, local: bool) -> bool | None:
    """Return measured energy flow without changing session/control semantics.

    Args:
        values: Observations whose freshness the caller has checked.
        local: Whether native phase-current consistency must also be checked.

    Returns:
        True for positive measured power, False for confirmed zero load, or None
        for missing, invalid, or contradictory measurements.
    """
    def number(raw):
        if isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(value) and value >= 0 else None

    power = number(values.get("power"))
    if power is None:
        return None
    if local:
        currents = values.get("currents_a")
        if not isinstance(currents, (list, tuple)) or len(currents) != 3:
            return None
        parsed = [number(value) for value in currents]
        if any(value is None for value in parsed):
            return None
        if (power > 0) != any(value > 0 for value in parsed):
            return None
    return power > 0


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
        # Owner-confirmed target reduction: state 2 remains after load stops,
        # with connection 1 and zero phase currents. This proves connection,
        # not completion; the same tuple can also occur during Start.
        if (values.get("raw_state") == 2 and type(connection) is int
                and connection == 1 and energy_flow_active(values, local=True) is False):
            return "connected"
        if (values.get("raw_state") == 0
                and charging_active(values, local=True) is False
                and type(connection) is int):
            return {0: "not_plugged_in", 1: "connected"}.get(connection)
        return None
    # Last-session codes 6/8 do not establish the current cable/completion state.
    legacy_state = _CLOUD_VEHICLE.get(values.get("workstate"))
    if "vehConnStu" in values:
        # An owner-confirmed cable cycle on HCA FW1010 returned 1 -> 0 -> 1,
        # while SEMS+ workState incorrectly stayed available_gun_no_insered.
        # This flag proves connection only, not completion or charging activity.
        connection = values["vehConnStu"]
        if type(connection) is not int or connection not in (0, 1):
            return None
        if connection == 0:
            return "not_plugged_in"
        return "finished_charging" if legacy_state == "finished_charging" else "connected"
    return legacy_state
