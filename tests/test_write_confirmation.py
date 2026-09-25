"""Readback scenarios use a deterministic clock and never contact a wallbox."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

_spec = importlib.util.spec_from_file_location(
    "wallbox_write_confirmation_test",
    Path(__file__).parents[1] / "custom_components/sems_wallbox/write_confirmation.py",
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)


class Clock:
    """Record timers while allowing device replies to advance elapsed time."""

    def __init__(self):
        self.now = 100.0
        self.timer = None

    def later(self, hass, delay, callback):
        record = [self.now + delay, callback, False]
        self.timer = record
        return lambda: record.__setitem__(2, True)


@pytest.fixture
def setup(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, "async_call_later", clock.later)
    owner = SimpleNamespace(
        hass=SimpleNamespace(),
        routing_epoch=0,
        local=False,
        last_update_success=True,
        update_interval=60,
    )
    monitor = module.WriteConfirmation(owner)
    owner.write_confirmation = monitor
    return clock, owner, monitor


def arm(monitor, field="charging", value=True, source="telemetry"):
    monitor.accepted(field, value, monitor.begin(field), source=source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modbus,offsets", [(False, [5, 10, 20, 35, 60]), (True, list(range(5, 61, 5)))]
)
async def test_old_reports_follow_bounded_schedule_without_writes(
    setup, modbus, offsets
):
    clock, owner, monitor = setup
    monitor.modbus = modbus
    reads = []

    async def refresh():
        reads.append(clock.now - 100)
        monitor.observed({"status": "standby", "modbus_status_raw": 2})

    owner.async_request_refresh = refresh
    arm(monitor)
    for offset in offsets:
        assert clock.timer[0] == 100 + offset
        clock.now = clock.timer[0]
        monitor._busy = True
        await monitor._refresh()
    assert reads == offsets
    assert monitor.diagnostics() == {"charging": "unconfirmed"}
    assert not monitor.pending
    assert owner.update_interval == 60


def test_fresh_report_confirms_without_measured_power_matching_limit(setup):
    clock, owner, monitor = setup
    arm(monitor, "set_charge_power", 5)
    clock.now += 5
    monitor.observed({"set_charge_power": 5, "power": 4.6})
    assert monitor.results["set_charge_power"] == "confirmed"
    assert not monitor.pending
    assert clock.timer[2]


def test_old_inflight_read_cannot_confirm_new_write(setup):
    clock, owner, monitor = setup
    arm(monitor, "set_charge_power", 5)
    monitor.observed({"set_charge_power": 5}, read_started=99)
    assert "set_charge_power" in monitor.pending
    clock.now += 5
    monitor.observed({"set_charge_power": 5}, read_started=104)
    assert not monitor.pending


def test_latest_power_and_stop_supersede_older_completion(setup):
    clock, owner, monitor = setup
    old = monitor.begin("charging")
    arm(monitor, "charging", False)
    monitor.accepted("charging", True, old)
    assert monitor.pending["charging"].value is False
    old = monitor.begin("set_charge_power")
    arm(monitor, "set_charge_power", 6)
    monitor.rejected("set_charge_power", old)
    monitor.observed({"set_charge_power": 5, "power": 0})
    assert monitor.pending["set_charge_power"].value == 6
    assert "charging" in monitor.pending
    monitor.observed({"set_charge_power": 6, "status": "standby"})
    assert not monitor.pending


@pytest.mark.parametrize("ending", ["failed", "close", "cancel"])
def test_failure_reload_and_transport_cancel_timers(setup, ending):
    clock, owner, monitor = setup
    arm(monitor)
    getattr(monitor, ending)()
    assert not monitor.pending
    assert clock.timer[2]


def test_transport_epoch_rejects_late_command(setup):
    clock, owner, monitor = setup
    ticket = monitor.begin("charging")
    owner.routing_epoch += 1
    monitor.accepted("charging", True, ticket)
    assert not monitor.pending


def test_native_tcp_adds_no_polling(setup):
    clock, owner, monitor = setup
    owner.local = True
    arm(monitor)
    assert not monitor.pending
    assert clock.timer is None


def test_settings_source_cannot_be_confirmed_by_v3_telemetry(setup):
    clock, owner, monitor = setup
    arm(monitor, "set_charge_power", 6, "settings")
    monitor.observed({"set_charge_power": 6})
    assert monitor.pending
    monitor.observed({"set_charge_power": 6}, source="settings")
    assert not monitor.pending


@pytest.mark.asyncio
async def test_slow_read_skips_missed_slots_instead_of_burst(setup):
    clock, owner, monitor = setup

    async def refresh():
        clock.now = 127
        monitor.observed({"status": "standby"})

    owner.async_request_refresh = refresh
    arm(monitor)
    clock.now = 105
    monitor._busy = True
    await monitor._refresh()
    assert clock.timer[0] == 135


def test_normal_polling_or_mqtt_read_reuses_confirmation(setup):
    clock, owner, monitor = setup
    arm(monitor)
    clock.now = 104
    monitor.observed({"status": "standby"})
    assert clock.timer[0] == 109
    clock.now = 107
    monitor.observed({"status": "charging"})
    assert not monitor.pending


@pytest.mark.asyncio
async def test_decorator_sends_once_and_failure_never_arms(setup):
    clock, owner, monitor = setup
    entity = SimpleNamespace(coordinator=owner)
    write = AsyncMock()

    @module.confirm_write("set_charge_power")
    async def service(entity, value):
        await write(value)

    await service(entity, 6)
    write.assert_awaited_once_with(6)
    assert monitor.pending["set_charge_power"].value == 6
    write.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await service(entity, 5)
    assert not monitor.pending
    assert monitor.results["set_charge_power"] == "write_failed"


@pytest.mark.asyncio
async def test_read_failure_yields_to_coordinator_recovery(setup):
    clock, owner, monitor = setup
    owner._station_id = "test"

    @module.confirmation_read
    async def read(owner):
        raise TimeoutError

    arm(monitor)
    with pytest.raises(TimeoutError):
        await read(owner)
    assert not monitor.pending
    assert monitor.results["charging"] == "read_failed"


@pytest.mark.asyncio
async def test_settings_read_finishes_before_deadline_is_evaluated(setup):
    clock, owner, monitor = setup

    async def settings_read():
        monitor.observed({"set_charge_power": 6}, source="settings")

    owner.cloud_settings = SimpleNamespace(
        _task=None, refresh=AsyncMock(side_effect=settings_read)
    )
    owner.async_request_refresh = AsyncMock()
    arm(monitor, "set_charge_power", 6, "settings")
    clock.now = 160
    monitor._busy = True
    await monitor._refresh()
    assert monitor.results["set_charge_power"] == "confirmed"
    owner.cloud_settings.refresh.assert_awaited_once()
    owner.async_request_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        {"power": 0},
        {"status": "charging", "power": 0},
        {"modbus_status_raw": 2, "power": 0},
    ],
)
def test_zero_power_does_not_confirm_stop(data):
    assert not module.matches("charging", False, data)


@pytest.mark.asyncio
async def test_unexpected_refresh_failure_does_not_repeat_or_leak_task_error(setup):
    clock, owner, monitor = setup
    owner.async_request_refresh = AsyncMock(side_effect=RuntimeError("read failed"))
    arm(monitor)
    clock.now += 5
    monitor._busy = True
    await monitor._refresh()
    assert monitor.results["charging"] == "read_failed"
    assert not monitor.pending and not monitor._busy


def test_multiple_settings_share_one_timer_and_have_independent_results(setup):
    clock, owner, monitor = setup
    arm(monitor, "set_charge_power", 5)
    first_timer = clock.timer
    arm(monitor, "chargeMode", 0)
    assert first_timer[2]
    assert clock.timer[0] == 105
    clock.now = 105
    monitor.observed({"set_charge_power": 5, "chargeMode": 1})
    assert monitor.results["set_charge_power"] == "confirmed"
    assert list(monitor.pending) == ["chargeMode"]
    assert clock.timer[0] == 110


def test_legacy_one_shot_timer_is_replaced_not_duplicated(setup):
    clock, owner, monitor = setup
    cancelled = []
    owner._pending_refresh_cancel = lambda: cancelled.append(True)
    arm(monitor)
    assert cancelled == [True]
    assert owner._pending_refresh_cancel is None


def test_session_history_cannot_override_current_idle_status():
    assert not module.matches(
        "charging", True, {"status": "standby", "last_charge_work_status": 6}
    )


@pytest.mark.parametrize("status", [5, 8])
def test_modbus_alarm_or_start_failure_ends_readback_without_a_stop(setup, status):
    clock, owner, monitor = setup
    monitor.modbus = True
    arm(monitor)
    monitor.observed({"modbus_status_raw": status})
    assert monitor.results["charging"] == "device_rejected"
    assert not monitor.pending


@pytest.mark.asyncio
async def test_real_cloud_setting_entity_arms_and_confirms_correct_source(setup):
    from tests.test_native_cloud_settings import owner as make_owner, settings

    clock, _, _ = setup
    owner = make_owner()
    monitor = module.WriteConfirmation(owner)
    owner.write_confirmation = monitor
    descriptor = next(item for item in settings.SETTINGS if item.field == "dynamicLoad")
    entity = settings.CloudSettingSwitch(owner, descriptor)
    await owner.cloud_settings.refresh()
    await entity.async_turn_on()
    owner.cloud.set_config_gen2.assert_called_once_with("TEST", dynamicLoad=1)
    assert monitor.pending["dynamicLoad"].value is True
    assert monitor.pending["dynamicLoad"].source == "settings"
    owner.cloud.get_data_gen2.return_value["dynamicLoad"] = True
    clock.now += 5
    await owner.cloud_settings.refresh()
    assert monitor.results["dynamicLoad"] == "confirmed"


@pytest.mark.asyncio
async def test_real_cloud_start_stop_hooks_keep_latest_choice(setup):
    from tests.test_switch import _make_switch, STANDBY_DATA

    clock, _, _ = setup
    entity = _make_switch(dict(STANDBY_DATA))
    entity.async_write_ha_state = Mock()
    owner = entity.coordinator
    owner.hass = entity.hass
    monitor = module.WriteConfirmation(owner)
    owner.write_confirmation = monitor

    async def execute(function, *args):
        return function(*args)

    entity.hass.async_add_executor_job = execute
    entity.api.change_status_gen2.return_value = True
    await entity.async_turn_on()
    await entity.async_turn_off()
    assert monitor.pending["charging"].value is False
    assert entity.api.change_status_gen2.call_count == 2
    monitor.observed({"status": "charging"})
    assert monitor.pending
    monitor.observed({"status": "EVDetail_Status_Title_Waiting"})
    assert monitor.results["charging"] == "confirmed"


def test_reported_mode_takes_precedence_over_fallback_mode():
    assert not module.matches(
        "chargeMode", 0, {"chargeMode": 0, "_reported_charge_mode": 1}
    )
    assert module.matches("chargeMode", 1, {"_reported_charge_mode": 1})


@pytest.mark.asyncio
async def test_cancelled_background_read_cannot_rearm_timer(setup):
    clock, owner, monitor = setup
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh():
        entered.set()
        await release.wait()

    owner.async_request_refresh = refresh
    arm(monitor)
    clock.now += 5
    task = asyncio.create_task(monitor._refresh())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not monitor.pending and monitor._cancel is None
    assert monitor.results["charging"] == "cancelled"


@pytest.mark.asyncio
async def test_inflight_configuration_read_is_reused_then_fresh_read_confirms(setup):
    clock, owner, monitor = setup
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_read():
        entered.set()
        await release.wait()
        monitor.observed({"set_charge_power": 6}, source="settings", read_started=99)

    async def fresh_read():
        monitor.observed({"set_charge_power": 6}, source="settings", read_started=110)

    task = asyncio.create_task(old_read())
    await entered.wait()
    owner.cloud_settings = SimpleNamespace(
        _task=task, refresh=AsyncMock(side_effect=fresh_read)
    )
    owner.async_request_refresh = AsyncMock()
    arm(monitor, "set_charge_power", 6, "settings")
    clock.now = 105
    release.set()
    await monitor._refresh()
    owner.cloud_settings.refresh.assert_not_awaited()
    assert "set_charge_power" in monitor.pending
    clock.now = 110
    await monitor._refresh()
    assert monitor.results["set_charge_power"] == "confirmed"
    owner.cloud_settings.refresh.assert_awaited_once()
    owner.async_request_refresh.assert_not_awaited()
