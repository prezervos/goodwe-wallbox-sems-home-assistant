"""Keep independently observed cloud session data separate from configuration."""

import math
from datetime import datetime


def active_session_available(values):
    """Check for an active session with usable measured power, not completed history.

    Args:
        values: Latest mapped response from the current-session endpoint.

    Returns:
        Whether an independent active-session observation is usable.
    """
    if not isinstance(values, dict) or type(values.get("last_charge_work_status")) is not int:
        return False
    if values["last_charge_work_status"] != 6:
        return False
    power = values.get("last_charge_power")
    if power is None or isinstance(power, bool):
        return False
    try:
        value = float(power)
        return math.isfinite(value) and value >= 0
    except (TypeError, ValueError, OverflowError):
        return False


def configuration_available(coordinator, serial):
    """Check configuration availability independently of active-session telemetry.

    Args:
        coordinator: Owner of the current cloud observation.
        serial: Configured device identity.

    Returns:
        Whether the current update includes a valid configuration detail.
    """
    return coordinator.last_update_success and (
        (coordinator.data.get(serial) or {}).get("_cloud_detail_available") is not False
    )


def equipment_report(serial, rows):
    """Validate the identity, state and actual report time from SEMS+ information.

    Args:
        serial: Expected charger identity.
        rows: Equipment information rows returned by SEMS+.

    Returns:
        Validated equipment fields indexed by code.

    Raises:
        ConnectionError: Identity, report timestamp or state cannot be validated.
    """
    if not isinstance(rows, list):
        raise ConnectionError("Missing SEMS+ equipment report")
    values = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("code"), str):
            raise ConnectionError("Malformed SEMS+ equipment information")
        code = row["code"]
        if code in values:
            raise ConnectionError("Duplicate SEMS+ equipment information field")
        values[code] = row.get("data")
    if values.get("sn") != serial or str(values.get("deviceType", "")).lower() != "ev_charger":
        raise ConnectionError("Mismatched SEMS+ equipment identity")
    stamp = values.get("updateTime")
    try:
        if not isinstance(stamp, str) or " " not in stamp and "T" not in stamp:
            raise ValueError("Missing report timestamp")
        datetime.fromisoformat(stamp)
    except ValueError as error:
        raise ConnectionError("Invalid SEMS+ device report timestamp") from error
    status = values.get("workStu")
    if type(status) not in (int, str) or status not in ("0", "6", "8", 0, 6, 8):
        raise ConnectionError("Unverified SEMS+ station status")
    return values


def modern_status_observation(serial, equipment, session, settings):
    """Combine independent modern sources without inventing measured idle power.

    Args:
        serial: Expected charger identity.
        equipment: Validated output of equipment_report.
        session: Mapped current-session data.
        settings: Current matching configuration detail, or None.

    Returns:
        A source-labelled observation carrying the device information timestamp.

    Raises:
        ConnectionError: Configuration identity or session/station state disagrees.
    """
    state = int(equipment["workStu"])
    if settings is not None and settings.get("sn") != serial:
        raise ConnectionError("Mismatched modern cloud configuration")
    result = dict(settings or {})
    for key in ("power", "current", "status", "workstate", "startStatus", "lastUpdate"):
        result.pop(key, None)
    result.update(sn=serial, lastUpdate=equipment["updateTime"],
                  observation_source="semsplus_equipment", power=None, current=None,
                  _cloud_detail_available=settings is not None)
    if state == 0:
        result["status"] = "offline"
        return result
    work_status = session.get("last_charge_work_status") if isinstance(session, dict) else None
    if type(work_status) is not int or work_status != state:
        raise ConnectionError("Modern cloud station and session reports disagree")
    if state == 6 and not active_session_available(session):
        raise ConnectionError("Modern cloud session lacks valid measured power")
    result.update(session)
    result["status"] = "charging" if state == 6 else "standby"
    result["_cloud_session_idle"] = state == 8
    if state == 6:
        result["power"] = float(session["last_charge_power"])
    return result
