"""Cloud compatibility tests with no network or wallbox writes."""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_native_transport import PACKAGE

settings = importlib.import_module(PACKAGE + ".native_cloud_settings")


def owner():
    """Build one shared-session owner with separate configuration telemetry."""

    async def execute(function, *args):
        return function(*args)

    async def serialized(operation):
        return await operation()

    result = SimpleNamespace(
        serial="TEST",
        local=False,
        transitioning=False,
        _closed=False,
        cloud_restored_at=None,
        routing_epoch=0,
        last_update_success=True,
        data={"TEST": {"power": 0.0, "lastUpdate": "original"}},
        entry=SimpleNamespace(data={}),
        hass=SimpleNamespace(async_add_executor_job=execute),
        async_update_listeners=Mock(),
        charge_mode_policy=SimpleNamespace(
            desired_power=4.2, async_setting_write=serialized
        ),
        cloud=SimpleNamespace(
            get_data_gen2=Mock(),
            fetch_device_info=Mock(return_value={"productModel": "MODEL"}),
            set_config_gen2=Mock(return_value=True),
            set_charge_mode_gen2=Mock(return_value=True),
        ),
    )
    result.cloud_settings = settings.CloudSettings(result)
    result.cloud.get_data_gen2.return_value = {
        "sn": "TEST",
        "_reported_charge_mode": 0,
        "max_energy": 20,
        "min_energy": 3,
        "charge_target_soc": 10,
        "finish_time": "2",
        "set_charge_power": 11,
        "dynamicLoad": False,
        "currentLimit": 16,
        "rated_max_charge_power": 11,
    }
    return result


@pytest.mark.parametrize(
    "field,value,method,payload",
    [
        ("currentLimit", 12, "set_config_gen2", {"currentLimit": 12}),
        ("dynamicLoad", 1, "set_config_gen2", {"dynamicLoad": 1}),
        (
            "rated_max_charge_power",
            7.0,
            "set_config_gen2",
            {"ratedMaxiChargePower": 7.0},
        ),
        (
            "max_energy",
            15,
            "set_charge_mode_gen2",
            {"max_energy": 15, "soc_target": 10},
        ),
    ],
)
@pytest.mark.asyncio
async def test_write_preserves_other_settings_and_saved_power(
    field, value, method, payload
):
    instance = owner()
    descriptor = next(item for item in settings.SETTINGS if item.field == field)
    await instance.cloud_settings.write(descriptor, value)
    args = ("TEST", 0, 4.2, None) if descriptor.mode_parameter else ("TEST",)
    getattr(instance.cloud, method).assert_called_once_with(*args, **payload)
    assert instance.data == {"TEST": {"power": 0.0, "lastUpdate": "original"}}
    # API acknowledgement must not overwrite the reported setting.
    assert (
        instance.cloud_settings.values[field]
        == instance.cloud.get_data_gen2.return_value[field]
    )


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("local", True),
        ("transitioning", True),
        ("cloud_restored_at", 1),
        ("_closed", True),
    ],
)
@pytest.mark.asyncio
async def test_no_cloud_write_on_tcp_or_during_handover(attribute, value):
    instance = owner()
    setattr(instance, attribute, value)
    with pytest.raises(settings.ModeVerificationError):
        await instance.cloud_settings.write(settings.SETTINGS[0], 12)
    instance.cloud.set_config_gen2.assert_not_called()
    instance.cloud.get_data_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_missing_companion_value_prevents_destructive_mode_write():
    instance = owner()
    instance.cloud.get_data_gen2.return_value["charge_target_soc"] = None
    descriptor = next(item for item in settings.SETTINGS if item.field == "max_energy")
    with pytest.raises(settings.ModeVerificationError, match="preserve"):
        await instance.cloud_settings.write(descriptor, 5)
    instance.cloud.set_charge_mode_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_cloud_setting_entities_keep_identity_and_recover(monkeypatch):
    monkeypatch.setattr(
        settings.NativeEntity, "available", property(lambda self: True), raising=False
    )
    instance = owner()
    await instance.cloud_settings.refresh()
    entities = [
        entity
        for platform in ("number", "select", "switch")
        for entity in settings.setup_cloud_settings(platform, instance)
    ]
    ids = {entity._attr_unique_id for entity in entities}
    assert {"TEST-" + item.suffix for item in settings.SETTINGS[:7]} <= ids
    dynamic = next(
        entity for entity in entities if entity.setting.field == "dynamicLoad"
    )
    assert dynamic.available and dynamic.is_on is False
    instance.cloud_settings.values["dynamicLoad"] = None
    assert dynamic.is_on is None
    instance.local = True
    instance.cloud_settings.request_refresh()
    assert not dynamic.available and dynamic.is_on is None
    instance.local = False
    await instance.cloud_settings.refresh()
    assert dynamic.available and dynamic.is_on is False


