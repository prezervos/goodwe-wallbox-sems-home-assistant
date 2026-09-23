"""Preserve independently reported cloud mode targets during partial edits."""

import math

from .charge_mode_policy import ModeVerificationError


def preserved_mode_parameters(values, mode):
    """Return confirmed parameters required by this mode, including real zeros.

    Args:
        values: Independently reported SEMS configuration.
        mode: Explicit reported mode (0, 1 or 2).

    Returns:
        Keyword arguments accepted by the mode endpoint.

    Raises:
        ModeVerificationError: The mode or a required parameter is unknown.
    """
    if type(mode) is not int or mode not in (0, 1, 2):
        raise ModeVerificationError("The wallbox did not report a valid mode")
    fields = {"max_energy": "max_energy"}
    if mode in (0, 2):
        fields["soc_target"] = "charge_target_soc"
    if mode in (1, 2):
        fields.update(min_energy="min_energy", finish_time="finish_time")
    params = {}
    for parameter, field in fields.items():
        raw = values.get(field)
        try:
            value = float(raw) if raw is not None and not isinstance(raw, bool) else math.nan
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            raise ModeVerificationError("Cannot preserve unreported mode settings")
        params[parameter] = str(int(value)) if parameter == "finish_time" else int(value)
    return params
