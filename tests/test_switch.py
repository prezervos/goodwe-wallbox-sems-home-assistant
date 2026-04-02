"""Unit tests for switch.py — SemsSwitch grace period logic."""

import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock
import time

# ---------------------------------------------------------------------------
# All HA stubs are set up by conftest.py before this file is collected.
# ---------------------------------------------------------------------------

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "sems-wallbox")

# --------------------------------------------------------------------------
# Make sure CoordinatorEntity in the stub accepts (coordinator) init arg
# --------------------------------------------------------------------------
coord_mod = sys.modules["homeassistant.helpers.update_coordinator"]

# Add SwitchDeviceClass / SwitchEntity stubs if not present
switch_comp = sys.modules.get("homeassistant.components.switch")
if switch_comp is None or not hasattr(switch_comp, "SwitchDeviceClass"):
    switch_comp = types.ModuleType("homeassistant.components.switch")
    class SwitchDeviceClass:
        SWITCH = "switch"
    class SwitchEntity:
        pass
    switch_comp.SwitchDeviceClass = SwitchDeviceClass
    switch_comp.SwitchEntity = SwitchEntity
    sys.modules["homeassistant.components.switch"] = switch_comp

# --------------------------------------------------------------------------
# Load switch.py under the "sems_wallbox_pkg_switch" package namespace
# --------------------------------------------------------------------------
_pkg_name = "sems_wallbox_pkg_switch"

_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [_HERE]
_pkg.__package__ = _pkg_name
sys.modules[_pkg_name] = _pkg

# const stub
_const = types.ModuleType(f"{_pkg_name}.const")
_const.DOMAIN = "sems-wallbox"
sys.modules[f"{_pkg_name}.const"] = _const
setattr(_pkg, "const", _const)

# coordinator stub
_coord_stub = types.ModuleType(f"{_pkg_name}.coordinator")


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True

    def async_request_refresh(self):
        pass

    def schedule_delayed_refresh(self, delay=5):
        pass


_coord_stub.SemsUpdateCoordinator = _FakeCoordinator
sys.modules[f"{_pkg_name}.coordinator"] = _coord_stub
setattr(_pkg, "coordinator", _coord_stub)

# Now load switch.py
_spec = importlib.util.spec_from_file_location(
    f"{_pkg_name}.switch", os.path.join(_HERE, "switch.py")
)
_switch_mod = importlib.util.module_from_spec(_spec)
_switch_mod.__package__ = _pkg_name
sys.modules[f"{_pkg_name}.switch"] = _switch_mod
_spec.loader.exec_module(_switch_mod)

SemsSwitch = _switch_mod.SemsSwitch
GRACE_ON_SECONDS = _switch_mod.GRACE_ON_SECONDS
GRACE_OFF_SECONDS = _switch_mod.GRACE_OFF_SECONDS


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------

SAMPLE_SN = "GWSN001"

CHARGING_DATA = {
    "sn": SAMPLE_SN,
    "status": "EVDetail_Status_Title_Charging",
    "power": 7.4,
    "startStatus": 0,
}

STANDBY_DATA = {
    "sn": SAMPLE_SN,
    "status": "EVDetail_Status_Title_Waiting",
    "power": 0.0,
    "startStatus": 1,
}


def _make_switch(data: dict, current_is_on: bool = False) -> SemsSwitch:
    coord = _FakeCoordinator({SAMPLE_SN: data})
    api = MagicMock()
    sw = SemsSwitch(coord, SAMPLE_SN, api, current_is_on)

    # hass mock needs loop.time()
    hass = MagicMock()
    hass.loop.time.return_value = time.monotonic()
    hass.async_create_task = MagicMock()
    sw.hass = hass
    return sw


# ===========================================================================
# _compute_is_on_from_data — no grace (no command issued)
# ===========================================================================

class TestComputeIsOnNoGrace:
    def test_charging_status_returns_true(self):
        sw = _make_switch(CHARGING_DATA)
        assert sw._compute_is_on_from_data(CHARGING_DATA) is True

    def test_standby_status_returns_false(self):
        sw = _make_switch(STANDBY_DATA)
        assert sw._compute_is_on_from_data(STANDBY_DATA) is False

    def test_power_above_zero_is_on(self):
        data = {**STANDBY_DATA, "power": 0.5}
        sw = _make_switch(data)
        assert sw._compute_is_on_from_data(data) is True

    def test_zero_power_not_charging_is_off(self):
        data = {**STANDBY_DATA, "power": 0.0}
        sw = _make_switch(data)
        assert sw._compute_is_on_from_data(data) is False