@pytest.mark.asyncio
async def test_discard_configuration_response_crossing_transport_epoch():
    instance = owner()

    def read(serial):
        instance.routing_epoch += 1
        return {"sn": serial, "dynamicLoad": True}

    instance.cloud.get_data_gen2.side_effect = read
    await instance.cloud_settings.refresh()
    assert not instance.cloud_settings.valid


@pytest.mark.asyncio
async def test_failed_write_is_visible_and_read_back_without_replay():
    instance = owner()
    instance.cloud.set_config_gen2.return_value = False
    with pytest.raises(settings.ModeVerificationError, match="acknowledge"):
        await instance.cloud_settings.write(settings.SETTINGS[0], 12)
    assert instance.cloud.set_config_gen2.call_count == 1
    assert instance.cloud.get_data_gen2.call_count == 2


@pytest.mark.parametrize("raw", [None, True, "nan", "inf", "invalid"])
def test_no_fabricated_numeric_value(raw):
    assert settings.number(raw) is None


@pytest.mark.asyncio
async def test_pv_completion_write_preserves_energy_and_soc():
    """Keep all reported PV+battery parameters when changing completion time."""
    instance = owner()
    instance.cloud.get_data_gen2.return_value["_reported_charge_mode"] = 2
    descriptor = next(item for item in settings.SETTINGS if item.field == "finish_time")
    await instance.cloud_settings.write(descriptor, "3")
    instance.cloud.set_charge_mode_gen2.assert_called_once_with(
        "TEST",
        2,
        None,
        None,
        max_energy=20,
        min_energy=3,
        soc_target=10,
        finish_time="3",
    )


def test_mode_change_invalidates_cached_configuration():
    """Do not display previous-mode settings until a new configuration read."""
    instance = owner()
    manager = instance.cloud_settings
    manager.valid = True
    manager.next_refresh = 999999999999
    manager.request_refresh = Mock()
    manager.observe_mode(2)
    assert not manager.valid and manager.next_refresh == 0
    manager.request_refresh.assert_called_once()
    manager.observe_mode(2)
    manager.request_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_entity_update_reads_configuration_without_writing():
    """Refresh absent configuration through the shared client on user request."""
    instance = owner()
    entity = settings.setup_cloud_settings("number", instance)[0]
    await entity.async_update()
    instance.cloud.get_data_gen2.assert_called_once_with("TEST")
    assert instance.cloud_settings.valid
    instance.cloud.set_config_gen2.assert_not_called()
    instance.cloud.set_charge_mode_gen2.assert_not_called()
    assert instance.data == {"TEST": {"power": 0.0, "lastUpdate": "original"}}


