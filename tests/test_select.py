"""Unit tests for select.py — InverterOperationModeEntity."""

import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock, AsyncMock, call
import pytest

# ---------------------------------------------------------------------------
# All HA stubs are set up by conftest.py before this file is collected.
# ---------------------------------------------------------------------------

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "sems-wallbox")

# Add EntityCategory stub
_const_mod = sys.modules["homeassistant.const"]
if not hasattr(_const_mod, "EntityCategory"):
    class EntityCategory:
        CONFIG = "config"
    _const_mod.EntityCategory = EntityCategory

# EntityCategory is also imported from homeassistant.const in select.py
# but HA actually defines it in homeassistant.const — stub it there too
import homeassistant.const as _ha_const
if not hasattr(_ha_const, "EntityCategory"):
    class EntityCategory:
        CONFIG = "config"
    _ha_const.EntityCategory = EntityCategory

# --------------------------------------------------------------------------
# Load select.py under its own package namespace
# --------------------------------------------------------------------------
_pkg_name = "sems_wallbox_pkg_select"

_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [_HERE]
_pkg.__package__ = _pkg_name
sys.modules[_pkg_name] = _pkg

_const = types.ModuleType(f"{_pkg_name}.const")
_const.DOMAIN = "sems-wallbox"
sys.modules[f"{_pkg_name}.const"] = _const
setattr(_pkg, "const", _const)

_coord_stub = types.ModuleType(f"{_pkg_name}.coordinator")


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True

    async def async_request_refresh(self):
        pass


_coord_stub.SemsUpdateCoordinator = _FakeCoordinator
sys.modules[f"{_pkg_name}.coordinator"] = _coord_stub
setattr(_pkg, "coordinator", _coord_stub)

_spec = importlib.util.spec_from_file_location(
    f"{_pkg_name}.select", os.path.join(_HERE, "select.py")
)
_select_mod = importlib.util.module_from_spec(_spec)
_select_mod.__package__ = _pkg_name
sys.modules[f"{_pkg_name}.select"] = _select_mod
_spec.loader.exec_module(_select_mod)

InverterOperationModeEntity = _select_mod.InverterOperationModeEntity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_SN = "GWSN001"

SAMPLE_DATA = {
    "sn": SAMPLE_SN,
    "chargeMode": 0,
    "set_charge_power": 7.4,
    "min_charge_power": 4.2,
    "max_charge_power": 11.0,
    "name": "My Wallbox",
}


def _make_entity(chargeMode=0, set_charge_power=7.4, min_charge_power=4.2, max_charge_power=11.0):
    data = {
        **SAMPLE_DATA,
        "chargeMode": chargeMode,
        "set_charge_power": set_charge_power,
        "min_charge_power": min_charge_power,
        "max_charge_power": max_charge_power,
    }
    coordinator = _FakeCoordinator({SAMPLE_SN: data})
    api = MagicMock()
    api.set_charge_mode = MagicMock()

    entity = InverterOperationModeEntity(
        coordinator,
        api,
        SAMPLE_SN,
        _select_mod.OPERATION_MODE,
        list(_select_mod._MODE_TO_OPTION.values()),
        _select_mod._MODE_TO_OPTION.get(chargeMode),
    )
    # Minimal hass mock
    hass = MagicMock()
    hass.async_create_task = MagicMock()

    async def fake_executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = fake_executor
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    return entity


# ---------------------------------------------------------------------------
# Tests: option <-> mode mapping
# ---------------------------------------------------------------------------

class TestModeMapping:
    def test_option_to_mode_fast(self):
        assert _select_mod._OPTION_TO_MODE["fast"] == 0

    def test_option_to_mode_pv_priority(self):
        assert _select_mod._OPTION_TO_MODE["pv_priority"] == 1

    def test_option_to_mode_pv_and_battery(self):
        assert _select_mod._OPTION_TO_MODE["pv_and_battery"] == 2

    def test_mode_to_option_roundtrip(self):
        for mode, option in _select_mod._MODE_TO_OPTION.items():
            assert _select_mod._OPTION_TO_MODE[option] == mode


# ---------------------------------------------------------------------------
# Tests: API call behaviour in async_select_option
# ---------------------------------------------------------------------------

class TestSelectOption:
    @pytest.mark.asyncio
    async def test_switch_to_fast_sends_charge_power(self):
        """Switching TO fast (mode 0) must include set_charge_power in the API call."""
        entity = _make_entity(chargeMode=1, set_charge_power=6.0)
        await entity.async_select_option("fast")
        entity.api.set_charge_mode.assert_called_once_with(SAMPLE_SN, 0, 6.0)

    @pytest.mark.asyncio
    async def test_switch_to_fast_falls_back_to_min_when_power_none(self):
        """When set_charge_power is None, fall back to min_charge_power."""
        entity = _make_entity(chargeMode=1, set_charge_power=None, min_charge_power=4.2)
        await entity.async_select_option("fast")
        entity.api.set_charge_mode.assert_called_once_with(SAMPLE_SN, 0, 4.2)

    @pytest.mark.asyncio
    async def test_switch_to_fast_clamps_out_of_range_power_to_min(self):
        """When set_charge_power is out of range, clamp it to min."""
        entity = _make_entity(chargeMode=1, set_charge_power=1.0, min_charge_power=4.2, max_charge_power=11.0)
        await entity.async_select_option("fast")
        entity.api.set_charge_mode.assert_called_once_with(SAMPLE_SN, 0, 4.2)

    @pytest.mark.asyncio
    async def test_switch_to_pv_priority_no_charge_power(self):
        """Switching to pv_priority must NOT include charge_power."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        await entity.async_select_option("pv_priority")
        entity.api.set_charge_mode.assert_called_once_with(SAMPLE_SN, 1, None)

    @pytest.mark.asyncio
    async def test_switch_to_pv_and_battery_no_charge_power(self):
        """Switching to pv_and_battery must NOT include charge_power."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        await entity.async_select_option("pv_and_battery")
        entity.api.set_charge_mode.assert_called_once_with(SAMPLE_SN, 2, None)

    @pytest.mark.asyncio
    async def test_optimistic_update_on_select(self):
        """Current option is set optimistically before API call."""
        entity = _make_entity(chargeMode=1)
        await entity.async_select_option("fast")
        assert entity._attr_current_option == "fast"
        entity.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_unknown_option_is_ignored(self):
        """An unknown option string must not call the API."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("invalid_option")
        entity.api.set_charge_mode.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: coordinator update
# ---------------------------------------------------------------------------

class TestCoordinatorUpdate:
    def test_update_sets_fast(self):
        entity = _make_entity(chargeMode=0)
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 0
        entity._handle_coordinator_update()
        assert entity._attr_current_option == "fast"

    def test_update_sets_pv_priority(self):
        entity = _make_entity(chargeMode=0)
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity._handle_coordinator_update()
        assert entity._attr_current_option == "pv_priority"

    def test_update_sets_pv_and_battery(self):
        entity = _make_entity(chargeMode=0)
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 2
        entity._handle_coordinator_update()
        assert entity._attr_current_option == "pv_and_battery"
