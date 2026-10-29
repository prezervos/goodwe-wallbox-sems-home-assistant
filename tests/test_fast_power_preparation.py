"""Exercise staged Fast limits through cloud/Modbus entities and real policy."""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from tests.test_number import _number_mod, _make_entity, SAMPLE_SN

policy_module = importlib.import_module(_number_mod.__package__ + ".charge_mode_policy")
adapter_module = importlib.import_module(_number_mod.__package__ + ".charge_mode_adapter")


@pytest.fixture(params=["cloud", "legacy_cloud", "modbus"])
def rig(request):
    cloud = _make_entity(chargeMode=1, set_charge_power=0)
    coordinator = cloud.coordinator
    state = coordinator.data[SAMPLE_SN]
    state.update(modbus_power_spec=1, modbus_max_charging_power=0,
                 status="standby", lastUpdate="2026-09-24T10:00:00Z",
                 modbus_status_raw=1, modbus_car_connected=1, modbus_power=0, power=0)
    client = MagicMock()
    client.supports_timestamped_observation = request.param == "cloud"
    client.fetch_last_charge.return_value = {}
    calls = []
    counter = count(1)

    def read(*args):
        stamp = datetime(2026, 9, 24, tzinfo=timezone.utc) + timedelta(seconds=next(counter))
        return dict(state, _reported_charge_mode=state["chargeMode"], lastUpdate=stamp.isoformat())

    def mode(value):
        calls.append(("mode", value))
        state["chargeMode"] = value
        state["set_charge_power"] = 11  # Firmware resets limit on mode change.
        return True

    def power(value):
        calls.append(("power", value))
        state["set_charge_power"] = value
        return True

    def cloud_mode(sn, value, limit):
        mode(value)
        power(limit)
        return True

    client.fetch_status_observation.side_effect = lambda sn: dict(read(), set_charge_power=4.2)
    client.read_all.side_effect = read
    client.get_data_gen2.side_effect = read
    client.write_charge_mode.side_effect = mode
    client.write_max_charge_power.side_effect = power
    client.set_charge_mode_gen2.side_effect = cloud_mode
    client.write_start_stop.side_effect = lambda enabled: calls.append(("start", enabled)) or True
    client.change_status_gen2.side_effect = lambda sn, action: calls.append(("start", action)) or True
    modbus = request.param == "modbus"
    entity = _number_mod.ModbusMaxChargePowerNumber(coordinator, SAMPLE_SN, client) if modbus else cloud
    entity.hass = cloud.hass
    entity.async_write_ha_state = MagicMock()
    entity.api = client
    adapter = adapter_module.ModeTransportAdapter(entity.hass, SAMPLE_SN, client, modbus=modbus)
    store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    policy = policy_module.ChargeModePolicy(adapter, store, enabled=True, timeout=.04, interval=0)
    coordinator.charge_mode_policy = policy
    return entity, state, client, policy, calls


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", [1, 2])
async def test_pv_zero_limit_can_stage_reload_select_fast_and_start(rig, enabled, mode):
    entity, state, client, policy, calls = rig
    state["chargeMode"] = mode
    policy.enabled = enabled
    assert entity.available
    await entity.async_set_native_value(4.2)
    assert calls == []
    client.fetch_status_observation.assert_not_called()
    client.read_all.assert_not_called()
    client.get_data_gen2.assert_not_called()
    assert state["chargeMode"] == mode
    assert state["set_charge_power"] == 0
    assert entity.native_value == 4.2
    assert entity.extra_state_attributes["reported_power_limit"] == 0
    saved = policy.store.async_save.call_args.args[0]
    policy.store.async_load.return_value = saved
    restored = policy_module.ChargeModePolicy(policy.adapter, policy.store,
                                              enabled=enabled, timeout=.04, interval=0)
    await restored.async_load()
    entity.coordinator.charge_mode_policy = restored
    assert restored.desired_power == 4.2
    assert await policy_module.async_apply_policy(entity.coordinator, "select_mode", 0)
    assert calls == [("mode", 0), ("power", 4.2)]
    assert state["set_charge_power"] == 4.2
    await restored.async_start()
    assert calls[-1][0] == "start"


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 12, 1])
async def test_invalid_staged_limit_does_not_write_or_persist(rig, value):
    entity, state, client, policy, calls = rig
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(value)
    assert policy.desired_power is None
    policy.store.async_save.assert_not_awaited()
    assert calls == []
    assert state["set_charge_power"] == 0