@pytest.mark.parametrize("capabilities,expected", [
    ([], True),
    (["Phase_Switch"], False),
    (["Dynamic_Load_Control"], True),
])
def test_load_controls_respect_explicit_capabilities(capabilities, expected):
    """Retain legacy registrations without contradicting known capabilities."""
    instance = owner()
    instance.entry.data["more_device_controls"] = capabilities
    fields = {entity.setting.field for platform in ("number", "switch")
              for entity in settings.setup_cloud_settings(platform, instance)}
    assert ("dynamicLoad" in fields) is expected
    assert ("rated_max_charge_power" in fields) is expected
    assert "currentLimit" in fields


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, None, {"dashboardFunctions": "invalid"}])
async def test_failed_capability_discovery_keeps_saved_data(response):
    instance = owner()
    instance.cloud.fetch_device_info = Mock(return_value=response)
    instance.hass.config_entries = SimpleNamespace(async_update_entry=Mock())
    await instance.cloud_settings.discover_capabilities()
    instance.hass.config_entries.async_update_entry.assert_not_called()
    instance.cloud.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_capability_discovery_fills_missing_fields_without_overwriting():
    instance = owner()
    instance.entry.data = {"more_device_controls": ["Phase_Switch"], "rated_power": 11}
    instance.cloud.fetch_device_info = Mock(return_value={
        "productModel": "MODEL", "dashboardFunctions": ["plugAndCharge"],
        "moreDeviceControls": ["Dynamic_Load_Control"], "ratedPower": 22,
        "pileGeneration": 2,
    })
    instance.hass.config_entries = SimpleNamespace(async_update_entry=Mock())
    result = await instance.cloud_settings.discover_capabilities()
    assert result["productModel"] == "MODEL"
    instance.cloud.fetch_device_info.assert_called_once_with("TEST")
    instance.hass.config_entries.async_update_entry.assert_called_once_with(
        instance.entry, data={"more_device_controls": ["Phase_Switch"],
                              "rated_power": 11, "pile_generation": "2",
                              "dashboard_functions": ["plugAndCharge"]})
    instance.cloud.set_config_gen2.assert_not_called()


@pytest.mark.parametrize("reported,valid,local,expected", [
    (True, True, False, True), (False, True, False, True),
    (None, True, False, False), (170, True, False, False),
    (True, False, False, False), (True, True, True, False),
])
def test_minimum_power_uses_valid_report_without_capability_advertisement(
    reported, valid, local, expected,
):
    instance = owner()
    instance.entry.data["more_device_controls"] = ["Dynamic_Load_Control"]
    instance.local = local
    instance.cloud_settings.valid = valid
    instance.cloud_settings.values["ensure_minimum_charging_power"] = reported
    fields = {e.setting.field for e in settings.setup_cloud_settings("switch", instance)}
    assert ("ensure_minimum_charging_power" in fields) is expected
    assert "phaseSwitch" not in fields and "plug_and_charge" not in fields


def test_late_report_adds_minimum_switch_once_and_registers_cleanup():
    instance = owner()
    instance.entry.data["more_device_controls"] = ["Dynamic_Load_Control"]
    instance.entry.async_on_unload = Mock()
    instance.async_add_listener = Mock(return_value=Mock())
    add = Mock()
    initial = settings.setup_cloud_settings("switch", instance)
    settings.watch_reported_cloud_settings("switch", instance, add, initial)
    callback = instance.async_add_listener.call_args.args[0]
    callback()
    add.assert_not_called()
    instance.cloud_settings.valid = True
    instance.cloud_settings.values["ensure_minimum_charging_power"] = False
    callback()
    callback()
    add.assert_called_once()
    assert add.call_args.args[0][0].setting.field == "ensure_minimum_charging_power"
    instance.entry.async_on_unload.assert_called_once_with(
        instance.async_add_listener.return_value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize("value", [0, 170])
async def test_hca_minimum_power_uses_boolean_mode_write(mode, value):
    instance = owner()
    instance.entry.data["pile_generation"] = "1"
    instance.data["TEST"]["status"] = "waiting"
    instance.cloud.get_data_gen2.return_value.update(
        _reported_charge_mode=mode, ensure_minimum_charging_power=True,
    )
    descriptor = next(item for item in settings.SETTINGS
                      if item.field == "ensure_minimum_charging_power")
    await instance.cloud_settings.write(descriptor, value)
    instance.cloud.set_charge_mode_gen2.assert_called_once_with(
        "TEST", mode, ensure_minimum_charging_power=bool(value))
    instance.cloud.set_config_gen2.assert_not_called()
    assert instance.charge_mode_policy.desired_power == 4.2
    # An ACK cannot replace device readback with the requested value.
    assert instance.cloud_settings.values[descriptor.field] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,allowed", [(0, False), (None, False), (1, True), (2, True)])
async def test_hca_minimum_power_availability_and_write_guard(monkeypatch, mode, allowed):
    monkeypatch.setattr(settings.NativeEntity, "available", property(lambda self: True),
                        raising=False)
    instance = owner()
    instance.entry.data["pile_generation"] = "1"
    instance.data["TEST"]["status"] = "waiting"
    instance.cloud.get_data_gen2.return_value.update(
        _reported_charge_mode=mode, ensure_minimum_charging_power=False,
    )
    await instance.cloud_settings.refresh()
    entity = next(e for e in settings.setup_cloud_settings("switch", instance)
                  if e.setting.field == "ensure_minimum_charging_power")
    assert entity.available
    assert entity.is_on is False
    if not allowed:
        with pytest.raises((settings.ModeVerificationError, ValueError)):
            await instance.cloud_settings.write(entity.setting, 170)
        instance.cloud.set_charge_mode_gen2.assert_not_called()
        instance.cloud.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [None, "2", "3"])
