"""Translate service failures at the HA boundary; retain technical details in logs."""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError

from .cloud_rate_limit import CloudRateLimitedError

_LOGGER = logging.getLogger(__name__)

_MESSAGES = {
    "Cloud current-limit metadata is invalid or contradicts the reported value": "current_limit_range_unverified",
    "Cloud current limit is outside the supported range or precision": "current_limit_invalid",
    "Disable the wallbox schedule before changing Auto start": "auto_start_schedule",
    "Auto start change was not confirmed by the wallbox": "auto_start_unconfirmed",
    "Wallbox schedule changed during Auto start update": "auto_start_schedule_changed",
    "Auto start is available only over TCP on this wallbox": "auto_start_tcp_only",
    "Cloud minimum-power changes are not supported in Fast mode": "minimum_power_cloud_fast",
    "Minimum-power write requires an idle wallbox": "minimum_power_stop_first",
    "Minimum-power write requires idle mode with a known flag": "minimum_power_stop_first",
    "Stop charging before changing native mode settings": "stop_before_mode",
    "Stop charging before changing native mode": "stop_before_mode",
    "Wallbox stop state not confirmed; Start was not sent": "start_requires_idle",
    "A Start request is already pending": "start_pending",
    "A native Start is already awaiting charging": "start_pending",
    "Charging request superseded; Start was not sent": "request_superseded",
    "Setting superseded; command was not sent": "request_superseded",
    "No fresh confirmation of charging mode; Start was not sent": "mode_unconfirmed",
    "Stop was not acknowledged; check actual device state": "stop_unconfirmed",
    "Start was not acknowledged; it was not retried": "start_unconfirmed",
}


def operation_error(error: Exception) -> HomeAssistantError:
    """Return a localized service error while logging its technical cause.

    Args:
        error: Original failure, possibly wrapping a transport error.

    Returns:
        Home Assistant exception with a translatable UI message.
    """
    _LOGGER.warning("Wallbox operation failed: %s", error)
    key = _MESSAGES.get(str(error))
    cause = error
    while key is None:
        if isinstance(cause, CloudRateLimitedError):
            return HomeAssistantError(
                str(error), translation_domain="sems_wallbox",
                translation_key="cloud_rate_limited",
                translation_placeholders={"seconds": str(cause.retry_after)},
            )
        if isinstance(cause, TimeoutError):
            key = "operation_timeout"
            break
        if isinstance(cause, ConnectionError):
            key = "connection_failed"
            break
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    return HomeAssistantError(
        str(error),
        translation_domain="sems_wallbox",
        translation_key=key or "operation_failed",
    )
