"""Export allowlisted runtime facts without credentials or device identifiers."""

from __future__ import annotations

import math
import time

_NUMERIC_FIELDS = (
    "power",
    "set_charge_power",
    "chargeMode",
    "fault_code",
    "connection",
    "raw_state",
    "session_seconds",
)


def _number(value):
    """Keep finite numbers only; do not copy arbitrary strings into diagnostics."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


async def async_get_config_entry_diagnostics(hass, entry):
    """Build a read-only snapshot with no network requests or sensitive free text.

    Args:
        hass: Home Assistant instance.
        entry: Existing integration entry.

    Returns:
        Allowlisted measurements and connection/protection metadata.
    """
    runtime = hass.data.get("sems_wallbox", {}).get(entry.entry_id, {})
    coordinator = runtime.get("coordinator")
    if coordinator is None:
        return {"loaded": False}
    serial = entry.data.get("wallbox_serial_No")
    values = (coordinator.data or {}).get(serial, {})
    result = {
        "loaded": True,
        "update_success": bool(coordinator.last_update_success),
        "transport": "tcp"
        if getattr(coordinator, "local", False)
        else ("modbus" if runtime.get("connection_type") == "modbus" else "cloud"),
        "measurements": {key: _number(values.get(key)) for key in _NUMERIC_FIELDS},
    }
    policy = getattr(coordinator, "charge_mode_policy", None)
    if policy is not None:
        result["requested"] = {
            "mode": _number(policy.desired_mode),
            "power_kw": _number(policy.desired_power),
        }
    transport = getattr(coordinator, "transport", None)
    if transport is not None:
        guard = transport.session_guard
        result["native"] = {
            "connected": bool(transport.available),
            "transitioning": bool(coordinator.transitioning),
            "recovery_pending": coordinator.endpoint.journal is not None,
            "waiting_for_cloud": coordinator.cloud_restored_at is not None,
            "telemetry_age_seconds": max(0, time.monotonic() - transport.observed_at)
            if transport.observed_at
            else None,
            "supervision_active": guard.limit is not None,
            "supervised_limit_kw": _number(guard.limit),
            "protection_issue": bool(guard.error),
        }
    fallback = getattr(coordinator, "automatic_fallback", None)
    if fallback is not None:
        result["automatic_fallback"] = fallback.diagnostics()
    control = getattr(coordinator, "control_fallback", None)
    if control is not None:
        result["control_fallback"] = {
            "preparing": control.preparing,
            "error": control.error,
        }
    cloud = getattr(coordinator, "cloud", None)
    if cloud is not None and hasattr(cloud, "_web_login_retry_at"):
        result["cloud_login"] = {
            "session_cached": cloud._web_token is not None,
            "login_attempts": _number(getattr(cloud, "login_attempts", None)),
            "successful_logins": _number(getattr(cloud, "successful_logins", None)),
            "session_recovery_attempts": _number(getattr(cloud, "session_recovery_attempts", None)),
            "last_login_age_seconds": max(0, time.monotonic() - cloud.last_login_at)
            if getattr(cloud, "last_login_at", None) is not None else None,
            "retry_in_seconds": max(0, round(cloud._web_login_retry_at - time.monotonic())),
            "authentication_rejected": cloud._web_login_auth_error,
        }
    pending = getattr(coordinator, "pending_intent", None)
    if pending is not None:
        result["pending_intent"] = pending.diagnostics()
    energy = getattr(coordinator, "energy_polling", None)
    if energy is not None:
        result["cumulative_energy"] = {
            "enabled": energy.enabled,
            "available": energy.available,
            "read_failed": energy.error is not None,
            "energy_kwh": _number(energy.value.energy_kwh) if energy.available else None,
        }
    push = getattr(coordinator, "cloud_push", None)
    if push is not None:
        result["cloud_push"] = {
            "connected": bool(push.connected),
            "subscription_count": _number(getattr(push, "subscription_count", None)),
            "last_subscription_age_seconds": max(0, time.monotonic() - push.last_subscription_at)
            if getattr(push, "last_subscription_at", None) is not None else None,
            "polling": push.polling.diagnostics(),
            "refresh_count": push.refresh_count,
            "telemetry_events": push.event_counts["telemetry"],
            "charging_events": push.event_counts["charging"],
            "last_event_age_seconds": max(0, time.monotonic() - push.last_event_at)
            if push.last_event_at is not None else None,
        }
    return result