async def test_other_generations_keep_existing_minimum_power_writer(generation):
    instance = owner()
    instance.entry.data["pile_generation"] = generation
    instance.cloud.get_data_gen2.return_value["ensure_minimum_charging_power"] = False
    descriptor = next(item for item in settings.SETTINGS
                      if item.field == "ensure_minimum_charging_power")
    await instance.cloud_settings.write(descriptor, 170)
    instance.cloud.set_config_gen2.assert_called_once_with(
        "TEST", ensureMinimumChargingPower=170)
    instance.cloud.set_charge_mode_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [None, "transition", "generation", "active", "active_fast", "unknown", "stale", "epoch"])
async def test_minimum_power_routes_native_without_cloud_cache(monkeypatch, blocked):
    from unittest.mock import AsyncMock
    instance = owner()
    instance.entry.data["pile_generation"] = "2" if blocked == "generation" else "1"
    instance.local = True
    instance.transitioning = blocked == "transition"
    instance.transport = SimpleNamespace(
        available=blocked != "stale",
        latest=SimpleNamespace(minimum_power=None if blocked == "unknown" else False,
                               mode=0 if blocked == "active_fast" else 1,
                               state=2 if blocked in ("active", "active_fast") else 0,
                               stopped=blocked not in ("active", "active_fast")),
        session_guard=SimpleNamespace(phase="idle"),
        async_command=AsyncMock(),
    )
    instance.async_refresh = AsyncMock()
    instance.cloud_settings.values["ensure_minimum_charging_power"] = True
    entity = next(e for e in settings.setup_cloud_settings("switch", instance)
                  if e.setting.field == "ensure_minimum_charging_power")
    initially_available = blocked in (None, "epoch", "active", "active_fast")
    assert entity.available is initially_available
    assert entity.is_on is (False if initially_available else None)
    if blocked == "epoch":
        async def change_route(operation):
            instance.routing_epoch += 1
            await operation()
        instance.charge_mode_policy.async_setting_write = change_route
    assert entity._attr_unique_id == "TEST-switch-ensure-minimum-power"
    if blocked is None:
        await entity.write(170)
        instance.transport.async_command.assert_awaited_once_with(
            "minimum_power", minimum_power=True, timeout=15)
        assert entity.is_on is False  # No optimistic state assignment.
    else:
        # Bypass HA's translated exception wrapper; assert the routing guard.
        async def invoke(operation, **kwargs):
            await operation()
        monkeypatch.setattr(entity, "invoke", invoke)
        with pytest.raises(settings.ModeVerificationError):
            await entity.write(170)
        instance.transport.async_command.assert_not_awaited()
    instance.cloud.set_config_gen2.assert_not_called()
    instance.cloud.set_charge_mode_gen2.assert_not_called()
    instance.cloud.get_data_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["charging", "unknown", None])
async def test_combined_cloud_minimum_requires_confirmed_idle(status):
    instance = owner()
    instance.entry.data["pile_generation"] = "1"
    instance.data["TEST"]["status"] = status
    instance.cloud.get_data_gen2.return_value.update(
        _reported_charge_mode=1, ensure_minimum_charging_power=True,
    )
    descriptor = next(item for item in settings.SETTINGS
                      if item.field == "ensure_minimum_charging_power")
    with pytest.raises(ValueError, match="requires an idle wallbox"):
        await instance.cloud_settings.write(descriptor, 0)
    instance.cloud.set_charge_mode_gen2.assert_not_called()
    instance.cloud.set_config_gen2.assert_not_called()
    assert instance.cloud_settings.values[descriptor.field] is True