async def test_unconfirmed_fast_power_blocks_start_and_retains_raw_report(rig):
    entity, state, client, policy, calls = rig
    await entity.async_set_native_value(4.2)
    if isinstance(entity, _number_mod.ModbusMaxChargePowerNumber):
        client.write_max_charge_power.side_effect = lambda value: True
    else:
        client.set_charge_mode_gen2.side_effect = lambda *args: True
    with pytest.raises(HomeAssistantError):
        await policy_module.async_apply_policy(entity.coordinator, "select_mode", 0)
    with pytest.raises(policy_module.ModeVerificationError, match="confirmation"):
        await policy.async_start()
    assert not any(call[0] == "start" for call in calls)
    assert policy.desired_power == 4.2
    assert state["set_charge_power"] != 4.2


async def test_latest_staged_request_wins_without_device_io(rig):
    entity, state, client, policy, calls = rig
    await entity.async_set_native_value(4.2)
    await entity.async_set_native_value(5.0)
    assert calls == []
    await policy_module.async_apply_policy(entity.coordinator, "select_mode", 0)
    assert calls == [("mode", 0), ("power", 5.0)]


async def test_modern_explicit_idle_can_prepare_without_inventing_zero_power():
    entity = _make_entity(chargeMode=0, set_charge_power=4.2)
    client = MagicMock()
    client.supports_timestamped_observation = True
    state = {"sn": SAMPLE_SN, "observation_source": "semsplus_equipment",
             "_cloud_session_idle": True, "last_charge_work_status": 8,
             "status": "standby", "power": None, "chargeMode": 0,
             "lastUpdate": "2026-10-29T11:57:26Z"}
    client.fetch_status_observation.return_value = state
    client.get_data_gen2.return_value = {"sn": SAMPLE_SN, "_reported_charge_mode": 0,
                                        "set_charge_power": 4.2}
    adapter = adapter_module.ModeTransportAdapter(entity.hass, SAMPLE_SN, client)
    observation = await adapter.read()
    assert observation.active is False
    assert observation.report_marker.startswith("equipment:")
    assert state["power"] is None
    client.change_status_gen2.assert_not_called()


@pytest.mark.parametrize("advances,source", [(True, "semsplus_equipment"), (False, "semsplus_equipment"), (True, None)])
async def test_modern_idle_stop_confirmation_requires_advancing_same_source(advances, source):
    entity = _make_entity(chargeMode=0, set_charge_power=4.2)
    client = MagicMock()
    first = {"sn": SAMPLE_SN, "lastUpdate": "2026-10-29T11:57:26Z",
             "status": "standby", "power": None, "observation_source": "semsplus_equipment",
             "_cloud_session_idle": True, "last_charge_work_status": 8}
    second = dict(first, lastUpdate="2026-10-29T11:57:56Z" if advances else first["lastUpdate"],
                  observation_source=source)
    reports = iter([first])
    client.fetch_status_observation.side_effect = lambda sn: next(reports, second)
    adapter = adapter_module.ModeTransportAdapter(entity.hass, SAMPLE_SN, client)
    assert await adapter._confirm_stopped(timeout=0.2, interval=0.001) is (advances and source == "semsplus_equipment")
    client.change_status_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preparation", "readback", "identity", "malformed_mode"])
async def test_modern_configuration_failure_proves_no_write_only_before_dispatch(stage):
    from types import SimpleNamespace
    commands = importlib.import_module(adapter_module.__package__ + ".cloud_command")
    serial = "TEST"
    def observation(sn):
        return next(replies)
    valid = {"sn": serial, "_reported_charge_mode": 0, "set_charge_power": 4.2,
             "status": "standby", "power": 0, "lastUpdate": "2026-10-29T10:00:00Z"}
    unavailable = {"sn": serial, "_cloud_detail_available": False}
    if stage == "identity":
        unavailable = {"sn": "OTHER", "_cloud_detail_available": False}
    elif stage == "malformed_mode":
        unavailable = dict(valid, _reported_charge_mode="wrong")
    replies = iter([valid, unavailable] if stage == "readback" else [unavailable])
    client = SimpleNamespace(supports_timestamped_observation=True,
        fetch_status_observation=observation,
        get_data_gen2=lambda sn: dict(valid),
        set_charge_mode_gen2=lambda *args: True)
    hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
    adapter = adapter_module.ModeTransportAdapter(hass, serial, client)
    adapter.write_mode = AsyncMock(return_value=True)
    adapter.start = AsyncMock(return_value=True)
    store = SimpleNamespace(async_save=AsyncMock())
    policy = policy_module.ChargeModePolicy(adapter, store, initial_mode=1, timeout=1)
    expected = commands.CloudPreparationUnavailable if stage == "preparation" else (
        commands.CloudConfigurationUnavailable if stage == "readback" else policy_module.ModeVerificationError
    )
    with pytest.raises(expected):
        await policy.async_start()
    assert adapter.write_mode.await_count == (1 if stage == "readback" else 0)
    adapter.start.assert_not_awaited()
