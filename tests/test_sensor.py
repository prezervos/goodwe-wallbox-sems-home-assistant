"""Unit tests for sensor.py — SemsSensor and SemsWorkStateSensor."""

import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Minimal HA stubs
# ---------------------------------------------------------------------------

def _ensure_ha_stubs():
    """Register minimal HA stubs so platform modules can be imported."""
    for name in [
        "homeassistant",
        "homeassistant.exceptions",
        "homeassistant.components",
        "homeassistant.components.sensor",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.update_coordinator",
    ]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    # SensorDeviceClass, SensorStateClass, SensorEntity
    sensor_mod = sys.modules["homeassistant.components.sensor"]
    if not hasattr(sensor_mod, "SensorDeviceClass"):
        class SensorDeviceClass:
            ENUM = "enum"
            POWER = "power"
            ENERGY = "energy"
            CURRENT = "current"
        class SensorStateClass:
            TOTAL_INCREASING = "total_increasing"
        class SensorEntity:
            pass
        sensor_mod.SensorDeviceClass = SensorDeviceClass
        sensor_mod.SensorStateClass = SensorStateClass
        sensor_mod.SensorEntity = SensorEntity

    # CoordinatorEntity, DataUpdateCoordinator
    coord_mod = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(coord_mod, "CoordinatorEntity"):
        class CoordinatorEntity:
            def __init__(self, coordinator):
                self.coordinator = coordinator
            async def async_added_to_hass(self):
                pass
        class DataUpdateCoordinator:
            pass
        class UpdateFailed(Exception):
            pass
        coord_mod.CoordinatorEntity = CoordinatorEntity
        coord_mod.DataUpdateCoordinator = DataUpdateCoordinator
        coord_mod.UpdateFailed = UpdateFailed

    # UnitOfPower, UnitOfEnergy, UnitOfElectricCurrent
    const_mod = sys.modules["homeassistant.const"]
    if not hasattr(const_mod, "UnitOfPower"):
        class UnitOfPower:
            KILO_WATT = "kW"
        class UnitOfEnergy:
            KILO_WATT_HOUR = "kWh"
        class UnitOfElectricCurrent:
            AMPERE = "A"
        const_mod.UnitOfPower = UnitOfPower
        const_mod.UnitOfEnergy = UnitOfEnergy
        const_mod.UnitOfElectricCurrent = UnitOfElectricCurrent

    # HA exceptions
    ha_exc = sys.modules["homeassistant.exceptions"]
    if not hasattr(ha_exc, "HomeAssistantError"):
        class HomeAssistantError(Exception):
            pass
        ha_exc.HomeAssistantError = HomeAssistantError

    # HomeAssistant core
    core_mod = sys.modules["homeassistant.core"]
    if not hasattr(core_mod, "HomeAssistant"):
        core_mod.HomeAssistant = object
        core_mod.callback = lambda f: f

    # config_entries
    ce_mod = sys.modules["homeassistant.config_entries"]
    if not hasattr(ce_mod, "ConfigEntry"):
        class ConfigEntry:
            entry_id = "test_entry"
        ce_mod.ConfigEntry = ConfigEntry

    # entity_platform
    ep_mod = sys.modules["homeassistant.helpers.entity_platform"]
    if not hasattr(ep_mod, "AddEntitiesCallback"):
        ep_mod.AddEntitiesCallback = object


_ensure_ha_stubs()

# ---------------------------------------------------------------------------
# Import the modules under test
# ---------------------------------------------------------------------------

_HERE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "custom_components", "sems-wallbox")


def _load_module(name, filepath):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# coordinator stub
coord_stub = types.ModuleType("coordinator")
class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True
coord_stub.SemsUpdateCoordinator = _FakeCoordinator
sys.modules["coordinator"] = coord_stub

# const stub
const_stub = types.ModuleType("const")
const_stub.DOMAIN = "sems-wallbox"
sys.modules["const"] = const_stub

# Patch relative imports inside sensor.py
# We need ".coordinator" and ".const" to point to our stubs
# Use a package trick: register a fake package
pkg = types.ModuleType("sems_wallbox_pkg")
sys.modules.setdefault("sems_wallbox_pkg", pkg)
sys.modules["sems_wallbox_pkg.coordinator"] = coord_stub
sys.modules["sems_wallbox_pkg.const"] = const_stub

# Monkey-patch: load sensor.py as a top-level module but pretend its package exports
sensor_source = os.path.join(_HERE, "sensor.py")
spec = importlib.util.spec_from_file_location("sems_wallbox_pkg.sensor", sensor_source)
sensor_mod = importlib.util.module_from_spec(spec)
sensor_mod.__package__ = "sems_wallbox_pkg"
sys.modules["sems_wallbox_pkg.sensor"] = sensor_mod
spec.loader.exec_module(sensor_mod)

