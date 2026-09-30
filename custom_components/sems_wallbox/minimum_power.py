"""Shared SEMS minimum-power encoding for cloud and combined runtimes."""


import math
import time

from .native_fallback import report_age
from .observed_state import charging_active


def write_minimum_power(api, serial, generation, reported, enabled, *, observation):
    """Write a reported setting without changing mode or inventing companion data.

    Args:
        api: Existing shared SEMS API client.
        serial: Expected wallbox identity.
        generation: Discovered product generation; unknown retains legacy encoding.
        reported: Fresh, identity-checked configuration response.
        enabled: Requested boolean setting.
        observation: Current coordinator telemetry, separate from configuration.

    Returns:
        Whether the cloud acknowledged the write; this is not device readback.

    Raises:
        ValueError: Identity, original setting, requested value or PV mode is invalid.
    """
    if (type(enabled) is not bool or not isinstance(reported, dict)
            or reported.get("sn") != serial
            or type(reported.get("ensure_minimum_charging_power")) is not bool):
        raise ValueError("The wallbox did not report a valid minimum-power setting")
    if str(generation) == "1":
        # HCA cloud mode writes also left the flag unchanged during charging.
        # Require confirmed idle; an absent report is not evidence of idle.
        if (charging_active(observation, local=False) is not False
                or observation.get("power") != 0):
            raise ValueError("Minimum-power write requires an idle wallbox")
        mode = reported.get("_reported_charge_mode")
        # Fast set-mode acknowledged the boolean but left the device flag unchanged
        # on the tested HCA. Keep the state visible without pretending this writes.
        if type(mode) is int and mode == 0:
            raise ValueError("Cloud minimum-power changes are not supported in Fast mode")
        if type(mode) is not int or mode not in (1, 2):
            raise ValueError("Minimum-power setting requires a reported PV charging mode")
        # SEMS+ generation1 uses boolean set-mode. set-config 0/170 ACKed
        # without changing the tested HCA flag; never send both encoders.
        return api.set_charge_mode_gen2(
            serial, mode, ensure_minimum_charging_power=enabled)
    return api.set_config_gen2(serial, ensureMinimumChargingPower=170 if enabled else 0)


def write_checked_minimum_power(api, serial, generation, enabled, zone):
    """Read independent current telemetry before an idle-only cloud write.

    The caller holds the shared API lock across reads and the write. A recent
    session-history zero or configured power is never evidence of present idle.
    The 60-second freshness bound is an integration control policy, not a
    claimed hardware guarantee.

    Args:
        api: Shared, serialized SEMS client.
        serial: Expected device identity.
        generation: Discovered product generation.
        enabled: Requested boolean setting.
        zone: Home Assistant timezone for timestamp interpretation.

    Returns:
        Whether the cloud acknowledged the write, without implying readback.

    Raises:
        ValueError: Configuration or current idle state cannot be verified.
    """
    reported = api.get_data_gen2(serial)
    observation = {}
    if str(generation) == "1":
        # Only generation1 uses compound set-mode. Generation2 set-config is
        # independent of mode companions and must not inherit their fence.
        api._confirm_previous_mode_edit(serial, reported)
        observation = api.fetch_status_observation(serial)
        try:
            if not isinstance(observation, dict) or observation.get("sn") != serial:
                raise ValueError("Missing device identity")
            raw = observation.get("power")
            if raw is None or isinstance(raw, bool):
                raise ValueError("Missing measured power")
            measured = float(raw)
            if not math.isfinite(measured) or measured < 0:
                raise ValueError("Invalid measured power")
            if report_age(observation, zone, time.time()) > 60:
                raise ValueError("Stale device report")
            config_mode = reported.get("_reported_charge_mode") if isinstance(reported, dict) else None
            observed_mode = observation.get("_reported_charge_mode", observation.get("chargeMode"))
            if (type(config_mode) is int and config_mode in (0, 1, 2)
                    and type(observed_mode) is int and observed_mode in (0, 1, 2)
                    and config_mode != observed_mode):
                raise ValueError("Conflicting current mode")
            if charging_active(observation, local=False) is None:
                raise ValueError("Unknown current state")
            observation = {**observation, "power": measured}
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Cannot verify fresh idle state for minimum-power write") from error
    return write_minimum_power(api, serial, generation, reported, enabled,
                               observation=observation)
