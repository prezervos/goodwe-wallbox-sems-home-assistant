"""Actual energy-flow regressions, separate from session controls and safety."""
import importlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from tests.test_native_transport import PACKAGE

state = importlib.import_module(PACKAGE + ".observed_state")
binary = importlib.import_module(PACKAGE + ".binary_sensor")
entities = importlib.import_module(PACKAGE + ".native_entities")
NOW = 1790267000.0


def owner(values, *, local=False):
    return SimpleNamespace(
        serial="TEST", data={"TEST": {"transport": "tcp" if local else "cloud",
            "lastUpdate": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            "observed_at": 100.0, **values}},
        local=local, last_update_success=True, transitioning=False, _closed=False,
        cloud_restored_at=None, hass=SimpleNamespace(config=SimpleNamespace(time_zone="UTC")),
        transport=SimpleNamespace(available=True, observed_at=100.0,
            session_guard=SimpleNamespace(phase="idle", mode=0)),
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(binary.time, "time", lambda: NOW)


@pytest.mark.parametrize("power,expected", [
    (0, False), ("0", False), (0.001, True), ("4.1", True),
    (None, None), ("", None), ("unknown", None), (True, None), (False, None),
    (-1, None), ("nan", None), (float("inf"), None), ([], None),
])
def test_cloud_activity_uses_measurement_without_faking_invalid_zero(power, expected):
    entity = binary.ChargingActiveSensor(owner({"status": "charging", "power": power}))
    assert entity.is_on is expected


@pytest.mark.parametrize("power,currents,expected", [
    (0, [0, 0, 0], False), (4.1, [6, 6, 6], True),
    (0, [0, 2.9, 0], None), (4.1, [0, 0, 0], None),
    (0, None, None), (0, [0, 0], None), (0, [False, 0, 0], None),
    (0, [0, float("nan"), 0], None), (0, [0, -1, 0], None),
])
def test_native_energy_requires_consistent_phase_measurements(power, currents, expected):
    entity = binary.ChargingActiveSensor(owner({
        "raw_state": 2, "connection": 1, "power": power, "currents_a": currents,
    }, local=True))
    assert entity.is_on is expected


@pytest.mark.parametrize("power", [0, 4.1])
@pytest.mark.parametrize("change", [
    "failed", "transition", "closed", "handover", "wrong_transport",
    "missing_timestamp", "invalid_timestamp", "stale", "future",
])
def test_cloud_invalid_observation_never_becomes_on_or_off(power, change):
    coordinator = owner({"status": "charging", "power": power})
    values = coordinator.data["TEST"]
    if change == "failed": coordinator.last_update_success = False
    elif change == "transition": coordinator.transitioning = True
    elif change == "closed": coordinator._closed = True
    elif change == "handover": coordinator.cloud_restored_at = NOW
    elif change == "wrong_transport": values["transport"] = "tcp"
    elif change == "missing_timestamp": values.pop("lastUpdate")
    elif change == "invalid_timestamp": values["lastUpdate"] = "invalid"
    else:
        offset = -601 if change == "stale" else 6
        values["lastUpdate"] = datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat()
    assert binary.ChargingActiveSensor(coordinator).is_on is None


@pytest.mark.parametrize("change", ["unavailable", "old_report", "wrong_transport"])
def test_native_stale_or_replaced_report_is_unknown(change):
    coordinator = owner({"power": 0, "currents_a": [0, 0, 0]}, local=True)
    if change == "unavailable": coordinator.transport.available = False
    elif change == "old_report": coordinator.transport.observed_at = 101
    else: coordinator.data["TEST"]["transport"] = "cloud"
    assert binary.ChargingActiveSensor(coordinator).is_on is None


def test_physical_cloud_completion_sequence_preserves_session_controls():
    coordinator = owner({})
    activity = binary.ChargingActiveSensor(coordinator)
    wallbox = entities.ValueSensor(coordinator, "TEST_status", "status", "status")
    vehicle = entities.ValueSensor(coordinator, "TEST_vehicle", "workstate", "workstate")
    switch = entities.ChargingSwitch(coordinator, "TEST_switch", "start_charging")
    for status, workstate, power, expected_flow, expected_vehicle, expected_switch in [
        ("EVDetail_Status_Title_Charging", "", "4.1", True, "connected", True),
        ("EVDetail_Status_Title_Charging", "", "0", False, "connected", True),
        ("EVDetail_Status_Title_Waiting", "EVDetail_Status_Waiting_Stat02", "0", False, "finished_charging", False),
        ("EVDetail_Status_Title_Waiting", "EVDetail_Status_Waiting_Stat01", "0", False, "connected", False),
    ]:
        coordinator.data["TEST"].update(status=status, workstate=workstate, power=power)
        assert activity.is_on is expected_flow
        assert vehicle.native_value == expected_vehicle
        assert switch.is_on is expected_switch
        assert wallbox.native_value == ("charging" if expected_switch else "standby")
    assert activity._attr_unique_id == "TEST_charging_active"
    assert activity._attr_translation_key == "charging_active"
    assert activity._attr_entity_registry_enabled_default is False


def test_native_zero_load_connection_does_not_authorize_idle_controls():
    values = {"raw_state": 2, "connection": 1, "power": 0, "currents_a": [0, 0, 0]}
    assert state.vehicle_state(values, local=True) == "connected"
    assert state.energy_flow_active(values, local=True) is False
    # Stop/fallback and idle-only settings still require confirmed session idle.
    assert state.charging_active(values, local=True) is None
    assert state.charging_active({"status": "charging", "power": "0"}, local=False) is True


@pytest.mark.parametrize("offset,expected", [(-600, False), (-601, None), (5, False), (6, None)])
def test_cloud_freshness_boundaries(offset, expected):
    coordinator = owner({"power": 0,
        "lastUpdate": datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat()})
    assert binary.ChargingActiveSensor(coordinator).is_on is expected


@pytest.mark.parametrize("stamp", [NOW, int(NOW * 1000), str(int(NOW)),
    datetime.fromtimestamp(NOW, ZoneInfo("Europe/Prague")).isoformat(),
    datetime.fromtimestamp(NOW, ZoneInfo("Europe/Prague")).replace(tzinfo=None).isoformat(),
])
def test_cloud_timestamp_formats_use_configured_timezone(stamp):
    coordinator = owner({"power": 0, "lastUpdate": stamp})
    coordinator.hass.config.time_zone = "Europe/Prague"
    assert binary.ChargingActiveSensor(coordinator).is_on is False
