"""Session energy identity and transport boundaries without hardware writes."""

import importlib
from types import SimpleNamespace

import pytest

from tests.test_native_transport import PACKAGE

entities = importlib.import_module(PACKAGE + ".native_entities")


def coordinator(values, *, local=False):
    return SimpleNamespace(serial="TEST", data={"TEST": values}, local=local,
                           last_update_success=True)


@pytest.mark.parametrize("values,expected", [
    ({"chargeEnergy": "1.25"}, 1.25),
    ({"last_charge_energy": "2.5"}, 2.5),
    ({"chargeEnergy": 0}, 0),
    ({}, None),
    ({"chargeEnergy": None, "last_charge_energy": 99}, None),
] + [({"chargeEnergy": value}, None)
     for value in (True, -1, "invalid", "nan", "inf", float("-inf"))])
def test_observed_energy_without_fabricated_values(values, expected):
    entity = entities.SessionEnergySensor(coordinator(values), "TEST-energy", "energy")
    assert entity.native_value == expected


def test_registration_identity_and_transport_round_trip(monkeypatch):
    monkeypatch.setattr(
        entities.CoordinatorEntity, "available",
        property(lambda self: self.coordinator.last_update_success), raising=False,
    )
    owner = coordinator({"chargeEnergy": 3.25})
    registered = []
    entities.setup_platform("sensor", owner, registered.extend)
    entity, = [item for item in registered
               if isinstance(item, entities.SessionEnergySensor)]
    assert entity._attr_unique_id == "TEST-energy"
    assert entity._attr_translation_key == "energy"
    assert entity._attr_native_unit_of_measurement == "kWh"
    assert entity._attr_state_class == "total_increasing"
    assert entity.native_value == 3.25
    assert entity.available is True
    owner.last_update_success = False
    assert entity.available is False
    owner.last_update_success = True
    owner.local = True
    assert entity.native_value is None
    assert entity.available is True
    owner.data = {"TEST": {"session_energy_kwh": 0.06, "chargeEnergy": 99}}
    assert entity.native_value == 0.06
    owner.data["TEST"]["session_energy_kwh"] = 0
    assert entity.native_value == 0
    owner.local = False
    owner.data = {"TEST": {"session_energy_kwh": 0.06}}
    assert entity.native_value is None
    owner.data = {"TEST": {"chargeEnergy": 0}}
    assert entity.native_value == 0


@pytest.mark.parametrize("local,values,expected", [
    (False, {"last_charge_duration_minutes": 42}, 42),
    (False, {"time": "12"}, None),
    (False, {"time": "0"}, 0),
    (False, {"session_seconds": 600}, None),
    (True, {"session_seconds": 90}, 1.5),
    (True, {"last_charge_duration_minutes": 42}, None),
] + [(False, {"last_charge_duration_minutes": raw}, None)
     for raw in (True, -1, "nan", "inf", "bad")])
def test_duration_preserves_upstream_minutes_without_cross_transport_values(
    local, values, expected
):
    """Keep the same identity/unit and use only verified duration fields."""
    registered = []
    entities.setup_platform("sensor", coordinator(values, local=local), registered.extend)
    entity = next(item for item in registered
                  if item._attr_unique_id == "TEST_charge_duration")
    assert entity._attr_native_unit_of_measurement == "min"
    assert entity._attr_state_class == "measurement"
    assert entity.native_value == expected


def test_status_preserves_reported_legacy_attributes_without_defaults():
    """Automations can keep reading reported attributes, including false/zero."""
    values = {"status": "standby", "chargeMode": 0, "set_charge_power": 4.2,
              "ensure_minimum_charging_power": False, "scheduleMode": None}
    entity = entities.ValueSensor(coordinator(values), "TEST", "status", "status")
    assert entity.extra_state_attributes == {
        "statusText": "standby", "chargeMode": 0, "set_charge_power": 4.2,
        "ensure_minimum_charging_power": False,
    }
    entity.coordinator.data = {"TEST": {"status": "charging"}}
    assert entity.extra_state_attributes == {"statusText": "charging"}


def test_captured_tcp_session_reaches_existing_energy_entity(monkeypatch):
    """Replay physical charging/reset frames through decoder and energy entity."""
    import json
    from pathlib import Path

    protocol = importlib.import_module(PACKAGE + ".native_protocol")
    capture = json.loads((Path(__file__).parent / "fixtures/hca_session_energy.json").read_text())
    monkeypatch.setattr(
        entities.CoordinatorEntity, "available",
        property(lambda self: self.coordinator.last_update_success), raising=False,
    )
    owner = coordinator({}, local=True)
    entity = entities.SessionEnergySensor(owner, "TEST-energy", "energy")
    for record in capture["frames"]:
        frame, = protocol.NativeDecoder().feed(bytes.fromhex(record["hex"]))
        assert frame.command == record["command"]
        status = protocol.decode_status(frame, "TEST")
        owner.data = {"TEST": {"session_energy_kwh": status.session_energy_kwh}}
        assert status.session_seconds == record["seconds"]
        assert entity.available
        assert entity.native_value == record["energy_kwh"]
    owner.last_update_success = False
    assert not entity.available
