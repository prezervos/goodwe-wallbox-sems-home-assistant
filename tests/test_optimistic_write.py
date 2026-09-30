"""Failed and cancelled settings must not masquerade as device observations."""

import asyncio
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from tests.test_number import _make_entity as power_entity
from tests.test_number import _make_power_limit_entity
from tests.test_select import _make_entity as mode_entity
from tests.test_switch import _switch_mod


@pytest.mark.parametrize("kind", ["switch", "modbus_switch", "power", "limit", "mode"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_failed_setting_clears_pending_and_preserves_reported_state(kind, failure):
    if kind in ("switch", "modbus_switch"):
        coordinator = MagicMock()
        coordinator.data = {"SN": {"dynamicLoad": False, "modbus_dynamic_load": False}}
        coordinator.charge_mode_policy = None
        client = MagicMock()
        cls = (_switch_mod.SemsDynamicLoadSwitch if kind == "switch"
               else _switch_mod.ModbusDynamicLoadMgmtSwitch)
        entity = cls(coordinator, "SN", client)
        writer = client.set_config_gen2 if kind == "switch" else client.write_dynamic_load_mgmt
        operation = entity.async_turn_on
        observed = lambda: entity.is_on
        original = False
        entity.hass = MagicMock()
        entity.async_write_ha_state = MagicMock()
    elif kind == "power":
        entity = power_entity(set_charge_power=7.4)
        writer = entity.api.set_charge_mode_gen2
        operation = lambda: entity.async_set_native_value(5.0)
        observed = lambda: entity._attr_native_value
        original = 7.4
    elif kind == "limit":
        entity = _make_power_limit_entity(rated_max=11.0)
        writer = entity.api.set_config_gen2
        operation = lambda: entity.async_set_native_value(5.0)
        observed = lambda: entity.native_value
        original = 11.0
    else:
        entity = mode_entity(chargeMode=0, set_charge_power=7.4)
        writer = entity.api.set_charge_mode_gen2
        operation = lambda: entity.async_select_option("pv_priority")
        observed = lambda: entity._attr_current_option
        original = "fast"
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    async def executor(fn, *args):
        return fn(*args)
    entity.hass.async_add_executor_job = executor
    writer.side_effect = failure("write interrupted")
    before = dict(entity.coordinator.data[entity.sn])
    with pytest.raises(failure):
        await operation()
    assert observed() == original
    assert entity.coordinator.data[entity.sn] == before
    assert all(getattr(entity, key, None) is None
               for key in ("_pending_state", "_pending_value", "_pending_mode"))
    entity.coordinator.schedule_delayed_refresh.assert_called_with(3.0)
    assert writer.call_count == 1


async def test_failed_write_does_not_restore_over_newer_observation():
    entity = power_entity(set_charge_power=7.4)
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    async def executor(fn, *args):
        entity.coordinator.data = {entity.sn: {"chargeMode": 0, "set_charge_power": 6.0}}
        raise TimeoutError("write interrupted after fresh report")
    entity.hass.async_add_executor_job = executor
    with pytest.raises(TimeoutError):
        await entity.async_set_native_value(5.0)
    assert entity.coordinator.data[entity.sn]["set_charge_power"] == 6.0
    assert entity._attr_native_value == 6.0
    assert entity._pending_value is None


async def test_cancelled_inflight_write_drains_then_clears_optimistic_value():
    entity = _make_power_limit_entity(rated_max=11.0)
    entered, release = asyncio.Event(), asyncio.Event()
    async def executor(fn, *args):
        entered.set()
        await release.wait()
        return fn(*args)
    entity.hass.async_add_executor_job = executor
    write = asyncio.create_task(entity.async_set_native_value(5.0))
    await entered.wait()
    assert entity.native_value == 5.0
    write.cancel()
    await asyncio.sleep(0)
    assert not write.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert entity._pending_value is None and entity.native_value == 11.0
    entity.api.set_config_gen2.assert_called_once()
    entity.coordinator.schedule_delayed_refresh.assert_called_with(3.0)


@pytest.mark.parametrize("first_outcome,second_outcome", [(False, True), (True, False), (False, False), ("timeout", "timeout")])
@pytest.mark.parametrize("first_finishes_first", [False, True])
async def test_overlapping_writes_keep_authoritative_baseline_and_latest_success(
    first_outcome, second_outcome, first_finishes_first
):
    entity = power_entity(set_charge_power=7.4)
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = []
    async def executor(fn, *args):
        index = len(calls)
        calls.append(index)
        entered[index].set()
        await release[index].wait()
        outcome = [first_outcome, second_outcome][index]
        if outcome == "timeout":
            raise TimeoutError("unconfirmed write")
        return outcome
    entity.hass.async_add_executor_job = executor
    tasks = [asyncio.create_task(entity.async_set_native_value(5.0))]
    await entered[0].wait()
    tasks.append(asyncio.create_task(entity.async_set_native_value(6.0)))
    await entered[1].wait()
    order = [0, 1] if first_finishes_first else [1, 0]
    for index in order:
        release[index].set()
        result, = await asyncio.gather(tasks[index], return_exceptions=True)
        outcome = [first_outcome, second_outcome][index]
        if outcome is True:
            assert result is None
        else:
            expected_error = TimeoutError if outcome == "timeout" else HomeAssistantError
            assert isinstance(result, expected_error), result
    expected = 6.0 if second_outcome is True else 7.4
    assert entity.coordinator.data[entity.sn]["set_charge_power"] == expected
    assert entity.native_value == expected
    assert len(calls) == 2


async def test_false_return_cannot_overwrite_newer_report():
    entity = power_entity(set_charge_power=7.4)
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    async def executor(fn, *args):
        entity.coordinator.data = {entity.sn: {"chargeMode": 0, "set_charge_power": 6.0}}
        return False
    entity.hass.async_add_executor_job = executor
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(5.0)
    assert entity.coordinator.data[entity.sn]["set_charge_power"] == 6.0
    assert entity.native_value == 6.0


async def test_old_rate_limit_error_does_not_clear_new_pending_choice():
    import importlib
    rate = importlib.import_module(_switch_mod.__package__ + ".cloud_rate_limit")
    coordinator = MagicMock()
    coordinator.data = {"SN": {"dynamicLoad": None}}
    coordinator.charge_mode_policy = None
    entity = _switch_mod.SemsDynamicLoadSwitch(coordinator, "SN", MagicMock())
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = []
    async def executor(fn, *args):
        index = len(calls)
        calls.append(index)
        entered[index].set()
        await release[index].wait()
        if index == 0:
            raise rate.CloudRateLimitedError(60)
        return True
    entity.hass.async_add_executor_job = executor
    first = asyncio.create_task(entity.async_turn_on())
    await entered[0].wait()
    second = asyncio.create_task(entity.async_turn_off())
    await entered[1].wait()
    release[0].set()
    result = await asyncio.gather(first, return_exceptions=True)
    assert isinstance(result[0], Exception)
    assert not second.done() and entity._pending_state is False
    release[1].set()
    await second
    assert entity.is_on is False


async def test_mode_poll_remains_authoritative_after_failed_write():
    entity = mode_entity(chargeMode=0, set_charge_power=7.4)
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    async def executor(fn, *args):
        entity.coordinator.data = {entity.sn: {"chargeMode": 0, "set_charge_power": 7.4}}
        entity._handle_coordinator_update()
        assert entity.coordinator.data[entity.sn]["chargeMode"] == 0
        raise TimeoutError("unconfirmed mode write")
    entity.hass.async_add_executor_job = executor
    with pytest.raises(TimeoutError):
        await entity.async_select_option("pv_priority")
    assert entity._pending_mode is None
    assert entity._attr_current_option == "fast"
    assert entity.coordinator.data[entity.sn]["chargeMode"] == 0


@pytest.mark.parametrize("mode_finishes_first", [False, True])
async def test_failed_mode_and_power_writes_do_not_copy_optimistic_telemetry(mode_finishes_first):
    power = power_entity(set_charge_power=7.4)
    mode = mode_entity(chargeMode=0, set_charge_power=7.4)
    mode.coordinator = power.coordinator
    power.coordinator.schedule_delayed_refresh = MagicMock()
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    async def executor(index, fn, *args):
        entered[index].set()
        await release[index].wait()
        raise TimeoutError("unconfirmed write")
    async def power_executor(fn, *args):
        return await executor(0, fn, *args)
    async def mode_executor(fn, *args):
        return await executor(1, fn, *args)
    power.hass.async_add_executor_job = power_executor
    mode.hass.async_add_executor_job = mode_executor
    tasks = [asyncio.create_task(power.async_set_native_value(5.0))]
    await entered[0].wait()
    tasks.append(asyncio.create_task(mode.async_select_option("pv_priority")))
    await entered[1].wait()
    for index in ([1, 0] if mode_finishes_first else [0, 1]):
        release[index].set()
        result = await asyncio.gather(tasks[index], return_exceptions=True)
        assert isinstance(result[0], TimeoutError)
    assert power.coordinator.data[power.sn]["set_charge_power"] == 7.4
    assert power.coordinator.data[power.sn]["chargeMode"] == 0
    assert power.native_value == 7.4 and mode._attr_current_option == "fast"


async def test_mode_failure_does_not_own_report_received_during_preference_save():
    import importlib
    from types import SimpleNamespace
    from tests.test_select import _select_mod
    policy_module = importlib.import_module(_select_mod.__package__ + ".charge_mode_policy")
    entity = mode_entity(chargeMode=0, set_charge_power=7.4)
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    async def save(data):
        entity.coordinator.data = {entity.sn: {"chargeMode": 0, "set_charge_power": 6.0}}
        await asyncio.sleep(0)
    entity.coordinator.charge_mode_policy = policy_module.ChargeModePolicy(
        None, SimpleNamespace(async_save=save), enabled=False, initial_mode=0
    )
    async def executor(fn, *args):
        raise TimeoutError("unconfirmed mode write")
    entity.hass.async_add_executor_job = executor
    with pytest.raises(TimeoutError):
        await entity.async_select_option("pv_priority")
    assert entity.coordinator.data[entity.sn]["set_charge_power"] == 6.0
    assert entity._attr_current_option == "fast" and entity._pending_mode is None