SemsSensor = sensor_mod.SemsSensor
SemsWorkStateSensor = sensor_mod.SemsWorkStateSensor
SemsPowerSensor = sensor_mod.SemsPowerSensor
SemsStatisticsSensor = sensor_mod.SemsStatisticsSensor
SemsCurrentSensor = sensor_mod.SemsCurrentSensor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_SN = "GWSN001"

SAMPLE_DATA = {
    "sn": SAMPLE_SN,
    "name": "My Wallbox",
    "model": "AC Charger Pro",
    "fireware": "1.2.3",
    "status": "EVDetail_Status_Title_Charging",
    "workstate": "EVDetail_Status_Waiting_Stat01",
    "power": 7.4,
    "current": 32.0,
    "chargeEnergy": "123.5",
    "chargeMode": 0,
    "max_charge_power": 11,
    "set_charge_power": 7.4,
    "startStatus": True,
}


def _make_coordinator(data=None):
    return _FakeCoordinator({SAMPLE_SN: data or SAMPLE_DATA.copy()})


# ===========================================================================
# SemsSensor
# ===========================================================================

class TestSemsSensor:
    def test_state_charging(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.state == "charging"

    def test_state_standby(self):
        d = {**SAMPLE_DATA, "status": "EVDetail_Status_Title_Waiting"}
        coord = _make_coordinator(d)
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.state == "standby"

    def test_state_offline(self):
        d = {**SAMPLE_DATA, "status": "EVDetail_Status_Title_Offline"}
        coord = _make_coordinator(d)
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.state == "offline"

    def test_state_unknown(self):
        d = {**SAMPLE_DATA, "status": "EVDetail_Status_SomethingElse"}
        coord = _make_coordinator(d)
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.state == "unknown"

    def test_icon_charging(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.icon == "mdi:battery-charging-100"

    def test_icon_standby(self):
        d = {**SAMPLE_DATA, "status": "EVDetail_Status_Title_Waiting"}
        coord = _make_coordinator(d)
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.icon == "mdi:ev-station"

    def test_icon_offline(self):
        d = {**SAMPLE_DATA, "status": "EVDetail_Status_Title_Offline"}
        coord = _make_coordinator(d)
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.icon == "mdi:power-plug-off"

    def test_icon_unknown(self):
        d = {**SAMPLE_DATA, "status": "EVDetail_Status_Other"}
        coord = _make_coordinator(d)
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.icon == "mdi:help-circle-outline"

    def test_translation_key_is_status(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor._attr_translation_key == "status"

    def test_has_entity_name(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor._attr_has_entity_name is True

    def test_unique_id(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.unique_id == SAMPLE_SN

    def test_available_follows_coordinator(self):
        coord = _make_coordinator()
        coord.last_update_success = False
        sensor = SemsSensor(coord, SAMPLE_SN)
        assert sensor.available is False

    def test_extra_state_attributes_contains_status_text(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        attrs = sensor.extra_state_attributes
        assert "statusText" in attrs
        assert attrs["statusText"] == SAMPLE_DATA["status"]

    def test_device_info_has_identifiers(self):
        coord = _make_coordinator()
        sensor = SemsSensor(coord, SAMPLE_SN)
        info = sensor.device_info
        assert ("sems-wallbox", SAMPLE_SN) in info["identifiers"]
        assert info["manufacturer"] == "GoodWe"
        assert info["model"] == "AC Charger Pro"
        assert info["sw_version"] == "1.2.3"
        assert info["name"] == "My Wallbox"


# ===========================================================================
# SemsWorkStateSensor
# ===========================================================================

class TestSemsWorkStateSensor:
    def _sensor(self, workstate: str):
        d = {**SAMPLE_DATA, "workstate": workstate}
        coord = _make_coordinator(d)
        return SemsWorkStateSensor(coord, SAMPLE_SN)

    def test_not_plugged_in(self):
        s = self._sensor("EVDetail_Status_Waiting_Stat00")
        assert s.native_value == "not_plugged_in"
        assert s.icon == "mdi:power-plug-off-outline"

    def test_connected(self):
        s = self._sensor("EVDetail_Status_Waiting_Stat01")
        assert s.native_value == "connected"
        assert s.icon == "mdi:power-plug"

    def test_finished_charging(self):
        s = self._sensor("EVDetail_Status_Waiting_Stat02")
        assert s.native_value == "finished_charging"
        assert s.icon == "mdi:battery-check"

    def test_charging_dash(self):
        s = self._sensor("")
        assert s.native_value == "dash"
        assert s.icon == "mdi:progress-clock"

    def test_unknown(self):
        s = self._sensor("SOMETHING_ELSE")
        assert s.native_value == "unknown"
        assert s.icon == "mdi:help-circle-outline"

    def test_unique_id(self):
        s = self._sensor("EVDetail_Status_Waiting_Stat00")
        assert s.unique_id == f"{SAMPLE_SN}_workstate"

    def test_translation_key(self):
        s = self._sensor("EVDetail_Status_Waiting_Stat00")
        assert s._attr_translation_key == "workstate"

    def test_device_info(self):
        s = self._sensor("EVDetail_Status_Waiting_Stat00")
        info = s.device_info
        assert ("sems-wallbox", SAMPLE_SN) in info["identifiers"]
        assert info["manufacturer"] == "GoodWe"


# ===========================================================================
# SemsPowerSensor
# ===========================================================================

class TestSemsPowerSensor:
    def test_normal_power(self):
        coord = _make_coordinator()
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s.native_value == pytest.approx(7.4)

    def test_not_charging_returns_zero(self):
        # startStatus=False means device is idle — chargePower is just the configured limit
        d = {**SAMPLE_DATA, "startStatus": False, "power": 5.8}
        coord = _make_coordinator(d)
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s.native_value == 0.0

    def test_no_start_status_defaults_to_zero(self):
        # Missing startStatus treated as not charging
        d = {k: v for k, v in SAMPLE_DATA.items() if k != "startStatus"}
        d["power"] = 5.8
        coord = _make_coordinator(d)
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s.native_value == 0.0

    def test_negative_power_clamped_to_zero(self):
        d = {**SAMPLE_DATA, "power": -1.5}
        coord = _make_coordinator(d)
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s.native_value == 0.0

    def test_none_power_defaults_to_zero(self):
        d = {**SAMPLE_DATA, "power": None}
        coord = _make_coordinator(d)
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s.native_value == 0.0

    def test_unique_id(self):
        coord = _make_coordinator()
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s.unique_id == f"{SAMPLE_SN}_power"

    def test_translation_key(self):
        coord = _make_coordinator()
        s = SemsPowerSensor(coord, SAMPLE_SN)
        assert s._attr_translation_key == "power"


# ===========================================================================
# SemsStatisticsSensor
# ===========================================================================

class TestSemsStatisticsSensor:
    def test_parse_string_value(self):
        from decimal import Decimal
        coord = _make_coordinator()
        s = SemsStatisticsSensor(coord, SAMPLE_SN)
        assert s.native_value == Decimal("123.5")

    def test_fallback_on_invalid_value(self):
        from decimal import Decimal
        d = {**SAMPLE_DATA, "chargeEnergy": "not_a_number"}
        coord = _make_coordinator(d)
        s = SemsStatisticsSensor(coord, SAMPLE_SN)
        assert s.native_value == Decimal("0")

    def test_unique_id(self):
        coord = _make_coordinator()
        s = SemsStatisticsSensor(coord, SAMPLE_SN)
        assert s.unique_id == f"{SAMPLE_SN}-energy"


# ===========================================================================
# SemsCurrentSensor
# ===========================================================================

class TestSemsCurrentSensor:
    def test_normal_current(self):
        coord = _make_coordinator()
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s.native_value == pytest.approx(32.0)

    def test_negative_current_falls_back_to_power(self):
        # When current field is negative, derive from power (7.4 kW / 230 V ≈ 32.2 A)
        d = {**SAMPLE_DATA, "current": -5.0}
        coord = _make_coordinator(d)
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s.native_value == pytest.approx(7.4 * 1000.0 / 230.0, abs=0.2)

    def test_none_current_falls_back_to_power(self):
        # When current field is absent/None, derive from power (7.4 kW / 230 V ≈ 32.2 A)
        d = {**SAMPLE_DATA, "current": None}
        coord = _make_coordinator(d)
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s.native_value == pytest.approx(7.4 * 1000.0 / 230.0, abs=0.2)

    def test_unique_id(self):
        coord = _make_coordinator()
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s.unique_id == f"{SAMPLE_SN}_current"

    def test_translation_key(self):
        coord = _make_coordinator()
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s._attr_translation_key == "current"

    def test_available_when_coordinator_success(self):
        coord = _make_coordinator()
        coord.last_update_success = True
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s.available is True

    def test_unavailable_when_coordinator_fails(self):
        coord = _make_coordinator()
        coord.last_update_success = False
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        assert s.available is False

    def test_device_info(self):
        coord = _make_coordinator()
        s = SemsCurrentSensor(coord, SAMPLE_SN)
        info = s.device_info
        assert info["name"] == "My Wallbox"
