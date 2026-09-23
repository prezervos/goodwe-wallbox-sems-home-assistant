"""Adaptive backup polling requires fresh telemetry and has a bounded lease."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_cloud_push import module, subject

polling_module = __import__(
    module.__package__ + ".cloud_push_polling", fromlist=["time"]
)


def report(stamp):
    return {
        "lastUpdate": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "power": 4.2,
        "set_charge_power": 4.2,
        "chargeMode": 0,
        "status": "charging",
    }


def setup():
    push = subject()
    push.connected = True
    push.owner.hass.config = SimpleNamespace(time_zone="UTC")
    with (
        patch.object(polling_module.time, "monotonic", return_value=100),
        patch.object(polling_module.time, "time", return_value=100),
    ):
        assert push.polling.observed(report(100), 30).total_seconds() == 30
    return push


def read(push, stamp, *, hint=True, data=None, interval=30):
    with (
        patch.object(polling_module.time, "monotonic", return_value=stamp),
        patch.object(polling_module.time, "time", return_value=stamp),
    ):
        if hint:
            push.polling.telemetry_hint()
        return push.polling.observed(
            report(stamp) if data is None else data, interval
        ).total_seconds()


def qualify(push):
    assert read(push, 110) == 30
    assert read(push, 120) == 30
    assert read(push, 130) == 60


def test_only_consecutive_fresh_reports_qualify():
    push = setup()
    qualify(push)
    assert push.polling.diagnostics()["backup_polling"]


@pytest.mark.parametrize("bad", ["old", "future", "missing", "nan", "backwards"])
def test_bad_reports_never_qualify(bad):
    push = setup()
    for stamp in (110, 120, 130, 140):
        data = report(stamp)
        if bad == "old":
            data = report(1)
        elif bad == "future":
            data = report(1000)
        elif bad == "missing":
            del data["lastUpdate"]
        elif bad == "nan":
            data["power"] = float("nan")
        elif bad == "backwards":
            data = report(150 - stamp / 5)
        assert read(push, stamp, data=data) == 30


def test_events_without_new_data_or_read_failures_cannot_qualify():
    push = setup()
    for stamp in (110, 120, 130):
        assert read(push, stamp, data=report(100)) == 30
    assert push.polling.samples < 3


def test_regular_polls_and_charging_events_do_not_qualify():
    push = setup()
    for stamp in (110, 120, 130):
        assert read(push, stamp, hint=False) == 30
    assert push.polling.samples == 0


@pytest.mark.parametrize(
    "cause", ["silence", "disconnect", "failure", "tcp", "transition", "epoch"]
)
def test_qualification_revoked_and_tcp_scheduler_untouched(cause):
    push = setup()
    qualify(push)
    push.owner.update_interval = "sentinel"
    stamp = 140
    if cause == "silence":
        stamp = 160
    elif cause == "disconnect":
        push.connected = False
    elif cause == "failure":
        push.owner.last_update_success = False
    elif cause == "tcp":
        push.owner.local = True
    elif cause == "transition":
        push.owner.transitioning = True
    elif cause == "epoch":
        push.owner.routing_epoch += 1
    with patch.object(polling_module.time, "monotonic", return_value=stamp):
        refresh = push.polling.check()
    assert not push.polling.extended
    assert refresh == (cause in {"silence", "disconnect", "epoch"})
    if cause in {"tcp", "transition"}:
        assert push.owner.update_interval == "sentinel"
    else:
        assert push.owner.update_interval.total_seconds() == 30


def test_noisy_hints_do_not_extend_lease():
    push = setup()
    qualify(push)
    with patch.object(polling_module.time, "monotonic", return_value=160):
        push.polling.telemetry_hint()
        assert push.polling.check()
    assert not push.polling.extended


def test_state_interval_change_requires_new_qualification():
    push = setup()
    qualify(push)
    assert read(push, 140, interval=60) == 60
    assert not push.polling.extended


def test_slow_stream_cannot_qualify():
    push = setup()
    for stamp in (140, 180, 220):
        assert read(push, stamp) == 30


def test_fresh_stream_keeps_bounded_backup_interval():
    push = setup()
    qualify(push)
    assert read(push, 140) == 60
    with patch.object(polling_module.time, "monotonic", return_value=150):
        assert not push.polling.check()
    assert push.polling.extended


def test_failed_read_restores_interval_before_ha_schedules_retry():
    push = setup()
    qualify(push)
    push.owner.update_interval = None
    push.polling.failed()
    assert not push.polling.extended
    assert push.owner.update_interval.total_seconds() == 30
    assert push.owner.last_update_success


def test_manual_disable_polling_is_respected_by_backup_watchdog():
    push = setup()
    qualify(push)
    push.owner.config_entry = SimpleNamespace(pref_disable_polling=True)
    push.connected = False
    assert not push.polling.check()
    assert not push.polling.extended
