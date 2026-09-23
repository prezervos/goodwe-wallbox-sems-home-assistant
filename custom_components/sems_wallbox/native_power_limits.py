"""Model-specific native power bounds and exact deci-kW validation."""

from __future__ import annotations

import math

# Reference firmware selects its current/phase profile from serial[1:8].
# Bounds match the corresponding GW7/GW11/GW22 cloud entity ranges.
_MODEL_LIMITS = {
    "011KHCA": (4.2, 11.0),
    "022KHCA": (4.2, 22.0),
    "7000HCA": (1.4, 7.0),
    "7000ACA": (1.4, 7.0),
}


def power_bounds(serial: str) -> tuple[float, float]:
    """Return supported power bounds without guessing an unknown model.

    Args:
        serial: Enrolled wallbox serial number.

    Returns:
        Minimum and maximum requested power in kW.

    Raises:
        ValueError: The serial does not identify a supported native profile.
    """
    try:
        return _MODEL_LIMITS[serial[1:8]]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Unknown native model; power range cannot be determined"
        ) from exc


def power_tenths(value: float, serial: str | None = None) -> int:
    """Validate a requested kW value without rounding an invalid user request.

    Args:
        value: Requested power in kW, in 0.1 kW increments.
        serial: Enrolled identity for model-specific bounds. Omission checks only
            the protocol-wide range; the transport must enforce model bounds.

    Returns:
        Integer power in tenths of a kW.

    Raises:
        ValueError: Nonfinite, nonnumeric, out-of-range or fractional-step input.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Power must be a finite number")
    if not math.isfinite(value):
        raise ValueError("Power must be a finite number")
    minimum, maximum = power_bounds(serial) if serial is not None else (1.4, 22.0)
    if not minimum <= value <= maximum:
        raise ValueError(f"Power must be between {minimum:g} and {maximum:g} kW")
    raw = round(value * 10)
    if not math.isclose(value * 10, raw, abs_tol=1e-8, rel_tol=0):
        raise ValueError("Power must use 0.1 kW increments")
    return raw
