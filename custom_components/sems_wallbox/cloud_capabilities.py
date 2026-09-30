"""Classify cloud controls without treating missing metadata as unsupported."""

from __future__ import annotations

from collections.abc import Mapping

from .const import CAP_DYNAMIC_LOAD_CONTROL, CAP_OUTPUT_POWER_SETTING, DOMAIN

# Shared cloud identities; the main power control and telemetry are not removed.
_CONTROLS = {
    "max_energy": ("number", "number-max-energy"),
    "min_energy": ("number", "number-min-energy"),
    "charge_target_soc": ("number", "number-target-soc"),
    "finish_time": ("select", "select-charge-duration"),
    "rated_max_charge_power": ("number", "number-output-power-limit"),
    "currentLimit": ("number", "number-current-limit"),
}
_MODE_TARGETS = frozenset(("max_energy", "min_energy", "charge_target_soc", "finish_time"))


def first_generation(capabilities: Mapping) -> bool:
    """Return whether metadata explicitly identifies generation 1.

    Args:
        capabilities: Discovered or previously stored device metadata.

    Returns:
        True only for generation 1; missing or unrecognized values stay unknown.
    """
    return str(capabilities.get("pile_generation") or "") == "1"


def control_support(capabilities: Mapping, field: str) -> bool | None:
    """Return a cloud setting's support, keeping incomplete metadata unknown.

    Args:
        capabilities: Runtime capabilities or the config entry's stored data.
        field: Normalized cloud configuration field.

    Returns:
        True or False when known, otherwise None. Empty or malformed capability
        lists cannot establish absence. Generation 1's mode form lacks targets;
        generation 2 supports them. Unknown generations keep legacy behavior.
    """
    generation = str(capabilities.get("pile_generation") or "")
    if field in _MODE_TARGETS:
        return {"1": False, "2": True}.get(generation)
    if field == "set_charge_power" and first_generation(capabilities):
        return True
    required = {
        "set_charge_power": CAP_OUTPUT_POWER_SETTING,
        "rated_max_charge_power": CAP_OUTPUT_POWER_SETTING,
        "currentLimit": CAP_DYNAMIC_LOAD_CONTROL,
    }.get(field)
    controls = capabilities.get("more_device_controls")
    if (required is None or not isinstance(controls, (list, tuple)) or not controls
            or not all(isinstance(item, str) and item for item in controls)):
        return None
    return required in controls


def unsupported_ids(capabilities: Mapping, platform: str, serial: str) -> list[str]:
    """List only positively unsupported cloud identities for one device.

    Args:
        capabilities: Discovered or stored device metadata.
        platform: HA entity domain, such as number or select.
        serial: Device serial used by the existing entity identities.

    Returns:
        Unique IDs suitable for scoped cleanup; unknown support is excluded.
    """
    return [
        f"{serial}-{suffix}"
        for field, (domain, suffix) in _CONTROLS.items()
        if domain == platform and control_support(capabilities, field) is False
    ]


def remove_unsupported(hass, config_entry, platform: str, unique_ids: list[str]) -> None:
    """Remove proven unsupported entries owned by the current configuration.

    Args:
        hass: Home Assistant instance.
        config_entry: Owner of the device being set up.
        platform: Entity domain, such as number or select.
        unique_ids: Proven unsupported identities from unsupported_ids.
    """
    if not unique_ids:
        return
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        entity = registry.async_get(entity_id) if entity_id is not None else None
        if entity is not None and entity.config_entry_id == config_entry.entry_id:
            registry.async_remove(entity_id)
