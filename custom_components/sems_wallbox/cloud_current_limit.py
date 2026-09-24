"""Resolve the SEMS+ household import-current range without changing telemetry.

The official client uses per-device controlItemRanges, falling back to 0-2000 A.
See docs/CLOUD_CURRENT_LIMIT.md for the traced endpoint and client contract.
"""

import math
from decimal import Decimal, InvalidOperation

RANGE_KEY = "charge_pile_dynamic_load_import_current_limit"
DEFAULT_MIN = 0.0
DEFAULT_MAX = 2000.0
BLOCKED = "Cloud current-limit metadata is invalid or contradicts the reported value"
INVALID = "Cloud current limit is outside the supported range or precision"
MISSING = "The wallbox did not report this setting; no write was sent"


def observed_current(value: object) -> float | None:
    """Return finite nonnegative telemetry without rounding or coercing booleans."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def current_bounds(metadata: object = None) -> tuple[float, float]:
    """Resolve per-device bounds, using defaults only for absent/null fields.

    Args:
        metadata: Raw controlItemRanges mapping from SEMS+ device metadata.

    Returns:
        Inclusive minimum and maximum current in amperes.

    Raises:
        ValueError: Present metadata is malformed or has reversed bounds.
    """
    if metadata is None:
        return DEFAULT_MIN, DEFAULT_MAX
    if not isinstance(metadata, dict):
        raise ValueError(BLOCKED)
    item = metadata.get(RANGE_KEY)
    if item is None:
        return DEFAULT_MIN, DEFAULT_MAX
    if not isinstance(item, dict):
        raise ValueError(BLOCKED)
    minimum = DEFAULT_MIN if item.get("min") is None else observed_current(item["min"])
    maximum = DEFAULT_MAX if item.get("max") is None else observed_current(item["max"])
    if minimum is None or maximum is None or minimum > maximum:
        raise ValueError(BLOCKED)
    return minimum, maximum


def current_writable(reported: object, metadata: object = None) -> bool:
    """Require a real observation consistent with valid device bounds."""
    try:
        minimum, maximum = current_bounds(metadata)
    except ValueError:
        return False
    value = observed_current(reported)
    return value is not None and minimum <= value <= maximum


def validate_current_write(reported: object, requested: object, metadata: object = None) -> float:
    """Validate the same range as the UI; reject precision loss instead of truncating.

    Args:
        reported: Last device observation of currentLimit.
        requested: User-requested current in amperes.
        metadata: Raw controlItemRanges mapping.

    Returns:
        Validated current with at most two decimal places, as in SEMS+.

    Raises:
        ValueError: Missing observation, inconsistent metadata, or invalid request.
    """
    minimum, maximum = current_bounds(metadata)
    if observed_current(reported) is None:
        raise ValueError(MISSING)
    if not current_writable(reported, metadata):
        raise ValueError(BLOCKED)
    value = observed_current(requested)
    if value is None or not minimum <= value <= maximum:
        raise ValueError(INVALID)
    try:
        scaled = Decimal(str(requested)) * 100
        if scaled != scaled.to_integral_value():
            raise ValueError(INVALID)
    except InvalidOperation as error:
        raise ValueError(INVALID) from error
    return value


def current_attributes(reported: object, metadata: object = None) -> dict:
    """Expose actual reports even when inconsistent metadata disables a control."""
    valid = current_writable(reported, metadata)
    return {
        "reported_current_limit": observed_current(reported),
        "write_supported": valid,
        "write_restriction": None if valid else "missing_or_inconsistent_cloud_data",
    }
