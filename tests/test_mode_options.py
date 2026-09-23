"""Exercise actual config/options flow with small HA framework stubs."""

import importlib
import sys
import types
from pathlib import Path

import pytest
import voluptuous as vol


@pytest.fixture
def flow_module(monkeypatch):
    import homeassistant.config_entries as entries

    class Flow:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return kwargs

        def async_create_entry(self, **kwargs):
            return kwargs

        def async_abort(self, **kwargs):
            return kwargs

        def _async_current_entries(self):
            return []

        async def async_set_unique_id(self, value):
            self.unique_id = value

        def _abort_if_unique_id_configured(self):
            pass

    monkeypatch.setattr(entries, "ConfigFlow", Flow, raising=False)
    monkeypatch.setattr(entries, "OptionsFlow", Flow, raising=False)
    monkeypatch.setattr(entries, "CONN_CLASS_CLOUD_POLL", "cloud_poll", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.config_validation",
        types.ModuleType("homeassistant.helpers.config_validation"),
    )
    name = "sems_mode_flow_tests"
    package = types.ModuleType(name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "sems_wallbox")
    ]
    monkeypatch.setitem(sys.modules, name, package)
    for key in list(sys.modules):
        if key.startswith(name + "."):
            monkeypatch.delitem(sys.modules, key)
    return importlib.import_module(name + ".config_flow")


async def test_initial_form_is_opt_in_without_initial_mode(flow_module):
    flow = flow_module.ConfigFlow()
    result = await flow.async_step_user()
    fields = [key.schema for key in result["data_schema"].schema]
    assert "initial_charge_mode" not in fields
    data = result["data_schema"]({"connection_type": "cloud"})
    assert data["remember_charge_mode"] is False
    assert "initial_charge_mode" not in data
    with pytest.raises(vol.Invalid):
        result["data_schema"]({"connection_type": "cloud", "initial_charge_mode": 7})


async def test_initial_choices_saved_into_cloud_entry(flow_module):
    flow = flow_module.ConfigFlow()
    await flow.async_step_user(
        {
            "connection_type": "cloud",
            "remember_charge_mode": True,
        }
    )
    data = flow._build_entry_data("SN")
    assert data["remember_charge_mode"] is True
    assert "initial_charge_mode" not in data


async def test_options_inherit_initial_choices_and_preserve_unknown_options(
    flow_module,
):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(
        options={"future_option": "preserve"},
        data={"remember_charge_mode": True, "initial_charge_mode": 1},
    )
    result = await flow.async_step_init()
    fields = [key.schema for key in result["data_schema"].schema]
    assert "initial_charge_mode" not in fields
    data = result["data_schema"]({})
    assert data["remember_charge_mode"] is True
    assert "initial_charge_mode" not in data
    result = await flow.async_step_init(data)
    assert result["data"]["future_option"] == "preserve"


async def test_options_override_setup_preferences(flow_module):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(
        options={"remember_charge_mode": False, "initial_charge_mode": 2},
        data={"remember_charge_mode": True, "initial_charge_mode": 0},
    )
    result = await flow.async_step_init()
    data = result["data_schema"]({})
    assert data["remember_charge_mode"] is False
    assert "initial_charge_mode" not in data


