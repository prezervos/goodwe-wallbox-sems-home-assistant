#!/usr/bin/env python3
"""Verify identity clearing and inherited polling without external network access."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed


async def check_modbus_forms(hass):
    """Exercise HA flow progression and frontend serialization without device I/O."""
    from probatio import to_field_list
    from homeassistant.helpers import config_validation as cv
    from custom_components.sems_wallbox import wallbox_modbus

    def serialize(form, step):
        assert form["type"] == "form" and form["step_id"] == step, form
        # This is the serializer used by HA's data-entry-flow HTTP response.
        fields = to_field_list(form["data_schema"], custom_serializer=cv.custom_serializer)
        json.dumps(fields)
        assert any(field["name"] == "modbus_host" for field in fields)

    with patch.object(wallbox_modbus, "WallboxModbusClient") as client, patch(
        "custom_components.sems_wallbox.async_setup_entry", new=AsyncMock(return_value=True)
    ):
        flow = await hass.config_entries.flow.async_init(
            "sems_wallbox", context={"source": "user"}
        )
        form = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"connection_type": "modbus", "remember_charge_mode": False}
        )
        serialize(form, "modbus")
        for blank in ("", "   "):
            form = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {"modbus_host": blank, "modbus_port": 502, "modbus_device_id": 0}
            )
            serialize(form, "modbus")
            assert form["errors"] == {"modbus_host": "connection_validation_failed"}
            client.assert_not_called()
            client.detect_device_id.assert_not_called()
        client.detect_device_id.return_value = None
        form = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"modbus_host": " wallbox.local ", "modbus_port": 502, "modbus_device_id": 0}
        )
        serialize(form, "modbus")
        assert form["errors"], form
        client.detect_device_id.assert_called_once_with("wallbox.local", 502)
        client.return_value.read_all.return_value = {"sn": "MODBUS-FORM-FIXTURE"}
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"modbus_host": " wallbox.local ", "modbus_port": 502, "modbus_device_id": 1}
        )
        assert result["type"] == "create_entry", result
        entry = result["result"]
        assert entry.data["modbus_host"] == "wallbox.local"
        client.assert_called_once_with("wallbox.local", 502, 1)
        await hass.async_block_till_done()
        client.reset_mock()
        flow = await hass.config_entries.flow.async_init(
            "sems_wallbox", context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        serialize(flow, "reconfigure")
        for blank in ("", "   "):
            form = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {"modbus_host": blank, "modbus_port": 502, "modbus_device_id": 1}
            )
            serialize(form, "reconfigure")
            assert form["errors"] == {"modbus_host": "connection_validation_failed"}
            client.assert_not_called()
        client.return_value.read_all.return_value = {"sn": "ANOTHER-WALLBOX"}
        values = {"modbus_host": " new-wallbox.local ", "modbus_port": 502, "modbus_device_id": 1}
        form = await hass.config_entries.flow.async_configure(flow["flow_id"], values)
        serialize(form, "reconfigure")
        assert form["errors"] == {"base": "wrong_device"}
        assert entry.data["modbus_host"] == "wallbox.local"
        client.return_value.read_all.return_value = {"sn": "MODBUS-FORM-FIXTURE"}
        with patch.object(hass.config_entries, "async_schedule_reload"):
            result = await hass.config_entries.flow.async_configure(flow["flow_id"], values)
        assert result["type"] == "abort" and result["reason"] == "reconfigure_successful", result
        assert entry.data["modbus_host"] == "new-wallbox.local"
        client.assert_called_with("new-wallbox.local", 502, 1)
    print("PASS: Modbus setup/reconfigure UI serialization, blank hosts, failed validation and corrected retry")


async def check_modbus_zero_power_setup(hass):
    """Load real Modbus platforms despite a zero initial limit, without writes."""
    from custom_components.sems_wallbox import WallboxModbusClient
    from homeassistant.helpers.storage import Store

    serial = "5007KCAA00000000"
    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Zero limit",
        unique_id=serial, source="user", discovery_keys={}, subentries_data=[],
        options={"remember_charge_mode": True}, data={
            "connection_type": "modbus", "modbus_host": "127.0.0.1",
            "modbus_port": 502, "modbus_device_id": 247,
            "wallbox_serial_No": serial,
        },
    )
    client = Mock(spec=WallboxModbusClient)
    client.read_all.return_value = {
        "sn": serial, "model": "GW7K-HCA-20", "source": "modbus",
        "set_charge_power": 0.0, "chargeMode": 0, "status": "available",
        "modbus_status_raw": 0, "modbus_status_name": "idle_no_plug",
        "modbus_power": 0.0, "modbus_car_connected": 0,
        "modbus_breaker_current": 63,
    }
    with patch("custom_components.sems_wallbox.WallboxModbusClient", return_value=client):
        try:
            await hass.config_entries.async_add(entry)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED, (entry.state, entry.reason)
            owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            assert owner.data[serial]["set_charge_power"] == 0.0
            assert owner.charge_mode_policy.desired_power is None
            store = Store(hass, 1, f"sems_wallbox.{entry.entry_id}.charge_mode")
            assert await store.async_load() is None
            assert any(state.attributes.get("device_class") == "power"
                       for state in hass.states.async_all("number"))
            current = [state for state in hass.states.async_all("number")
                       if state.attributes.get("unit_of_measurement") == "A"]
            assert len(current) == 1 and float(current[0].state) == 63
            assert current[0].attributes["min"] == 0
            assert current[0].attributes["max"] == 2000
            assert current[0].attributes["mode"] == "box"
            # Existing intent must still win after a reload with the same zero report.
            await owner.charge_mode_policy.async_seed_power(4.2)
            assert await hass.config_entries.async_reload(entry.entry_id)
            owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            assert owner.charge_mode_policy.desired_power == 4.2
            assert owner.data[serial]["set_charge_power"] == 0.0
            assert all(call[0] in {"read_all", "close"} for call in client.mock_calls)
        finally:
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
    print("PASS: Modbus zero-limit setup loads entities, performs no writes and preserves intent on reload")


async def check_fast_power_preparation(hass):
    """Verify real HA exposes writable PV-mode intent without device writes."""
    import logging
    from datetime import timedelta
    from homeassistant.helpers.entity_platform import EntityPlatform
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from custom_components.sems_wallbox.number import SemsNumber, ModbusMaxChargePowerNumber
    from custom_components.sems_wallbox.charge_mode_policy import ChargeModePolicy

    for modbus in (False, True):
        owner = DataUpdateCoordinator(hass, logging.getLogger(__name__), name="Power fixture", config_entry=None)
        serial = "POWER-FIXTURE"
        owner.data = {serial: {"sn": serial, "chargeMode": 1, "set_charge_power": 0,
                              "modbus_max_charging_power": 0, "modbus_power_spec": 1,
                              "min_charge_power": 4.2, "max_charge_power": 11}}
        owner.last_update_success = True
        owner.schedule_delayed_refresh = Mock()
        store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
        client = Mock()
        owner.charge_mode_policy = ChargeModePolicy(Mock(), store, enabled=False)
        entity = ModbusMaxChargePowerNumber(owner, serial, client) if modbus else SemsNumber(owner, serial, client, None)
        platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="number",
                                  platform_name="sems_wallbox", platform=None,
                                  scan_interval=timedelta(seconds=60), entity_namespace=None)
        await platform.async_add_entities([entity])
        assert entity.available
        await entity.async_set_native_value(4.2)
        entity.async_write_ha_state()
        state = hass.states.get(entity.entity_id)
        assert state.state == "4.2", state
        assert state.attributes["reported_power_limit"] == 0, state
        assert owner.data[serial]["chargeMode"] == 1
        assert owner.data[serial]["set_charge_power"] == 0
        assert not client.mock_calls, client.mock_calls
        assert store.async_save.call_args.args[0]["power"] == 4.2
        await platform.async_remove_entity(entity.entity_id)
        hass.states.async_remove(entity.entity_id)
    print("PASS: real HA cloud and Modbus numbers stage PV power without hardware writes")


async def check_cloud_current_limit(hass):
    """Validate real HA number state/range serialization without cloud access."""
    import logging
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.sems_wallbox.number import SemsCurrentLimitNumber
    from custom_components.sems_wallbox.native_cloud_settings import (
        CloudSettings, CloudSettingNumber, SETTINGS,
    )

    owner = DataUpdateCoordinator(hass, logging.getLogger(__name__), name="Current fixture", config_entry=None)
    owner.serial = "CURRENT-FIXTURE"
    owner.data = {owner.serial: {"sn": owner.serial, "currentLimit": 63}}
    owner.last_update_success = True
    owner.schedule_delayed_refresh = Mock()
    owner.local = owner.transitioning = owner._closed = False
    owner.cloud_restored_at = None
    owner.entry = SimpleNamespace(data={})
    owner.routing_epoch = 0
    async def serialized(operation, **kwargs):
        return await operation()
    owner.charge_mode_policy = SimpleNamespace(enabled=True, async_setting_write=serialized)
    owner.cloud = Mock()
    owner.cloud.fetch_device_info.return_value = {"productModel": "MODEL"}
    owner.cloud.get_data_gen2.return_value = {"sn": owner.serial, "currentLimit": 63}
    owner.cloud.set_config_gen2.return_value = True
    owner.cloud_settings = CloudSettings(owner)
    owner.cloud_settings.valid = True
    owner.cloud_settings.values = {"currentLimit": 63}
    descriptor = next(item for item in SETTINGS if item.field == "currentLimit")
    entities = [SemsCurrentLimitNumber(owner, owner.serial, owner.cloud),
                CloudSettingNumber(owner, descriptor)]
    from datetime import timedelta
    from homeassistant.helpers.entity_platform import EntityPlatform
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="number",
        platform_name="sems_wallbox", platform=None,
        scan_interval=timedelta(seconds=60), entity_namespace=None,
    )
    for entity in entities:
        await platform.async_add_entities([entity])
        entity.async_write_ha_state()
        state = hass.states.get(entity.entity_id)
        assert float(state.state) == 63, state
        assert (state.attributes["min"], state.attributes["max"], state.attributes["step"]) == (0, 2000, 0.01), state
        assert state.attributes["write_supported"] is True, state
        for value in (2001, 63.001):
            try:
                await entity.async_set_native_value(value)
            except HomeAssistantError as error:
                assert error.translation_key == "current_limit_invalid", error
            else:
                raise AssertionError("Unsupported cloud current write accepted")
        owner.cloud.set_config_gen2.assert_not_called()
        owner.cloud.get_data_gen2.assert_not_called()
        await entity.async_set_native_value(63.25)
        owner.cloud.set_config_gen2.assert_called_once_with(owner.serial, currentLimit=63.25)
        entity.async_write_ha_state()
        assert float(hass.states.get(entity.entity_id).state) == 63
        raw = owner.data[owner.serial] if isinstance(entity, SemsCurrentLimitNumber) else owner.cloud_settings.values
        raw["controlItemRanges"] = {"charge_pile_dynamic_load_import_current_limit": {"min": 0, "max": 32}}
        entity.async_write_ha_state()
        state = hass.states.get(entity.entity_id)
        assert state.state == "unavailable", state
        assert raw["currentLimit"] == 63  # Retained in cached diagnostics; HA hides extra attributes while unavailable.
        raw["controlItemRanges"] = None
        owner.cloud.reset_mock()
        await platform.async_remove_entity(entity.entity_id)
        hass.states.async_remove(entity.entity_id)
    print("PASS: both cloud current entities serialize 63 A, preserve decimal writes and disable conflicting metadata in real HA")


async def main(folder):
    """Exercise setup consumers and polling against simulated cloud data."""
    hass = HomeAssistant(folder)
    loader.async_setup(hass)
    hass.config.skip_pip = True
    await bootstrap.async_from_config_dict({}, hass)
    import custom_components.sems_wallbox as integration
    from custom_components.sems_wallbox import config_flow
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator

    await check_fast_power_preparation(hass)
    await check_cloud_current_limit(hass)
    await check_modbus_forms(hass)
    await check_modbus_zero_power_setup(hass)
    results = {}
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="sems_wallbox",
        title="Audit",
        unique_id="5011KHCA-AUDIT",
        source="user",
        discovery_keys={},
        subentries_data=[],
        options={"plant_id": "", "product_model": ""},
        data={
            "username": "fixture",
            "password": "fixture",
            "wallbox_serial_No": "5011KHCA-AUDIT",
            "plant_id": "old-plant",
            "product_model": "old-model",
            "native_host": "192.0.2.10",
            "native_advertised_host": "192.0.2.20",
            "native_port": 18899,
            "scan_interval": 123,
            "scan_interval_charging": 37,
        },
    )

    class Captured(Exception):
        """Stop setup after observing resolved identity, before device access."""

    class Client:
        """Provide telemetry while capturing identity resolution."""
        supports_timestamped_observation = True
        status = "Waiting"

        def configure_gen2(self, plant, model):
            """Record the setup result before external work can start."""
            results[current] = {"plant": plant, "model": model}
            raise Captured()

        def fetch_status_observation(self, serial):
            """Return independent simulated telemetry for interval selection."""
            return {
                "sn": serial,
                "status": self.status,
                "chargeMode": 1,
                "power": 0,
                "set_charge_power": 4.2,
            }

        get_data_gen2 = fetch_status_observation

    client = Client()
    with patch.object(integration, "_create_cloud_api", return_value=client):
        for current, setup in [
            ("blank_identity_cloud", integration._async_setup_cloud),
            ("blank_identity_native", integration._async_setup_native),
        ]:
            try:
                await setup(hass, entry)
            except Captured:
                pass

    class Options(config_flow.OptionsFlowHandler):
        """Expose the real form schema without starting an interactive flow."""
        @property
        def config_entry(self):
            """Return the test configuration entry."""
            return entry

        def async_show_form(self, **kwargs):
            """Capture the schema for default-value validation."""
            return kwargs

    flow = Options()
    flow.hass = hass
    form = await flow.async_step_init()
    defaults = form["data_schema"]({})
    results["blank_identity_form"] = {
        k: defaults[k] for k in ("plant_id", "product_model")
    }
    owner = NativeCoordinator(hass, entry, client)
    with patch.object(owner.cloud_settings, "observe_mode"):
        await owner._async_read_data()
    results["native_idle_interval_from_entry_data"] = {
        "configured": 123,
        "actual": owner.update_interval.total_seconds(),
    }
    client.status = "charging"
    with patch.object(owner.cloud_settings, "observe_mode"):
        await owner._async_read_data()
    results["native_charging_interval_from_entry_data"] = (
        owner.update_interval.total_seconds()
    )
    owner.entry = SimpleNamespace(
        options={"scan_interval_charging": 19}, data=entry.data
    )
    with patch.object(owner.cloud_settings, "observe_mode"):
        await owner._async_read_data()
    results["native_charging_option_overrides_data"] = (
        owner.update_interval.total_seconds()
    )
    owner.transport.session_guard.limit = 4.2
    owner.local = True
    try:
        await owner._async_read_data()
    except UpdateFailed as error:
        assert "telemetry is unavailable" in str(error)
    results["tcp_active_interval"] = owner.update_interval.total_seconds()
    await owner.cloud_settings.close()
    assert results["blank_identity_cloud"] == {"plant": None, "model": None}
    assert results["blank_identity_native"] == {"plant": None, "model": None}
    assert results["blank_identity_form"] == {"plant_id": "", "product_model": ""}
    assert results["native_idle_interval_from_entry_data"]["actual"] == 123
    assert results["native_charging_interval_from_entry_data"] == 37
    assert results["native_charging_option_overrides_data"] == 19
    assert results["tcp_active_interval"] == 2
    print(json.dumps({"result": "PASS", "checks": results}, indent=2))
    await hass.async_stop(force=True)


with tempfile.TemporaryDirectory(prefix="goodwe-config-audit-") as folder:
    (Path(folder) / "custom_components").symlink_to(Path(sys.argv[1]).resolve())
    sys.path.insert(0, folder)
    with patch(
        "requests.sessions.Session.request",
        side_effect=AssertionError("External HTTP forbidden"),
    ):
        asyncio.run(main(folder))
