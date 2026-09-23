"""Public status and device identity contracts shared by cloud and TCP."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_native_transport import PACKAGE

entities = importlib.import_module(PACKAGE + ".native_entities")


def owner(values, *, local=False, phase="idle", settings=None, identity=None):
    return SimpleNamespace(
        serial="TEST", data={"TEST": values}, local=local,
        last_update_success=True,
        transport=SimpleNamespace(session_guard=SimpleNamespace(phase=phase)),
        cloud_settings=SimpleNamespace(values=settings or {}),
        resolved_device_identity=identity or {},
    )


@pytest.mark.parametrize("raw,expected", [
    ("charging", "charging"), ("EVDetail_Status_Title_Charging", "charging"),
    ("waiting", "standby"), ("available", "standby"), ("standby", "standby"),
    ("EVDetail_Status_Title_Waiting", "standby"),
    ("offline", "offline"), ("unavailable", "offline"),
    ("EVDetail_Status_Title_Offline", "offline"),
    (None, "unknown"), ("unexpected_vendor_string", "unknown"),
])
def test_status_is_bounded_translated_enum(raw, expected):
    entity = entities.ValueSensor(owner({"status": raw}), "TEST", "status", "status")
    assert entity.native_value == expected
    assert entity._attr_device_class == "enum"
    assert entity._attr_unique_id == "TEST"
    component = Path(__file__).parents[1] / "custom_components/sems_wallbox"
    for name in ("strings.json", "translations/en.json", "translations/cs.json"):
        catalog = json.loads((component / name).read_text(encoding="utf-8"))
        assert set(entity._attr_options) == set(catalog["entity"]["sensor"]["status"]["state"])


@pytest.mark.parametrize("phase", ["starting", "waiting"])
def test_native_session_progress_keeps_existing_states(phase):
    entity = entities.ValueSensor(owner({"status": "standby"}, local=True, phase=phase),
                                  "TEST", "status", "status")
    assert entity.native_value == phase
    entity.coordinator.local = False
    assert entity.native_value == "standby"


def test_failed_telemetry_stays_unavailable(monkeypatch):
    monkeypatch.setattr(entities.CoordinatorEntity, "available",
                        property(lambda self: self.coordinator.last_update_success), raising=False)
    entity = entities.ValueSensor(owner({"status": "standby"}), "TEST", "status", "status")
    entity.coordinator.last_update_success = False
    assert not entity.available


@pytest.mark.parametrize("values,settings,identity,expected", [
    ({"model": "MODEL", "fireware": "1010"}, {}, {}, {"model": "MODEL", "sw_version": "1010"}),
    ({}, {"model": "MODEL", "fireware": "1010"}, {}, {"model": "MODEL", "sw_version": "1010"}),
    ({}, {}, {"product_model": "CONFIGURED"}, {"model": "CONFIGURED"}),
    ({"model": "unknown", "fireware": ""}, {}, {}, {}),
    ({"model": None, "fireware": None}, {}, {}, {}),
    ({"model": " unknown ", "fireware": " unavailable "}, {}, {"product_model": "unknown"}, {}),
    ({"model": "NEW", "fireware": "1011"}, {"model": "OLD", "fireware": "1010"}, {},
     {"model": "NEW", "sw_version": "1011"}),
])
def test_known_device_metadata_only(values, settings, identity, expected):
    entity = entities.NativeEntity(owner(values, settings=settings, identity=identity),
                                   "TEST", "status")
    assert entity.device_info == {
        "identifiers": {("sems_wallbox", "TEST")},
        "manufacturer": "GoodWe", "name": "GoodWe Wallbox TEST", **expected,
    }