async def test_native_options_preserve_cloud_credentials_and_entry_identity(
    flow_module, monkeypatch
):
    from unittest.mock import AsyncMock

    entry_data = {
        "wallbox_serial_No": "TEST000000000001",
        "username": "owner",
        "password": "secret",
    }
    flow = flow_module.OptionsFlowHandler()
    capture = AsyncMock()
    flow.hass = types.SimpleNamespace(data={flow_module.DOMAIN: {"entry": {
        "coordinator": types.SimpleNamespace(last_update_success=True,
            charge_mode_policy=types.SimpleNamespace(async_adopt_current_mode=capture))}}})
    flow.config_entry = types.SimpleNamespace(
        entry_id="entry", data=entry_data.copy(), options={"future_option": "keep"}
    )
    result = await flow.async_step_init(
        {"native_enabled": True, "remember_charge_mode": True}
    )
    assert result["step_id"] == "native"
    capture.assert_not_awaited()
    assert "data" not in result
    assert flow.config_entry.options == {"future_option": "keep"}
    validated = {
        "wallbox_serial_No": entry_data["wallbox_serial_No"],
        "native_host": "192.0.2.10",
    }
    validate = AsyncMock(return_value=validated)
    monkeypatch.setattr(flow_module, "validate_native", validate)
    result = await flow.async_step_native(
        {"native_host": "192.0.2.10", "wallbox_serial_No": "WRONG"}
    )
    assert (
        validate.call_args.args[0]["wallbox_serial_No"]
        == entry_data["wallbox_serial_No"]
    )
    assert flow.config_entry.data == entry_data
    assert result["data"]["future_option"] == "keep"
    capture.assert_awaited_once()
    assert result["data"]["native_enabled"] is True
    assert "wallbox_serial_No" not in result["data"]


async def test_native_options_failed_discovery_does_not_save(flow_module, monkeypatch):
    from unittest.mock import AsyncMock

    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(
        data={"wallbox_serial_No": "TEST000000000001"}, options={}
    )
    await flow.async_step_init({"native_enabled": True})
    monkeypatch.setattr(
        flow_module,
        "validate_native",
        AsyncMock(side_effect=ValueError("Wrong serial")),
    )
    result = await flow.async_step_native({"native_host": "192.0.2.10"})
    assert result["errors"] == {"base": "native_configuration_failed"}
    assert "data" not in result


async def test_native_only_entry_cannot_disable_its_only_transport(flow_module):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(data={"native_enabled": True}, options={})
    result = await flow.async_step_init({"native_enabled": False})
    assert result["reason"] == "cloud_credentials_required"


async def test_native_duplicate_serial_is_rejected_before_creation(
    flow_module, monkeypatch
):
    from unittest.mock import AsyncMock

    flow = flow_module.ConfigFlow()
    serial = "TEST000000000001"
    flow._async_current_entries = lambda: [
        types.SimpleNamespace(data={"wallbox_serial_No": serial})
    ]
    monkeypatch.setattr(
        flow_module,
        "validate_native",
        AsyncMock(return_value={"wallbox_serial_No": serial}),
    )
    result = await flow.async_step_native({})
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, ("detected-plant", "GW11K-HCA")),
        ({"plant_id": "", "product_model": " "}, ("", "")),
        (
            {"plant_id": "manual-plant", "product_model": "GW22K-HCA"},
            ("manual-plant", "GW22K-HCA"),
        ),
    ],
)
async def test_identity_options_prefill_detected_values_and_allow_override(
    flow_module, overrides, expected
):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(
        options=overrides,
        data={"plant_id": "detected-plant", "product_model": "GW11K-HCA"},
    )
    result = await flow.async_step_init()
    values = result["data_schema"]({})
    assert (values["plant_id"], values["product_model"]) == expected
    values.update(plant_id=" edited-plant ", product_model=" GW7K-HCA ")
    saved = await flow.async_step_init(values)
    assert saved["data"]["plant_id"] == "edited-plant"
    assert saved["data"]["product_model"] == "GW7K-HCA"


async def test_identity_options_use_runtime_detection_without_network(flow_module):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(entry_id="entry", options={}, data={})
    flow.hass = types.SimpleNamespace(
        data={
            flow_module.DOMAIN: {
                "entry": {
                    "coordinator": types.SimpleNamespace(
                        resolved_device_identity={
                            "plant_id": "runtime-plant",
                            "product_model": "GW11K-HCA",
                        }
                    )
                }
            }
        }
    )
    result = await flow.async_step_init()
    values = result["data_schema"]({})
    assert values["plant_id"] == "runtime-plant"
    assert values["product_model"] == "GW11K-HCA"


