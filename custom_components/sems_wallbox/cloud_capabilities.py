"""Match cloud entities to the controls SEMS+ offers for each wallbox generation."""

from __future__ import annotations

from .const import DOMAIN


def first_generation(capabilities: dict) -> bool:
    """Return whether SEMS+ reports the original (generation1) wallbox.

    SEMS+ only offers Fast power and minimum-power in the generation1 mode
    form; session energy, SOC and finish-time targets are generation2 fields.

    Args:
        capabilities: Runtime capabilities discovered for the config entry.

    Returns:
        True only for a reported generation "1"; unknown stays False.
    """
    return str(capabilities.get("pile_generation") or "") == "1"


def remove_unsupported(hass, platform: str, unique_ids: list[str]) -> None:
    """Remove registry entries earlier versions created for unsupported controls.

    Args:
        hass: Home Assistant instance.
        platform: Entity platform domain, e.g. "number".
        unique_ids: Unique IDs the device's capabilities no longer provide.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for unique_id in unique_ids:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)
