"""Unit tests for select.py -- InverterOperationModeEntity."""

import sys
import os
import types
import importlib.util
import time
from unittest.mock import MagicMock
import pytest

# ---------------------------------------------------------------------------
# All HA stubs are set up by conftest.py before this file is collected.
# ---------------------------------------------------------------------------

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "sems_wallbox")

# --------------------------------------------------------------------------
# Load select.py under its own package namespace
# --------------------------------------------------------------------------
_pkg_name = "sems_wallbox_pkg_select"

_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [_HERE]
_pkg.__package__ = _pkg_name
sys.modules[_pkg_name] = _pkg

_const = types.ModuleType(f"{_pkg_name}.const")
_const.DOMAIN = "sems_wallbox"
_const.CONN_TYPE_MODBUS = "modbus"
sys.modules[f"{_pkg_name}.const"] = _const
setattr(_pkg, "const", _const)

_coord_stub = types.ModuleType(f"{_pkg_name}.coordinator")


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True
        self._set_updated_data_calls = []

    def async_set_updated_data(self, new_data):
        self.data = new_data
        self._set_updated_data_calls.append(new_data)

    def async_request_refresh(self):
        pass

    def schedule_delayed_refresh(self, delay=5):
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
    api.set_charge_mode_gen2 = MagicMock()

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
    def test_mode_mapping_contract(self):
        expected = {"fast": 0, "pv_priority": 1, "pv_and_battery": 2}
        assert _select_mod._OPTION_TO_MODE == expected
        assert _select_mod._MODE_TO_OPTION == {
            mode: option for option, mode in expected.items()
        }


# ---------------------------------------------------------------------------
# Tests: API call behaviour in async_select_option
# ---------------------------------------------------------------------------