@pytest.mark.parametrize("serial,valid", [
    ("5011KHCA-TEST", True), ("5022KHCA-TEST", True),
    ("57000HCA-TEST", True), ("UNKNOWN-TEST", False),
])
async def test_native_configuration_validates_the_identified_model(flow_module, serial, valid):
    data = {
        "native_host": "192.0.2.40", "native_advertised_host": "192.0.2.41",
        "native_port": 18899, "native_discover": False,
        flow_module.CONF_STATION_ID: serial,
    }
    fields = [key.schema for key in flow_module.native_schema(data).schema]
    assert "native_initial_power" not in fields
    if valid:
        result = await flow_module.validate_native(data)
        assert "native_initial_power" not in result
    else:
        with pytest.raises(ValueError):
            await flow_module.validate_native(data)


async def test_blank_native_ingress_is_normalized_to_direct_connection(flow_module):
    data = {
        "native_host": "192.0.2.40",
        "native_advertised_host": "192.0.2.41",
        "native_port": 18899,
        "native_initial_power": 4.2,
        "native_discover": False,
        "native_ingress_peer": "   ",
        flow_module.CONF_STATION_ID: "5011KHCA-TEST",
    }
    result = await flow_module.validate_native(data)
    assert result["native_ingress_peer"] == ""

