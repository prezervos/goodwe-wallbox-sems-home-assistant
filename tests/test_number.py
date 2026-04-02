"""Unit tests for number.py — SemsNumber charge-power slider entity."""

import sys
import os
import types
import importlib.util
import time
from unittest.mock import MagicMock, call
import pytest

# ---------------------------------------------------------------------------
# All HA stubs are set up by conftest.py before this file is collected.
# ---------------------------------------------------------------------------

_HERE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "sems-wallbox",
)

# --------------------------------------------------------------------------
# Load number.py under its own isolated package namespace
# --------------------------------------------------------------------------
_pkg_name = "sems_wallbox_pkg_number"

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
        self._listeners: list = []
        self._refresh_requested = False

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def async_set_updated_data(self, new_data):
        self.data = new_data
        for listener in list(self._listeners):
            listener()

    def async_request_refresh(self):
        self._refresh_requested = True

    def schedule_delayed_refresh(self, delay=5):
        pass


_coord_stub.SemsUpdateCoordinator = _FakeCoordinator
sys.modules[f"{_pkg_name}.coordinator"] = _coord_stub
setattr(_pkg, "coordinator", _coord_stub)

_spec = importlib.util.spec_from_file_location(
    f"{_pkg_name}.number", os.path.join(_HERE, "number.py")
)
_number_mod = importlib.util.module_from_spec(_spec)
_number_mod.__package__ = _pkg_name
sys.modules[f"{_pkg_name}.number"] = _number_mod
_spec.loader.exec_module(_number_mod)

SemsNumber = _number_mod.SemsNumber

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


def _make_entity(
    chargeMode=0,
    set_charge_power=7.4,
    min_charge_power=4.2,
    max_charge_power=11.0,
):
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

    entity = SemsNumber(coordinator, SAMPLE_SN, api, set_charge_power)

    hass = MagicMock()
    hass.async_create_task = MagicMock()

    async def fake_executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = fake_executor
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    return entity


# ---------------------------------------------------------------------------
# Tests: initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_initial_native_value(self):
        entity = _make_entity(set_charge_power=6.5)
        assert entity._attr_native_value == 6.5

    def test_initial_native_value_none_when_data_none(self):
        entity = _make_entity(set_charge_power=None)
        assert entity._attr_native_value is None

    def test_unique_id(self):
        entity = _make_entity()
        assert entity.unique_id == f"{SAMPLE_SN}_number_set_charge_power"

    def test_translation_key(self):
        entity = _make_entity()
        assert entity._attr_translation_key == "charge_power"


# ---------------------------------------------------------------------------
# Tests: availability
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_available_in_fast_mode(self):
        entity = _make_entity(chargeMode=0)
        assert entity.available is True

    def test_unavailable_in_pv_priority(self):
        entity = _make_entity(chargeMode=1)
        assert entity.available is False

    def test_unavailable_in_pv_and_battery(self):
        entity = _make_entity(chargeMode=2)
        assert entity.available is False

    def test_unavailable_when_coordinator_failed(self):
        entity = _make_entity(chargeMode=0)
        entity.coordinator.last_update_success = False
        assert entity.available is False


# ---------------------------------------------------------------------------
# Tests: native_min_value / native_max_value
# ---------------------------------------------------------------------------

