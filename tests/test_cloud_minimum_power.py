"""Cloud-only minimum-power parity using the existing switch test harness."""

from types import SimpleNamespace
from unittest.mock import MagicMock
import time
from tests.cloud_settings_harness import bind_settings_api

import pytest
from homeassistant.exceptions import HomeAssistantError
from tests.test_switch import SAMPLE_SN, _FakeCoordinator, _switch_mod


def make_entity(generation="1", mode=1, reported=False):
    data = {"power": 0.0, "status": "waiting", "sn": SAMPLE_SN, "_reported_charge_mode": mode,
            "ensure_minimum_charging_power": reported}
    owner = _FakeCoordinator({SAMPLE_SN: dict(data)})
    owner.schedule_delayed_refresh = MagicMock()
    api = MagicMock()
    api.get_data_gen2.return_value = {**data, "power": 4.2}
    api.fetch_status_observation.return_value = {**data, "lastUpdate": time.time()}
    bind_settings_api(api, _switch_mod.__package__)
    api.set_charge_mode_gen2.return_value = True
    api.set_config_gen2.return_value = True
    entity = _switch_mod.SemsMinimumPowerSwitch(owner, SAMPLE_SN, api, generation=generation)

    async def execute(function):
        return function()

    entity.hass = SimpleNamespace(async_add_executor_job=lambda fn, *args: execute(lambda: fn(*args)), config=SimpleNamespace(time_zone="UTC"))
    return entity, api


@pytest.mark.parametrize("generation,mode", [("1", 1), ("1", 2), ("2", 0), (None, 0)])
@pytest.mark.parametrize("target", [False, True])
async def test_generation_encoder_and_non_optimistic_readback(generation, mode, target):
    entity, api = make_entity(generation, mode, not target)
    await entity._async_set(target)
    api.get_data_gen2.assert_called_once_with(SAMPLE_SN)
    if generation == "1":
        api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, mode, ensure_minimum_charging_power=target)
        api.set_config_gen2.assert_not_called()
    else:
        api.set_config_gen2.assert_called_once_with(
            SAMPLE_SN, ensureMinimumChargingPower=170 if target else 0)
        api.set_charge_mode_gen2.assert_not_called()
    assert entity.is_on is (not target)
    assert entity._pending_state is None
    assert entity.unique_id == SAMPLE_SN + "-switch-ensure-minimum-power"
    entity.coordinator.schedule_delayed_refresh.assert_called_once_with(5.0)


@pytest.mark.parametrize("change", [{"sn": "OTHER"}, {"ensure_minimum_charging_power": None},
                                   {"_reported_charge_mode": 0}, {"_reported_charge_mode": None},
                                   {"_reported_charge_mode": True}])
async def test_missing_or_foreign_report_and_wrong_mode_prevent_write(change):
    entity, api = make_entity()
    api.get_data_gen2.return_value.update(change)
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    api.set_charge_mode_gen2.assert_not_called()
    api.set_config_gen2.assert_not_called()


@pytest.mark.parametrize("failure", [False, TimeoutError("uncertain")])
async def test_rejected_or_uncertain_write_is_not_replayed(failure):
    entity, api = make_entity()
    if isinstance(failure, Exception):
        api.set_charge_mode_gen2.side_effect = failure
    else:
        api.set_charge_mode_gen2.return_value = failure
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    assert api.set_charge_mode_gen2.call_count == 1
    assert entity.is_on is False
    api.set_config_gen2.assert_not_called()


@pytest.mark.parametrize("mode,available", [(0, True), (1, True), (2, True), (None, True)])
def test_first_generation_availability_and_unknown_value(mode, available):
    entity, _ = make_entity(mode=mode, reported=None)
    assert entity.available is available
    assert entity.is_on is None


async def test_late_boolean_registers_missing_capability_once():
    entity, api = make_entity(reported=None)
    owner = entity.coordinator
    owner.async_add_listener = MagicMock(return_value=MagicMock())
    entry = SimpleNamespace(entry_id="test", async_on_unload=MagicMock())
    hass = SimpleNamespace(data={"sems_wallbox": {"test": {
        "coordinator": owner, "api": api,
        "capabilities": {"pile_generation": "1", "more_device_controls": ["Dynamic_Load_Control"]},
    }}})
    added = []
    await _switch_mod.async_setup_entry(hass, entry, added.extend)
    assert not any(isinstance(e, _switch_mod.SemsMinimumPowerSwitch) for e in added)
    owner.data[SAMPLE_SN]["ensure_minimum_charging_power"] = False
    listener = owner.async_add_listener.call_args.args[0]
    listener()
    listener()
    matches = [e for e in added if isinstance(e, _switch_mod.SemsMinimumPowerSwitch)]
    assert len(matches) == 1 and matches[0].generation == "1"
    entry.async_on_unload.assert_called_once_with(owner.async_add_listener.return_value)


@pytest.mark.parametrize("target", [False, True])
async def test_fast_cloud_minimum_remains_visible_but_rejects_ignored_write(target):
    entity, api = make_entity(mode=0, reported=not target)
    api.get_data_gen2.return_value["set_charge_power"] = 4.2
    assert entity.available and entity.is_on is (not target)
    with pytest.raises(HomeAssistantError) as caught:
        await entity._async_set(target)
    assert caught.value.translation_key == "minimum_power_cloud_fast"
    api.set_charge_mode_gen2.assert_not_called()
    api.set_config_gen2.assert_not_called()
    assert entity.is_on is (not target)


@pytest.mark.parametrize("status,power", [("charging", 4.0),
    ("EVDetail_Status_Title_Charging", 0.0), ("unknown", 0.0),
    (None, 0.0), ("waiting", 4.0), ("waiting", None)])
@pytest.mark.parametrize("target", [False, True])
async def test_cloud_minimum_requires_confirmed_idle(status, power, target):
    entity, api = make_entity(reported=not target)
    api.fetch_status_observation.return_value.update(status=status, power=power)
    with pytest.raises(HomeAssistantError) as caught:
        await entity._async_set(target)
    assert caught.value.translation_key == ("minimum_power_idle_unverified"
        if power is None or status in (None, "unknown") else "minimum_power_stop_first")
    api.set_charge_mode_gen2.assert_not_called()
    api.set_config_gen2.assert_not_called()
    assert entity.is_on is (not target)


@pytest.mark.parametrize("change", [
    {"sn": "OTHER"}, {"lastUpdate": None}, {"lastUpdate": 0},
    {"lastUpdate": time.time() - 61}, {"lastUpdate": time.time() + 30},
    {"power": True}, {"power": float("nan")}, {"power": -1},
    {"chargeMode": 2, "_reported_charge_mode": 2},
])
async def test_idle_only_write_requires_fresh_consistent_observation(change):
    entity, api = make_entity()
    api.fetch_status_observation.return_value.update(change)
    with pytest.raises(HomeAssistantError) as caught:
        await entity._async_set(True)
    assert caught.value.translation_key == "minimum_power_idle_unverified"
    api.set_charge_mode_gen2.assert_not_called()
    api.set_config_gen2.assert_not_called()


async def test_idle_check_uses_measurement_not_configured_power_or_stale_start_flag():
    entity, api = make_entity()
    api.fetch_status_observation.return_value["startStatus"] = True
    await entity._async_set(True)
    assert api.get_data_gen2.return_value["power"] == 4.2
    api.set_charge_mode_gen2.assert_called_once()
