"""Conservative observations and diagnostics privacy regression tests."""

import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE = "sems_observations_tests"
module = types.ModuleType(PACKAGE)
module.__path__ = [str(Path(__file__).parents[1] / "custom_components/sems_wallbox")]
sys.modules[PACKAGE] = module
state = importlib.import_module(PACKAGE + ".observed_state")
diagnostics = importlib.import_module(PACKAGE + ".diagnostics")


@pytest.mark.parametrize(
    "values,expected",
    [
        ({}, None),
        ({"workstate": "new_unknown_value"}, None),
        ({"workstate": "EVDetail_Status_Waiting_Stat00"}, "not_plugged_in"),
        ({"workstate": "EVDetail_Status_Waiting_Stat01"}, "connected"),
        ({"workstate": "EVDetail_Status_Waiting_Stat02"}, "finished_charging"),
        ({"status": "charging"}, "connected"),
        ({"startStatus": True}, None),
    ],
)
def test_cloud_vehicle_only_known_reports(values, expected):
    assert state.vehicle_state(values, local=False) == expected


@pytest.mark.parametrize(
    "values,expected",
    [
        ({}, None),
        ({"connection": 1, "raw_state": 0, "power": 0, "currents_a": [0, 0, 0]}, "connected"),
        ({"raw_state": 2, "power": 4, "currents_a": [6, 6, 6]}, "connected"),
        ({"raw_state": 2, "power": 0, "currents_a": [0, 0, 0]}, None),
    ],
)
def test_native_connector_codes_are_not_guessed(values, expected):
    assert state.vehicle_state(values, local=True) == expected


@pytest.mark.parametrize(
    "values,local,expected",
    [
        ({}, True, None),
        ({}, False, None),
        ({"startStatus": True}, False, None),
        ({"status": "EVDetail_Status_Title_Waiting"}, False, False),
        ({"status": "EVDetail_Status_Title_Charging"}, False, True),
        ({"raw_state": 0, "power": 0, "currents_a": [0, 0, 0]}, True, False),
        ({"raw_state": 0, "power": 3, "currents_a": [4, 4, 4]}, True, None),
        ({"raw_state": 2, "power": 3, "currents_a": [4, 4, 4]}, True, True),
    ],
)
def test_activity_is_not_start_intent(values, local, expected):
    assert state.charging_active(values, local=local) is expected


async def test_diagnostics_never_copies_secrets_or_free_text():
    secrets = [
        "secret-user",
        "secret-password",
        "secret-serial",
        "192.0.2.5",
        "secret-token",
    ]
    entry = SimpleNamespace(
        entry_id="entry",
        data=dict(
            zip(
                ["username", "password", "wallbox_serial_No", "native_host", "token"],
                secrets,
            )
        ),
    )
    guard = SimpleNamespace(limit=4.2, error="failure containing secret-token")
    coordinator = SimpleNamespace(
        data={
            "secret-serial": {
                "power": "4.2",
                "fault_code": "secret-password",
                "status": "secret-user",
                "arbitrary": secrets,
            }
        },
        last_update_success=True,
        local=True,
        transitioning=False,
        endpoint=SimpleNamespace(journal={"endpoint": "192.0.2.5"}),
        cloud_restored_at=None,
        transport=SimpleNamespace(available=True, observed_at=0, session_guard=guard),
        charge_mode_policy=SimpleNamespace(desired_mode=0, desired_power=4.2),
    )
    coordinator.cloud_push = SimpleNamespace(
        connected=True, refresh_count=2, last_event_at=None,
        event_counts={"telemetry": 1, "charging": 2},
        settings={"password": "secret-password"},
        polling=SimpleNamespace(diagnostics=lambda: {"backup_polling": False}),
    )
    hass = SimpleNamespace(
        data={"sems_wallbox": {"entry": {"coordinator": coordinator}}}
    )
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["cloud_push"] == {"connected": True, "refresh_count": 2, "telemetry_events": 1, "charging_events": 2, "last_event_age_seconds": None, "polling": {"backup_polling": False}}
    assert result["measurements"]["power"] == 4.2
    assert result["measurements"]["fault_code"] is None
    assert result["native"]["protection_issue"] is True
    assert all(secret not in json.dumps(result) for secret in secrets)


@pytest.mark.parametrize("connection,expected", [
    (0, "not_plugged_in"), (1, "connected"), (2, None),
    (255, None), (None, None), (True, None), ("1", None),
])
def test_verified_idle_connector_values(connection, expected):
    values = {"raw_state": 0, "power": 0, "currents_a": [0, 0, 0],
              "connection": connection}
    assert state.vehicle_state(values, local=True) == expected


@pytest.mark.parametrize("overrides", [
    {"raw_state": 3}, {"raw_state": 7}, {"power": 1},
    {"currents_a": [1, 0, 0]}, {"currents_a": None},
])
def test_idle_connector_mapping_rejects_unverified_or_conflicting_reports(overrides):
    values = {"raw_state": 0, "power": 0, "currents_a": [0, 0, 0],
              "connection": 1, **overrides}
    assert state.vehicle_state(values, local=True) is None


@pytest.mark.parametrize("last_status", [6, 8])
@pytest.mark.parametrize("workstate,expected", [
    (None, None),
    ("EVDetail_Status_Waiting_Stat00", "not_plugged_in"),
    ("EVDetail_Status_Waiting_Stat01", "connected"),
    ("EVDetail_Status_Waiting_Stat02", "finished_charging"),
])
def test_last_session_does_not_override_current_vehicle(last_status, workstate, expected):
    values = {"workstate": workstate, "last_charge_work_status": last_status}
    assert state.vehicle_state(values, local=False) == expected