class TestSelectOption:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requested", "expected"), [(6.0, 6.0), (None, 4.2), (1.0, 4.2)]
    )
    async def test_switch_to_fast_uses_valid_power_and_publishes_it(
        self, requested, expected
    ):
        """The cloud command and shared state use the same validated limit."""
        entity = _make_entity(
            chargeMode=1, set_charge_power=requested,
            min_charge_power=4.2, max_charge_power=11.0,
        )
        await entity.async_select_option("fast")
        entity.api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, 0, expected
        )
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == expected


    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("option", "mode", "power"),
        [("fast", 0, 7.4), ("pv_priority", 1, None), ("pv_and_battery", 2, None)],
    )
    async def test_selection_publishes_mode_and_sends_cloud_command(
        self, option, mode, power
    ):
        """Selecting a mode updates dependent entities without waiting for a poll."""
        entity = _make_entity(chargeMode=1 if mode == 0 else 0, set_charge_power=7.4)
        await entity.async_select_option(option)
        entity.api.set_charge_mode_gen2.assert_called_once_with(SAMPLE_SN, mode, power)
        assert entity._attr_current_option == option
        entity.async_write_ha_state.assert_called()
        assert entity.coordinator.data[SAMPLE_SN]["chargeMode"] == mode
        assert len(entity.coordinator._set_updated_data_calls) == 1


    @pytest.mark.asyncio
    async def test_unknown_option_is_ignored(self):
        """An unknown option string must not call the API."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("invalid_option")
        entity.api.set_charge_mode_gen2.assert_not_called()


    @pytest.mark.asyncio
    async def test_switch_to_fast_resends_if_power_changed_during_api_call(self):
        """If number.py updates set_charge_power in coordinator.data while the
        mode-switch API call is in flight (slider moved by user), select must
        re-fire set_charge_mode_gen2 with the new power so the last write wins."""
        entity = _make_entity(chargeMode=1, set_charge_power=6.0)

        calls = []

        def side_effect(sn, mode, power):
            calls.append((sn, mode, power))
            # Simulate number.py's optimistic write during the first API call
            if len(calls) == 1:
                entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 11.0
            return True

        entity.api.set_charge_mode_gen2 = side_effect

        await entity.async_select_option("fast")

        assert len(calls) == 2
        assert calls[0] == (SAMPLE_SN, 0, 6.0)
        assert calls[1] == (SAMPLE_SN, 0, 11.0)

    @pytest.mark.asyncio
    async def test_switch_to_fast_no_resend_if_power_unchanged_during_api_call(self):
        """If set_charge_power did not change during the API call, no re-fire."""
        entity = _make_entity(chargeMode=1, set_charge_power=6.0)
        await entity.async_select_option("fast")
        entity.api.set_charge_mode_gen2.assert_called_once_with(SAMPLE_SN, 0, 6.0)

    @pytest.mark.asyncio
    async def test_superseded_fast_call_does_not_refires_when_pv_pending(self):
        """If the user switches Fast then immediately PV, the Fast call's result
        must be discarded (no re-fire, no refresh) because _pending_mode is now 1."""
        entity = _make_entity(chargeMode=1, set_charge_power=6.0)

        calls = []

        def side_effect(sn, mode, power):
            calls.append((sn, mode, power))
            # Simulate: slider moved to 11 AND user then clicked PV
            # while this Fast API call was in flight.
            if len(calls) == 1:
                entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 11.0
                # Newer PV dispatch overwrites _pending_mode
                entity._pending_mode = 1
            return True

        entity.api.set_charge_mode_gen2 = side_effect

        await entity.async_select_option("fast")

        # Only the original Fast call -- no re-fire because _pending_mode=1 != mode=0
        assert len(calls) == 1
        assert calls[0] == (SAMPLE_SN, 0, 6.0)
        # No refresh scheduled either
        entity.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_superseded_fast_call_discarded_when_pv_already_confirmed_by_poll(self):
        """The superseded guard must also fire when _pending_mode was already
        cleared by a poll confirming PV *before* a timed-out Fast call returned.

        Real-world scenario (from log):
          14:55:33 Fast call starts (30 s timeout)
          14:55:49 PV call starts, _pending_mode=1
          14:56:00 Poll confirms PV → _pending_mode cleared to None
          14:56:03 Fast call FINALLY returns after timeout
                   _pending_mode is None → old guard didn't fire
                   coordinator.data["chargeMode"] = 1 → new guard fires ✓
        """
        entity = _make_entity(chargeMode=1, set_charge_power=6.0)

        calls = []

        def side_effect(sn, mode, power):
            calls.append((sn, mode, power))
            if len(calls) == 1:
                # Simulate: PV dispatch ran AND poll confirmed it while we waited.
                # _pending_mode is now None (poll cleared it), but chargeMode=1.
                entity._pending_mode = None
                entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
                entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 11.0
            return True

        entity.api.set_charge_mode_gen2 = side_effect

        await entity.async_select_option("fast")

        # Only the original Fast call -- no re-fire because chargeMode is now 1
        assert len(calls) == 1
        entity.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_mode_switch_reverts_on_api_failure(self):
        """If set_charge_mode_gen2 returns False the select entity must revert
        _attr_current_option to whatever coordinator.data currently holds,
        clear _pending_mode, and schedule a coordinator refresh.
        HomeAssistantError is raised so HA shows a toast notification."""
        entity = _make_entity(chargeMode=0, set_charge_power=6.0)  # currently Fast
        entity.api.set_charge_mode_gen2 = MagicMock(return_value=False)
        with pytest.raises(Exception):  # HomeAssistantError
            await entity.async_select_option("pv_priority")
        # _attr_current_option must be reverted to "fast" (chargeMode=0 in coordinator)
        assert entity._attr_current_option == "fast"
        # _pending_mode must be cleared so poll-based guard works correctly
        assert entity._pending_mode is None
        # A refresh must be scheduled so the UI catches up with the real device
        entity.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_mode_switch_revert_calls_write_ha_state_on_failure(self):
        """async_write_ha_state must be called after reverting so the UI
        reflects the correct option without waiting for the next poll."""
        entity = _make_entity(chargeMode=0, set_charge_power=6.0)
        entity.api.set_charge_mode_gen2 = MagicMock(return_value=False)
        with pytest.raises(Exception):  # HomeAssistantError
            await entity.async_select_option("pv_priority")
        entity.async_write_ha_state.assert_called()


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


# ---------------------------------------------------------------------------
# Tests: pending mode grace period (blink prevention)
# ---------------------------------------------------------------------------

class TestPendingMode:
    @pytest.mark.asyncio
    async def test_pending_mode_set_after_select(self):
        """_pending_mode is set after async_select_option."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("pv_priority")
        assert entity._pending_mode == 1

    @pytest.mark.asyncio
    async def test_poll_with_old_mode_does_not_revert_state(self):
        """When a poll returns the old chargeMode during a pending transition,
        the select entity must not revert its current_option."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("pv_priority")  # _pending_mode = 1

        # Simulate a regular poll that still returns old chargeMode=0
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 0
        entity.coordinator._set_updated_data_calls.clear()
        entity._handle_coordinator_update()

        # Entity must still show pv_priority (not reverted to fast)
        assert entity._attr_current_option == "pv_priority"

    @pytest.mark.asyncio
    async def test_poll_with_old_mode_restores_coordinator_data(self):
        """When the poll returns the old mode, coordinator.data must be patched
        back to the pending chargeMode so other entities (e.g. number) also
        see the correct state."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("pv_priority")  # _pending_mode = 1

        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 0
        entity.coordinator._set_updated_data_calls.clear()
        entity._handle_coordinator_update()

        # coordinator.data must be restored to chargeMode=1
        assert entity.coordinator.data[SAMPLE_SN]["chargeMode"] == 1
        # async_set_updated_data must have been called to notify other entities
        assert len(entity.coordinator._set_updated_data_calls) == 1

    @pytest.mark.asyncio
    async def test_poll_confirming_pending_mode_clears_pending(self):
        """When the poll returns the expected chargeMode, _pending_mode is cleared."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("pv_priority")  # _pending_mode = 1

        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity._handle_coordinator_update()

        assert entity._pending_mode is None
        assert entity._attr_current_option == "pv_priority"

    @pytest.mark.asyncio
    async def test_pending_mode_timeout_clears_pending(self):
        """When _pending_mode_set_at is long in the past, the grace period expires
        and the next poll result is accepted normally."""
        entity = _make_entity(chargeMode=0)
        await entity.async_select_option("pv_priority")  # _pending_mode = 1

        # Backdate the timestamp so timeout logic fires
        entity._pending_mode_set_at = time.monotonic() - 100.0  # 100s in the past

        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 0
        entity._handle_coordinator_update()

        # Grace period expired -- pending cleared, option updated from poll data
        assert entity._pending_mode is None
        assert entity._attr_current_option == "fast"

    def test_restoring_flag_prevents_reentrant_processing(self):
        """When _restoring=True, _handle_coordinator_update returns immediately."""
        entity = _make_entity(chargeMode=0)
        entity._restoring = True
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity._handle_coordinator_update()
        # async_write_ha_state must NOT have been called (early return)
        entity.async_write_ha_state.assert_not_called()


# ---------------------------------------------------------------------------
# SemsChargeDurationSelect tests
# ---------------------------------------------------------------------------

SemsChargeDurationSelect = _select_mod.SemsChargeDurationSelect


def _make_duration_entity(chargeMode=1, finish_time="0", **extra_data):
    data = {
        "sn": SAMPLE_SN,
        "chargeMode": chargeMode,
        "set_charge_power": 7.4,
        "max_energy": 20,
        "min_energy": 5,
        "charge_target_soc": 20,
        "finish_time": finish_time,
        "name": "My Wallbox",
        **extra_data,
    }
    coordinator = _FakeCoordinator({SAMPLE_SN: data})
    api = MagicMock()
    api.set_charge_mode_gen2 = MagicMock(return_value=True)
    entity = SemsChargeDurationSelect(coordinator, SAMPLE_SN, api)
    hass = MagicMock()

    async def fake_executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = fake_executor
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    return entity


@pytest.mark.parametrize("option,hours", [
    ("asap", "0"), ("1h", "1"), ("2h", "2"), ("3h", "3"),
    ("4h", "4"), ("5h", "5"), ("6h", "6"),
])
@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.asyncio
async def test_duration_read_and_write_contract(option, hours, mode):
    """Match each public choice to an independently specified API value."""
    entity = _make_duration_entity(chargeMode=mode, finish_time=hours)
    assert entity.current_option == option
    await entity.async_select_option(option)
    expected = {"max_energy": 20, "min_energy": 5, "finish_time": hours}
    if mode == 2:
        expected["soc_target"] = 20
    entity.api.set_charge_mode_gen2.assert_called_once_with(
        SAMPLE_SN, mode, None, None, **expected,
    )


class TestSemsChargeDurationSelectProperties:
    def test_unique_id(self):
        entity = _make_duration_entity()
        assert entity.unique_id == f"{SAMPLE_SN}-select-charge-duration"

    def test_available_in_pv_priority(self):
        entity = _make_duration_entity(chargeMode=1)
        assert entity.available is True

    def test_available_in_pv_and_battery(self):
        entity = _make_duration_entity(chargeMode=2)
        assert entity.available is True

    def test_not_available_in_fast_mode(self):
        entity = _make_duration_entity(chargeMode=0)
        assert entity.available is False

    def test_not_available_when_coordinator_failed(self):
        entity = _make_duration_entity(chargeMode=1)
        entity.coordinator.last_update_success = False
        assert entity.available is False


    def test_current_option_none_when_finish_time_missing(self):
        entity = _make_duration_entity()
        entity.coordinator.data[SAMPLE_SN].pop("finish_time")
        assert entity.current_option is None

    def test_options_list(self):
        entity = _make_duration_entity()
        assert entity._attr_options == ["asap", "1h", "2h", "3h", "4h", "5h", "6h"]


class TestSemsChargeDurationSelectOption:


    @pytest.mark.asyncio
    async def test_pending_value_set_optimistically(self):
        entity = _make_duration_entity(chargeMode=1, finish_time="0")
        await entity.async_select_option("4h")
        assert entity._pending_value == "4h"

    @pytest.mark.asyncio
    async def test_pending_value_cleared_on_api_failure(self):
        entity = _make_duration_entity(chargeMode=1)
        entity.api.set_charge_mode_gen2 = MagicMock(return_value=False)
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("2h")
        assert entity._pending_value is None

    @pytest.mark.asyncio
    async def test_unknown_option_ignored(self):
        entity = _make_duration_entity(chargeMode=1)
        await entity.async_select_option("999h")
        entity.api.set_charge_mode_gen2.assert_not_called()

    @pytest.mark.asyncio
    async def test_coordinator_refresh_scheduled_on_success(self):
        entity = _make_duration_entity(chargeMode=1)
        entity.coordinator.schedule_delayed_refresh = MagicMock()
        await entity.async_select_option("1h")
        entity.coordinator.schedule_delayed_refresh.assert_called_once()

    def test_current_option_returns_pending_while_waiting(self):
        entity = _make_duration_entity(chargeMode=1, finish_time="0")
        entity._pending_value = "3h"
        entity._pending_until = time.monotonic() + 30.0
        assert entity.current_option == "3h"

    def test_pending_cleared_when_poll_confirms(self):
        entity = _make_duration_entity(chargeMode=1, finish_time="3")
        entity._pending_value = "3h"
        entity._pending_until = time.monotonic() + 30.0
        result = entity.current_option
        assert entity._pending_value is None
        assert result == "3h"

    def test_pending_cleared_on_timeout(self):
        entity = _make_duration_entity(chargeMode=1, finish_time="0")
        entity._pending_value = "3h"
        entity._pending_until = time.monotonic() - 1.0
        result = entity.current_option
        assert entity._pending_value is None
        assert result == "asap"


async def test_duration_does_not_zero_an_unknown_energy_target():
    entity = _make_duration_entity(chargeMode=2, min_energy=None)
    from homeassistant.exceptions import HomeAssistantError
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("2h")
    entity.api.set_charge_mode_gen2.assert_not_called()