@pytest.mark.parametrize("generation,available,flag,expected", [
    ("1", True, False, 1), ("1", True, True, 1),
    ("1", True, None, 0), ("1", False, True, 0),
    (None, True, True, 0), ("2", True, True, 0),
])
def test_accountless_registration_requires_known_native_support(generation, available, flag, expected):
    instance = owner()
    instance.cloud = None
    instance.local = True
    instance.entry.data["pile_generation"] = generation
    instance.transport = SimpleNamespace(
        available=available, latest=SimpleNamespace(minimum_power=flag))
    entities = settings.setup_cloud_settings("switch", instance)
    assert len(entities) == expected
    assert settings.setup_cloud_settings("number", instance) == []
    assert settings.setup_cloud_settings("select", instance) == []
    if entities:
        entity = entities[0]
        assert entity._attr_unique_id == "TEST-switch-ensure-minimum-power"
        assert entity.is_on is flag
        assert entity.extra_state_attributes["supported_transport"] == "tcp"


def test_accountless_late_native_report_registers_once():
    instance = owner()
    instance.cloud = None
    instance.local = True
    instance.entry.data["pile_generation"] = "1"
    instance.entry.async_on_unload = Mock()
    instance.async_add_listener = Mock(return_value=Mock())
    instance.transport = SimpleNamespace(
        available=False, latest=SimpleNamespace(minimum_power=None))
    add = Mock()
    settings.watch_reported_cloud_settings("switch", instance, add, [])
    callback = instance.async_add_listener.call_args.args[0]
    callback()
    add.assert_not_called()
    instance.transport.available = True
    instance.transport.latest.minimum_power = False
    callback()
    callback()
    add.assert_called_once()
    assert add.call_args.args[0][0].is_on is False
    instance.entry.async_on_unload.assert_called_once_with(
        instance.async_add_listener.return_value)


@pytest.mark.asyncio
async def test_accountless_minimum_write_uses_native_command_only():
    from unittest.mock import AsyncMock

    instance = owner()
    instance.cloud = None
    instance.local = True
    instance.entry.data["pile_generation"] = "1"
    instance.transport = SimpleNamespace(
        available=True,
        latest=SimpleNamespace(minimum_power=False, mode=0, state=0, stopped=True),
        session_guard=SimpleNamespace(phase="idle"), async_command=AsyncMock(),
    )
    instance.async_refresh = AsyncMock()
    entity = settings.setup_cloud_settings("switch", instance)[0]
    await entity.write(170)
    instance.transport.async_command.assert_awaited_once_with(
        "minimum_power", minimum_power=True, timeout=15)
    assert entity.is_on is False  # Wait for a real device report.


@pytest.mark.asyncio
async def test_saved_capabilities_still_discover_missing_current_range():
    """A known model or local startup must not leave range metadata unresolved."""
    instance = owner()
    instance.cloud.get_data_gen2.return_value["controlItemRanges"] = False
    ranges = {"charge_pile_dynamic_load_import_current_limit": {"min": 6, "max": 80}}
    instance.cloud.fetch_device_info.return_value = {"controlItemRanges": ranges}
    await instance.cloud_settings.refresh()
    assert instance.cloud_settings.values["controlItemRanges"] == ranges
    instance.cloud.fetch_device_info.assert_called_once_with("TEST")
    instance.cloud.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_range_discovery_crossing_handover_is_discarded():
    instance = owner()
    instance.cloud.get_data_gen2.return_value["controlItemRanges"] = False
    def discover(serial):
        instance.routing_epoch += 1
        return {"productModel": "MODEL"}
    instance.cloud.fetch_device_info.side_effect = discover
    await instance.cloud_settings.refresh()
    assert not instance.cloud_settings.valid