async def test_reconfigure_cloud_preserves_identity_and_preferences(flow_module, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    entry = types.SimpleNamespace(entry_id="existing", data={
        "wallbox_serial_No": "original", "username": "old", "password": "secret",
        "remember_charge_mode": True}, options={"initial_charge_mode": 2}, update_listeners=[object()])
    flow = flow_module.ConfigFlow()
    async def execute(fn, *args):
        return fn(*args)
    flow.hass = types.SimpleNamespace(data={}, async_add_executor_job=execute)
    api = types.SimpleNamespace(close=Mock(), test_authentication=lambda: True,
                                fetch_status_observation=lambda sn: {"sn": sn})
    monkeypatch.setattr(flow_module, "SemsApi", lambda *args: api)
    flow.async_update_and_abort = Mock(return_value={"type": "abort"})
    result = await flow._async_connection_change(
        entry, {"username": "new", "password": "new-secret"}, reauth=False)
    assert result["type"] == "abort"
    call = flow.async_update_and_abort.call_args
    assert call.args == (entry,)
    assert call.kwargs["data_updates"] == {"username": "new", "password": "new-secret"}
    assert call.kwargs["options"] == entry.options
    assert entry.data["wallbox_serial_No"] == "original"
    api.close.assert_called_once_with()


@pytest.mark.parametrize("failure,error", [("auth", "invalid_auth"), ("identity", "wrong_device")])
async def test_reconfigure_does_not_save_invalid_account(flow_module, monkeypatch, failure, error):
    from unittest.mock import Mock
    flow = flow_module.ConfigFlow()
    async def execute(fn, *args):
        return fn(*args)
    flow.hass = types.SimpleNamespace(data={}, async_add_executor_job=execute)
    entry = types.SimpleNamespace(entry_id="existing", data={
        "wallbox_serial_No": "original", "username": "old", "password": "secret"}, options={})
    api = types.SimpleNamespace(close=Mock(), test_authentication=lambda: failure != "auth",
                                fetch_status_observation=lambda sn: {"sn": "other"})
    monkeypatch.setattr(flow_module, "SemsApi", lambda *args: api)
    flow.async_update_and_abort = Mock()
    result = await flow._async_connection_change(entry, {"username": "new"}, reauth=True)
    assert result["errors"] == {"base": error}
    flow.async_update_and_abort.assert_not_called()
    assert "secret" not in str(result["data_schema"])
    api.close.assert_called_once_with()


@pytest.mark.parametrize("local,transitioning,journal", [(True, False, None),
    (False, True, None), (False, False, {"owned": True})])
async def test_connection_changes_blocked_while_owned(flow_module, local, transitioning, journal):
    entry = types.SimpleNamespace(entry_id="existing", data={}, options={})
    owner = types.SimpleNamespace(local=local, transitioning=transitioning,
                                  endpoint=types.SimpleNamespace(journal=journal))
    flow = flow_module.ConfigFlow()
    flow.hass = types.SimpleNamespace(data={"sems_wallbox": {"existing": {"coordinator": owner}}})
    assert (await flow._async_connection_change(entry, {}, reauth=False))["reason"] == "connection_change_busy"

async def test_reconfigure_rechecks_ownership_after_network_read(flow_module, monkeypatch):
    from unittest.mock import Mock
    owner = types.SimpleNamespace(local=False, transitioning=False,
                                  endpoint=types.SimpleNamespace(journal=None))
    flow = flow_module.ConfigFlow()
    async def execute(fn, *args):
        return fn(*args)
    flow.hass = types.SimpleNamespace(data={"sems_wallbox": {"entry": {"coordinator": owner}}},
                                    async_add_executor_job=execute)
    entry = types.SimpleNamespace(entry_id="entry", data={
        "wallbox_serial_No": "original", "username": "old", "password": "secret"}, options={})
    def read(serial):
        owner.local = True
        return {"sn": serial}
    api = types.SimpleNamespace(close=Mock(), test_authentication=lambda: True, fetch_status_observation=read)
    monkeypatch.setattr(flow_module, "SemsApi", lambda *args: api)
    flow.async_update_and_abort = Mock()
    result = await flow._async_connection_change(entry, {"username": "new"}, reauth=False)
    assert result["reason"] == "connection_change_busy"
    flow.async_update_and_abort.assert_not_called()


async def test_local_only_setup_rejects_automatic_cloud_fallback(flow_module):
    flow = flow_module.ConfigFlow()
    result = await flow.async_step_native({"native_auto_fallback": True})
    assert result["reason"] == "cloud_credentials_required"


async def test_local_only_options_reject_automatic_cloud_fallback(flow_module):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(
        data={"native_enabled": True, "wallbox_serial_No": "5011KHCA-TEST"}, options={}
    )
    await flow.async_step_init({"native_enabled": True})
    result = await flow.async_step_native({"native_auto_fallback": True})
    assert result["reason"] == "cloud_credentials_required"


async def test_disabling_native_saves_first_page_without_second_form(flow_module):
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(
        data={"username": "owner", "native_enabled": True},
        options={"native_auto_fallback": True, "native_host": "192.0.2.10"},
    )
    result = await flow.async_step_init({"native_enabled": False, "scan_interval": 60})
    assert "step_id" not in result
    assert result["data"] == {
        "native_enabled": False, "scan_interval": 60,
        "native_auto_fallback": True, "native_host": "192.0.2.10",
    }


async def test_reauth_form_remains_available_while_tcp_owns_endpoint(flow_module):
    flow = flow_module.ConfigFlow()
    entry = types.SimpleNamespace(entry_id="entry", data={
        "username": "old", "password": "secret", "native_enabled": True}, options={})
    flow.hass = types.SimpleNamespace(data={flow_module.DOMAIN: {"entry": {
        "coordinator": types.SimpleNamespace(local=True, transitioning=False,
                                             endpoint=types.SimpleNamespace(journal={"owned": True}))}}})
    result = await flow._async_connection_change(entry, None, reauth=True)
    assert result["step_id"] == "reauth_confirm"
    assert not result["errors"]


@pytest.mark.parametrize("creation", ["native", "cloud_model", "cloud_discovered", "modbus"])
async def test_every_creation_path_rejects_existing_serial(flow_module, monkeypatch, creation):
    from unittest.mock import AsyncMock
    serial = "5011KHCA-TEST"
    flow = flow_module.ConfigFlow()
    flow._async_current_entries = lambda: [types.SimpleNamespace(data={"wallbox_serial_No": serial})]
    if creation == "native":
        monkeypatch.setattr(flow_module, "validate_native", AsyncMock(return_value={"wallbox_serial_No": serial}))
        result = await flow.async_step_native({})
    elif creation == "cloud_model":
        flow._pending_sn = serial
        result = await flow.async_step_model({"product_model": "GW11K-HCA"})
    elif creation == "cloud_discovered":
        flow._charger_capabilities[serial] = {"model": "known"}
        flow._charger_sn_to_model[serial] = "GW11K-HCA"
        result = await flow._finish_or_model_step(serial)
    else:
        module = importlib.import_module(flow_module.__package__ + ".wallbox_modbus")
        monkeypatch.setattr(module.WallboxModbusClient, "read_all", lambda self: {"sn": serial})
        async def execute(function, *args):
            return function(*args)
        flow.hass = types.SimpleNamespace(async_add_executor_job=execute)
        result = await flow.async_step_modbus({"modbus_host": "192.0.2.10", "modbus_device_id": 247})
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("values,valid", [
    ({"modbus_host": " 192.0.2.1 ", "modbus_port": 502, "modbus_device_id": 0}, True),
    ({"modbus_host": "wallbox.local", "modbus_port": 65535, "modbus_device_id": 255}, True),
    ({"modbus_host": " ", "modbus_port": 502, "modbus_device_id": 1}, False),
    ({"modbus_host": "192.0.2.1", "modbus_port": -1, "modbus_device_id": 1}, False),
    ({"modbus_host": "192.0.2.1", "modbus_port": 65536, "modbus_device_id": 1}, False),
    ({"modbus_host": "192.0.2.1", "modbus_port": 502, "modbus_device_id": 256}, False),
])
def test_modbus_setup_and_reconfigure_share_validation(flow_module, values, valid):
    schemas = [flow_module._STEP_MODBUS_SCHEMA, flow_module.ConfigFlow._connection_schema(
        {"connection_type": "modbus", "modbus_host": "192.0.2.1"}, reauth=False)]
    for schema in schemas:
        if valid:
            assert schema(values)["modbus_host"] == values["modbus_host"].strip()
        else:
            with pytest.raises(vol.Invalid):
                schema(values)


async def test_blank_manual_serial_is_rejected_before_lookup(flow_module):
    from unittest.mock import AsyncMock
    flow = flow_module.ConfigFlow()
    flow._finish_or_model_step = AsyncMock()
    result = await flow.async_step_charger_manual({"wallbox_serial_No": "  "})
    assert result["errors"] == {"wallbox_serial_No": "invalid_serial"}
    flow._finish_or_model_step.assert_not_awaited()
    assert await flow._async_create_serial_entry("", {}) == {"reason": "invalid_serial"}


@pytest.mark.parametrize("old,new,calls", [(False,True,1),(True,True,0),(True,False,0),(False,False,0)])
async def test_mode_capture_occurs_only_on_explicit_enable(flow_module, old, new, calls):
    from unittest.mock import AsyncMock
    capture = AsyncMock()
    flow = flow_module.OptionsFlowHandler()
    flow.config_entry = types.SimpleNamespace(entry_id="entry", data={}, options={"remember_charge_mode":old})
    flow.hass = types.SimpleNamespace(data={flow_module.DOMAIN:{"entry":{
        "coordinator":types.SimpleNamespace(last_update_success=True,
            charge_mode_policy=types.SimpleNamespace(async_adopt_current_mode=capture))}}})
    result = await flow.async_step_init({"native_enabled":False,"remember_charge_mode":new})
    assert result["data"]["remember_charge_mode"] is new
    assert capture.await_count == calls


async def test_failed_mode_capture_does_not_save_enabled_option(flow_module):
    from unittest.mock import AsyncMock
    flow = flow_module.OptionsFlowHandler()
    entry = types.SimpleNamespace(entry_id="entry",data={},options={"remember_charge_mode":False})
    flow.config_entry = entry
    flow.hass = types.SimpleNamespace(data={flow_module.DOMAIN:{"entry":{
        "coordinator":types.SimpleNamespace(last_update_success=True,
            charge_mode_policy=types.SimpleNamespace(async_adopt_current_mode=AsyncMock(side_effect=OSError())))}}})
    result = await flow.async_step_init({"native_enabled":False,"remember_charge_mode":True})
    assert result["errors"] == {"base":"mode_capture_failed"}
    assert "data" not in result
    assert entry.options == {"remember_charge_mode":False}
