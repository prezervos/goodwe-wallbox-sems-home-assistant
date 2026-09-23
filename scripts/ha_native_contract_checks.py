"""Real HA entity contracts and failure recovery, inspired by core integrations."""

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.icon import async_get_icons
from homeassistant.helpers.translation import async_get_translations


async def check_contracts(hass, entry, entities, cloud, serial):
    """Check user-visible contracts using real HA registries and a simulated API.

    Args:
        hass: Running isolated Home Assistant test instance.
        entry: Loaded test entry.
        entities: Registered entities for that entry.
        cloud: Controllable fake cloud client.
        serial: Synthetic device identifier.
    """
    from custom_components.sems_wallbox.cloud_observation import (
        CloudAuthenticationError,
    )

    owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
    registry = er.async_get(hass)
    by_key = {("completion_time" if e.domain == "select" and e.translation_key == "charge_duration" else e.translation_key): e for e in entities}
    expected = {
        "start_charging": ("switch", None, False),
        "native_connection": ("switch", "config", False),
        "charge_mode": ("select", None, False),
        "charge_power": ("number", None, False),
        "status": ("sensor", None, False),
        "energy": ("sensor", None, False),
        "current_limit": ("number", "config", False),
        "output_power_limit": ("number", "config", False),
        "max_session_energy": ("number", "config", False),
        "min_session_energy": ("number", "config", False),
        "charge_target_soc": ("number", "config", False),
        "completion_time": ("select", "config", False),
        "dynamic_load_control": ("switch", "config", False),
        "ensure_minimum_charging_power": ("switch", "config", False),
        "workstate": ("sensor", None, False),
        "power": ("sensor", None, False),
        "set_charge_power_limit": ("sensor", "diagnostic", False),
        "charge_duration": ("sensor", None, False),
        "native_fault": ("sensor", None, False),
        "active_transport": ("sensor", None, False),
        "native_total_energy": ("sensor", "diagnostic", True),
        "charging_active": ("binary_sensor", None, True),
    }
    for phase in "abc":
        expected["current_" + phase] = ("sensor", "diagnostic", True)
        expected["voltage_" + phase] = ("sensor", "diagnostic", True)
    assert set(by_key) == set(expected), (set(by_key), set(expected))
    devices = {e.device_id for e in entities}
    assert len(devices) == 1 and None not in devices
    assert len({e.unique_id for e in entities}) == len(entities)
    for key, (domain, category, disabled) in expected.items():
        item = by_key[key]
        assert item.domain == domain
        assert (item.entity_category.value if item.entity_category else None) == category
        assert (item.disabled_by is er.RegistryEntryDisabler.INTEGRATION) == disabled
    for key, unit in [("power", "kW"), ("charge_power", "kW"),
                      ("set_charge_power_limit", "kW"), ("charge_duration", "min")]:
        assert hass.states.get(by_key[key].entity_id).attributes["unit_of_measurement"] == unit
    for language, status_name in [("en", "Wallbox status"), ("cs", "Stav wallboxu")]:
        catalog = await async_get_translations(hass, language, "entity", {"sems_wallbox"})
        assert catalog["component.sems_wallbox.entity.sensor.status.name"] == status_name
        activity = "component.sems_wallbox.entity.binary_sensor.charging_active.state."
        expected_activity = ("Nabíjí", "Nenabíjí") if language == "cs" else ("Charging", "Not charging")
        assert catalog[activity + "on"] == expected_activity[0]
        assert catalog[activity + "off"] == expected_activity[1]

    icons = await async_get_icons(hass, "entity", {"sems_wallbox"})
    # Validate through HA's actual icon loader, not only the source JSON parser.
    icons = icons["sems_wallbox"]
    assert icons["switch"]["start_charging"]["default"] == "mdi:ev-plug-type2"
    assert icons["number"]["charge_power"]["default"] == "mdi:speedometer"
    assert icons["select"]["charge_mode"]["state"]["pv_priority"] == "mdi:solar-power"
    assert icons["select"]["charge_mode"]["state"]["pv_and_battery"] == "mdi:battery-charging"
    print("PASS: complete entity contract, device association, units, defaults and HA icon loader")

    # A user name must survive coordinator updates; entity IDs are not labels.
    status = by_key["status"]
    registry.async_update_entity(status.entity_id, name="My own charger status")
    await owner.async_refresh()
    await hass.async_block_till_done()
    assert "My own charger status" in hass.states.get(status.entity_id).attributes["friendly_name"]
    assert registry.async_get(status.entity_id).unique_id == serial
    registry.async_update_entity(status.entity_id, name=None)

    power_id = by_key["power"].entity_id
    cloud.error = ConnectionError("simulated network outage")
    await owner.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(power_id).state == STATE_UNAVAILABLE
    assert hass.states.get(by_key["active_transport"].entity_id).state == "connection_unavailable"
    assert not hass.config_entries.flow.async_progress()
    cloud.error = None
    await owner.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(power_id).state == "0.0"
    assert owner.automatic_fallback.failures == 0

    cloud.error = CloudAuthenticationError("simulated rejected credentials")
    for _ in range(2):
        await owner.async_refresh()
        await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1 and flows[0]["step_id"] == "reauth_confirm"
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert owner.automatic_fallback.blocked and not owner.local
    assert hass.states.get(power_id).state == STATE_UNAVAILABLE
    hass.config_entries.flow.async_abort(flows[0]["flow_id"])
    cloud.error = None
    await owner.async_refresh()
    await hass.async_block_till_done()
    assert not owner.automatic_fallback.blocked and owner.last_update_success
    print("PASS: unavailable/recovered entities, custom name preservation, one reauth flow, no auth fallback")