@pytest.mark.asyncio
async def test_current_metadata_read_cannot_send_after_transport_change():
    instance = owner()
    def discover(serial):
        instance.local = True
        instance.routing_epoch += 1
        return {"productModel": "MODEL"}
    instance.cloud.fetch_device_info.side_effect = discover
    descriptor = next(item for item in settings.SETTINGS if item.field == "currentLimit")
    with pytest.raises(settings.ModeVerificationError):
        await instance.cloud_settings.write(descriptor, 63)
    instance.cloud.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_reported_limit_uses_configuration_not_legacy_telemetry_or_intent():
    entities = importlib.import_module(PACKAGE + ".native_entities")
    instance = owner()
    instance.serial = "5011KHCA00000000"
    instance.data = {instance.serial: {"set_charge_power": 4.2, "status": "charging"}}
    instance.cloud.get_data_gen2.return_value["sn"] = instance.serial
    instance.charge_mode_policy.desired_power = 6
    sensor = entities.ValueSensor(instance, "limit", "limit", "set_charge_power")
    control = entities.ChargePowerNumber(instance, "power", "power")
    status = entities.ValueSensor(instance, "status", "status", "status")
    assert sensor.native_value is None
    await instance.cloud_settings.refresh()
    assert sensor.native_value == 11
    assert control.native_value == 6
    assert control.extra_state_attributes["reported_power_limit"] == 11
    assert status.extra_state_attributes["set_charge_power"] == 11
    instance.local = True
    assert sensor.native_value == 4.2
    assert control.extra_state_attributes["reported_power_limit"] == 4.2
    instance.local = False
    instance.cloud.get_data_gen2.return_value = None
    await instance.cloud_settings.refresh()
    assert sensor.native_value is None
    assert control.extra_state_attributes["reported_power_limit"] is None
    assert "set_charge_power" not in status.extra_state_attributes


@pytest.mark.parametrize("raw", [None, True, "", "bad", -1, float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_reported_limit_rejects_missing_or_invalid_cloud_values(raw):
    entities = importlib.import_module(PACKAGE + ".native_entities")
    instance = owner()
    instance.cloud.get_data_gen2.return_value["set_charge_power"] = raw
    await instance.cloud_settings.refresh()
    sensor = entities.ValueSensor(instance, "limit", "limit", "set_charge_power")
    assert sensor.native_value is None


@pytest.mark.parametrize("attribute,value", [
    ("transitioning", True), ("cloud_restored_at", 123),
    ("last_update_success", False), ("_closed", True),
])
@pytest.mark.asyncio
async def test_reported_limit_hides_unverified_configuration(attribute, value):
    entities = importlib.import_module(PACKAGE + ".native_entities")
    instance = owner()
    await instance.cloud_settings.refresh()
    setattr(instance, attribute, value)
    sensor = entities.ValueSensor(instance, "limit", "limit", "set_charge_power")
    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_configuration_read_started_before_write_invalidation_is_discarded():
    instance = owner()

    async def execute(function, *args):
        result = function(*args)
        instance.cloud_settings.invalidate()
        return result

    instance.hass.async_add_executor_job = execute
    await instance.cloud_settings.refresh()
    assert not instance.cloud_settings.valid
    assert instance.cloud_settings.next_refresh == 0


@pytest.mark.parametrize("key", ["power", "mode"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.asyncio
async def test_control_write_invalidates_configuration_before_telemetry_refresh(key, failed):
    entities = importlib.import_module(PACKAGE + ".native_entities")
    instance = owner()
    await instance.cloud_settings.refresh()
    invalidated = []

    async def refresh():
        invalidated.append(not instance.cloud_settings.valid)
        await instance.cloud_settings.refresh()

    instance.async_refresh = refresh
    entity = entities.ControlEntity(instance, "control", "control")

    async def write():
        instance.cloud.get_data_gen2.return_value["set_charge_power"] = 5
        if failed:
            raise RuntimeError("Uncertain write")

    if failed:
        with pytest.raises(RuntimeError, match="Uncertain write"):
            await entity.submit(key, 6, write)
    else:
        await entity.submit(key, 6, write)
    assert invalidated == [True]
    assert entity.reported_power_limit == 5