class TestMinMax:
    def test_min_from_api_data(self):
        entity = _make_entity(min_charge_power=3.0)
        assert entity.native_min_value == 3.0

    def test_max_from_api_data(self):
        entity = _make_entity(max_charge_power=22.0)
        assert entity.native_max_value == 22.0

    def test_min_fallback_to_default_when_none(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["min_charge_power"] = None
        assert entity.native_min_value == _number_mod.SemsNumber._DEFAULT_MIN

    def test_max_fallback_to_default_when_none(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["max_charge_power"] = None
        assert entity.native_max_value == _number_mod.SemsNumber._DEFAULT_MAX

    def test_min_fallback_on_invalid_string(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["min_charge_power"] = "bad"
        assert entity.native_min_value == _number_mod.SemsNumber._DEFAULT_MIN

    def test_max_fallback_on_invalid_string(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["max_charge_power"] = "bad"
        assert entity.native_max_value == _number_mod.SemsNumber._DEFAULT_MAX


# ---------------------------------------------------------------------------
# Tests: async_set_native_value (slider interaction)
# ---------------------------------------------------------------------------

class TestSetNativeValue:
    @pytest.mark.asyncio
    async def test_slider_sends_fast_mode_with_value(self):
        """Moving the slider must always send set_charge_mode(sn, 0, value).

        Sending mode=0 (Fast) together with the new power prevents a race
        condition where an in-flight mode-switch call (which also sends
        charge_power) could overwrite the slider value at the API side.
        By always including mode=0, the last write wins regardless of call
        ordering.
        """
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        await entity.async_set_native_value(9.0)
        entity.api.set_charge_mode.assert_called_once_with(SAMPLE_SN, 0, 9.0)

    @pytest.mark.asyncio
    async def test_slider_updates_coordinator_data_before_api_call(self):
        """async_set_native_value must write set_charge_power into coordinator.data
        before awaiting the API, so that an in-flight select.py mode-switch call
        can detect the change and re-send with the correct power."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)

        data_at_api_call_time: list[float] = []

        def capture_api(sn, mode, value):
            # Read coordinator.data at the moment the API is called
            data_at_api_call_time.append(
                entity.coordinator.data[SAMPLE_SN].get("set_charge_power")
            )
            return True

        entity.api.set_charge_mode = capture_api

        await entity.async_set_native_value(9.0)

        # coordinator.data must already hold 9.0 when the API was called
        assert data_at_api_call_time == [9.0]
        # And still correct after the call
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 9.0

    @pytest.mark.asyncio
    async def test_slider_optimistic_update_before_api(self):
        """native_value must be updated optimistically before the API call."""
        call_order: list[str] = []

        entity = _make_entity(chargeMode=0)
        original_write_state = entity.async_write_ha_state

        def capture_write():
            call_order.append(("write_ha_state", entity._attr_native_value))
            original_write_state()

        entity.async_write_ha_state = capture_write

        original_set_charge_mode = entity.api.set_charge_mode

        def capture_api(sn, mode, value):
            call_order.append(("api_call", value))
            return original_set_charge_mode(sn, mode, value)

        entity.api.set_charge_mode = capture_api

        await entity.async_set_native_value(9.0)

        # write_ha_state must have been called with 9.0 BEFORE the API call
        assert call_order[0] == ("write_ha_state", 9.0)
        assert call_order[1] == ("api_call", 9.0)

    @pytest.mark.asyncio
    async def test_slider_schedules_refresh_after_api(self):
        """schedule_delayed_refresh must be called to schedule a coordinator refresh."""
        entity = _make_entity(chargeMode=0)
        entity.coordinator.schedule_delayed_refresh = MagicMock()
        await entity.async_set_native_value(9.0)
        entity.coordinator.schedule_delayed_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_slider_sends_mode_0_not_mode_1(self):
        """Slider must always use mode=0, never another mode value."""
        entity = _make_entity(chargeMode=0)
        await entity.async_set_native_value(5.5)
        _, mode_arg, _ = entity.api.set_charge_mode.call_args[0]
        assert mode_arg == 0

    @pytest.mark.asyncio
    async def test_slider_passes_exact_value_to_api(self):
        """The value passed to the API must equal the slider value."""
        entity = _make_entity(chargeMode=0)
        await entity.async_set_native_value(10.3)
        _, _, power_arg = entity.api.set_charge_mode.call_args[0]
        assert power_arg == 10.3

    @pytest.mark.asyncio
    async def test_slider_reverts_value_on_api_failure(self):
        """If set_charge_mode returns False the slider must revert to the value
        currently in coordinator.data and async_write_ha_state must be called
        so the UI reflects the revert immediately.  HomeAssistantError is raised
        so HA shows a toast notification to the user."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        entity.api.set_charge_mode = MagicMock(return_value=False)
        with pytest.raises(Exception):  # HomeAssistantError
            await entity.async_set_native_value(9.0)
        assert entity._attr_native_value == 7.4
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 7.4
        # write_ha_state must be called to push the reverted value to the UI
        entity.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_slider_still_schedules_refresh_on_api_failure(self):
        """A coordinator refresh must be scheduled even when the API call fails,
        so the UI reconciles with the actual device state."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        entity.api.set_charge_mode = MagicMock(return_value=False)
        with pytest.raises(Exception):  # HomeAssistantError
            await entity.async_set_native_value(9.0)
        entity.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_slider_does_not_revert_when_pv_mode_during_timeout(self):
        """If an API call times out while the mode has already switched to PV
        (entity becomes unavailable), the revert must NOT overwrite the preserved
        PV power value.  The user set 11 kW; we must remember that for the next
        switch back to Fast — not silently revert to the old 7.4 kW.

        Real-world scenario (from log):
          15:27:30 Slider moved to 11 kW → API call starts (30 s timeout)
          15:27:33 PV mode selected → confirmed by poll, slider hidden (unavailable)
          15:28:00 Set-Fast-11 API call finally times out → revert must be skipped
        """
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)

        def side_effect(sn, mode, power):
            # Simulate: while waiting for the API, PV mode was confirmed by poll →
            # coordinator.data now shows chargeMode=1, slider is unavailable.
            entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
            return False  # timeout

        entity.api.set_charge_mode = side_effect

        with pytest.raises(Exception):  # HomeAssistantError
            await entity.async_set_native_value(11.0)

        # native_value must stay at 11.0 (not reverted to 7.4)
        assert entity._attr_native_value == 11.0
        # coordinator.data must also keep 11.0 so select.py uses it on next Fast switch
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 11.0


# ---------------------------------------------------------------------------
# Tests: _handle_coordinator_update
# ---------------------------------------------------------------------------

class TestCoordinatorUpdate:
    def test_update_sets_native_value(self):
        entity = _make_entity(set_charge_power=7.4)
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 9.0
        entity._handle_coordinator_update()
        assert entity._attr_native_value == 9.0

    def test_update_ignores_none_charge_power(self):
        entity = _make_entity(set_charge_power=7.4)
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = None
        entity._handle_coordinator_update()
        # Native value must remain unchanged when API returns None
        assert entity._attr_native_value == 7.4

    def test_update_calls_write_ha_state(self):
        entity = _make_entity(set_charge_power=7.4)
        entity._handle_coordinator_update()
        entity.async_write_ha_state.assert_called()

    def test_update_availability_reflects_charge_mode(self):
        entity = _make_entity(chargeMode=0)
        assert entity.available is True
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity._handle_coordinator_update()
        assert entity.available is False

    def test_pv_mode_does_not_overwrite_native_value_with_stale_api_value(self):
        """In PV mode the API may return a stale/default set_charge_power.
        The entity must keep the last user-set value so switching back to Fast
        restores it correctly."""
        entity = _make_entity(chargeMode=0, set_charge_power=11.0)
        # Simulate switch to PV mode: chargeMode changes, API returns old power
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 5.6
        entity._handle_coordinator_update()
        assert entity._attr_native_value == 11.0

    def test_pv_mode_preserves_value_in_coordinator_data_for_select(self):
        """In PV mode, the locally-held value must also be written back into
        coordinator.data so that select.py reads the right power when the user
        switches back to Fast."""
        entity = _make_entity(chargeMode=0, set_charge_power=11.0)
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 5.6
        entity._handle_coordinator_update()
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 11.0
