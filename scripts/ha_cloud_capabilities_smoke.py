"""Verify capability cleanup against a real, isolated HA entity registry."""

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.sems_wallbox import number, select
from custom_components.sems_wallbox.native_cloud_settings import CloudSettings
from custom_components.sems_wallbox.native_entities import setup_platform


async def run():
    """Exercise both setup paths with unknown metadata and scoped removals."""
    with tempfile.TemporaryDirectory(prefix="ha-cloud-capabilities-") as folder:
        hass = HomeAssistant(folder)
        hass.config_entries = ConfigEntries(hass, {})
        dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        registry = er.async_get(hass)
        entries = [ConfigEntry(
            version=1, minor_version=1, domain="sems_wallbox", title=name,
            unique_id=name, source="user", discovery_keys={}, subentries_data=[],
            data={}, options={},
        ) for name in ("current", "other")]
        current, other = entries
        ids = {}

        def seed():
            for domain, suffix in (
                ("number", "number-max-energy"), ("number", "number-min-energy"),
                ("number", "number-target-soc"), ("number", "number-output-power-limit"),
                ("number", "number-current-limit"), ("select", "select-charge-duration"),
                ("number", "primary-power"), ("sensor", "session-energy"),
                ("sensor", "elapsed-duration"),
            ):
                owner = other if suffix == "number-target-soc" else current
                entity = registry.async_get_or_create(
                    domain, "sems_wallbox", f"SN-{suffix}", config_entry=owner,
                    suggested_object_id=suffix.replace("-", "_"))
                registry.async_update_entity(entity.entity_id, name=f"Custom {suffix}")
                ids[suffix] = entity.entity_id

        async def execute(_hass, function, *args):
            return function(*args)

        co = SimpleNamespace(
            serial="SN", config_entry=current, hass=hass,
            data={"SN": {"sn": "SN", "chargeMode": 0, "set_charge_power": 4.2}},
            last_update_success=True, cloud=Mock(),
        )
        co.cloud_settings = CloudSettings(co)
        api = Mock()
        api.fetch_device_info.return_value = {}
        runtime = {"coordinator": co, "api": api, "capabilities": {}}
        hass.data["sems_wallbox"] = {current.entry_id: runtime}
        lookup = {entry.entry_id: entry for entry in entries}
        try:
            with (
                patch.object(hass.config_entries, "async_get_entry", lookup.get),
                patch.object(number, "async_execute", execute),
                patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")),
            ):
                for combined in (False, True):
                    seed()
                    original_ids = dict(ids)
                    for caps in ({}, {"more_device_controls": []},
                                 {"more_device_controls": [None]}):
                        runtime["capabilities"] = caps
                        co.entry = SimpleNamespace(entry_id=current.entry_id, data=caps)
                        for platform, module in (("number", number), ("select", select)):
                            added = []
                            if combined:
                                setup_platform(platform, co, added.extend)
                            else:
                                await module.async_setup_entry(hass, current, added.extend)
                        assert all(registry.async_get(entity_id) is not None for entity_id in ids.values())

                    caps = {"pile_generation": "1", "more_device_controls": ["Dynamic_Load_Control"]}
                    runtime["capabilities"] = caps
                    co.entry = SimpleNamespace(entry_id=current.entry_id, data=caps)
                    for platform, module in (("number", number), ("select", select)):
                        added = []
                        if combined:
                            setup_platform(platform, co, added.extend)
                        else:
                            await module.async_setup_entry(hass, current, added.extend)
                        assert added
                    for suffix in ("number-max-energy", "number-min-energy",
                                   "number-output-power-limit", "select-charge-duration"):
                        assert registry.async_get(ids[suffix]) is None, suffix
                    for suffix in ("number-target-soc", "number-current-limit", "primary-power",
                                   "session-energy", "elapsed-duration"):
                        assert registry.async_get(ids[suffix]) is not None, suffix
                    seed()
                    assert ids == original_ids
                    for suffix, entity_id in ids.items():
                        assert registry.async_get(entity_id).name == f"Custom {suffix}"
                    print(f"PASS: {'combined' if combined else 'cloud-only'} unknown metadata, scoped cleanup, sensors and restored custom identities")
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    asyncio.run(run())