# ===========================================================================
# _compute_is_on_from_data — within ON grace window
# ===========================================================================

class TestComputeIsOnGraceOn:
    def test_within_grace_api_standby_stays_true(self):
        """After ON command, even if API returns Waiting/power=0, we stay ON."""
        sw = _make_switch(STANDBY_DATA)
        now = time.monotonic()
        sw._last_command_target = True
        sw._last_command_ts = now - 5  # 5 s ago — well within 130 s grace
        sw.hass.loop.time.return_value = now

        assert sw._compute_is_on_from_data(STANDBY_DATA) is True

    def test_within_grace_api_charging_returns_true(self):
        """ON grace + API already charging → True (API state already matches)."""
        sw = _make_switch(CHARGING_DATA)
        now = time.monotonic()
        sw._last_command_target = True
        sw._last_command_ts = now - 5
        sw.hass.loop.time.return_value = now

        assert sw._compute_is_on_from_data(CHARGING_DATA) is True

    def test_grace_cleared_when_api_matches_command(self):
        """When API state matches the command, grace fields are cleared."""
        sw = _make_switch(CHARGING_DATA)
        now = time.monotonic()
        sw._last_command_target = True
        sw._last_command_ts = now - 5
        sw.hass.loop.time.return_value = now

        sw._compute_is_on_from_data(CHARGING_DATA)

        assert sw._last_command_target is None
        assert sw._last_command_ts is None

    def test_after_grace_expired_follows_api(self):
        """Once ON grace window expires, follow the real API state."""
        sw = _make_switch(STANDBY_DATA)
        now = time.monotonic()
        sw._last_command_target = True
        sw._last_command_ts = now - (GRACE_ON_SECONDS + 10)  # expired
        sw.hass.loop.time.return_value = now

        assert sw._compute_is_on_from_data(STANDBY_DATA) is False


# ===========================================================================
# _compute_is_on_from_data — within OFF grace window
# ===========================================================================

class TestComputeIsOnGraceOff:
    def test_within_grace_api_charging_stays_false(self):
        """After OFF command, even if API still reports Charging, we stay OFF."""
        sw = _make_switch(CHARGING_DATA)
        now = time.monotonic()
        sw._last_command_target = False
        sw._last_command_ts = now - 5  # 5 s ago — well within 130 s grace
        sw.hass.loop.time.return_value = now

        assert sw._compute_is_on_from_data(CHARGING_DATA) is False

    def test_within_grace_api_standby_returns_false(self):
        """OFF grace + API already standby → False (API state already matches)."""
        sw = _make_switch(STANDBY_DATA)
        now = time.monotonic()
        sw._last_command_target = False
        sw._last_command_ts = now - 5
        sw.hass.loop.time.return_value = now

        assert sw._compute_is_on_from_data(STANDBY_DATA) is False

    def test_after_grace_expired_follows_api(self):
        """Once OFF grace window expires, follow the real API state."""
        sw = _make_switch(CHARGING_DATA)
        now = time.monotonic()
        sw._last_command_target = False
        sw._last_command_ts = now - (GRACE_OFF_SECONDS + 10)  # expired
        sw.hass.loop.time.return_value = now

        assert sw._compute_is_on_from_data(CHARGING_DATA) is True

    def test_grace_cleared_when_api_matches_command(self):
        """When API state matches the OFF command, grace fields are cleared."""
        sw = _make_switch(STANDBY_DATA)
        now = time.monotonic()
        sw._last_command_target = False
        sw._last_command_ts = now - 5
        sw.hass.loop.time.return_value = now

        sw._compute_is_on_from_data(STANDBY_DATA)

        assert sw._last_command_target is None
        assert sw._last_command_ts is None


# ===========================================================================
# unique_id and basic properties
# ===========================================================================

class TestSemsSwitchProperties:
    def test_unique_id(self):
        sw = _make_switch(CHARGING_DATA, current_is_on=True)
        assert sw.unique_id == f"{SAMPLE_SN}-switch-start-charging"

    def test_translation_key(self):
        sw = _make_switch(CHARGING_DATA)
        assert sw._attr_translation_key == "start_charging"

    def test_available_true(self):
        sw = _make_switch(CHARGING_DATA)
        assert sw.available is True

    def test_available_false_when_coordinator_fails(self):
        sw = _make_switch(CHARGING_DATA)
        sw.coordinator.last_update_success = False
        assert sw.available is False

    def test_device_info_has_identifiers(self):
        sw = _make_switch(CHARGING_DATA)
        info = sw.device_info
        assert ("sems-wallbox", SAMPLE_SN) in info["identifiers"]
        assert info["manufacturer"] == "GoodWe"
