"""Shared SEMS minimum-power encoding for cloud and combined runtimes."""


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
