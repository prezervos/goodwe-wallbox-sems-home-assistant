"""Unit tests for number.py -- SemsNumber charge-power slider entity."""

import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock
import pytest
from homeassistant.exceptions import HomeAssistantError

# ---------------------------------------------------------------------------
# All HA stubs are set up by conftest.py before this file is collected.
# ---------------------------------------------------------------------------

_HERE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "sems_wallbox",
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
_const.DOMAIN = "sems_wallbox"
_const.CONN_TYPE_MODBUS = "modbus"
_const.CAP_OUTPUT_POWER_SETTING = "Output_Power_Setting"
_const.CAP_DYNAMIC_LOAD_CONTROL = "Dynamic_Load_Control"
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
SemsMaxEnergyNumber = _number_mod.SemsMaxEnergyNumber
SemsTargetSocNumber = _number_mod.SemsTargetSocNumber
SemsMinEnergyNumber = _number_mod.SemsMinEnergyNumber
SemsOutputPowerLimitNumber = _number_mod.SemsOutputPowerLimitNumber

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
    api.set_charge_mode_gen2 = MagicMock()

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
        assert entity.native_value == 6.5

    def test_initial_native_value_none_when_data_none(self):
        entity = _make_entity(set_charge_power=None)
        assert entity.native_value is None


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
        assert entity.native_min_value == entity._model_limits()[0]

    def test_max_fallback_to_default_when_none(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["max_charge_power"] = None
        assert entity.native_max_value == entity._model_limits()[1]

    def test_min_fallback_on_invalid_string(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["min_charge_power"] = "bad"
        assert entity.native_min_value == entity._model_limits()[0]

    def test_max_fallback_on_invalid_string(self):
        entity = _make_entity()
        entity.coordinator.data[SAMPLE_SN]["max_charge_power"] = "bad"
        assert entity.native_max_value == entity._model_limits()[1]


# ---------------------------------------------------------------------------
# Tests: async_set_native_value (slider interaction)
# ---------------------------------------------------------------------------

class TestSetNativeValue:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("power", [5.5, 9.0, 10.3])
    async def test_slider_sends_exact_fast_power_and_schedules_refresh(self, power):
        """Legacy cloud writes include Fast mode and preserve the requested power."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        entity.coordinator.schedule_delayed_refresh = MagicMock()
        await entity.async_set_native_value(power)
        entity.api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, 0, power, None
        )
        entity.coordinator.schedule_delayed_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_slider_publishes_optimistic_state_before_api(self):
        """Both UI state and shared power intent must precede the cloud write."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        observations = []

        def capture_api(sn, mode, value, ensure_min=None):
            entity.async_write_ha_state.assert_called()
            observations.append((
                entity.native_value,
                entity.coordinator.data[SAMPLE_SN]["set_charge_power"],
            ))
            return True

        entity.api.set_charge_mode_gen2 = capture_api
        await entity.async_set_native_value(9.0)
        assert observations == [(9.0, 9.0)]
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 9.0


    @pytest.mark.asyncio
    async def test_slider_failure_reverts_state_and_requests_reconciliation(self):
        """A rejected write restores UI/shared state and schedules a fresh read."""
        entity = _make_entity(chargeMode=0, set_charge_power=7.4)
        entity.api.set_charge_mode_gen2 = MagicMock(return_value=False)
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(9.0)
        assert entity.native_value == 7.4
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 7.4
        entity.async_write_ha_state.assert_called()
        entity.hass.async_create_task.assert_called_once()


    @pytest.mark.asyncio
    async def test_slider_from_pv_mode_switches_to_fast(self):
        """Moving the slider from PV mode should switch to Fast mode (0)."""
        entity = _make_entity(chargeMode=2, set_charge_power=5.6)

        entity.api.set_charge_mode_gen2 = MagicMock(return_value=True)
        await entity.async_set_native_value(9.0)

        entity.api.set_charge_mode_gen2.assert_called_once_with(SAMPLE_SN, 0, 9.0, None)
        assert entity.native_value == 9.0


# ---------------------------------------------------------------------------
# Tests: _handle_coordinator_update
# ---------------------------------------------------------------------------

class TestCoordinatorUpdate:
    def test_update_sets_native_value(self):
        entity = _make_entity(set_charge_power=7.4)
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 9.0
        entity._handle_coordinator_update()
        assert entity.native_value == 9.0

    def test_update_ignores_none_charge_power(self):
        entity = _make_entity(set_charge_power=7.4)
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = None
        entity._handle_coordinator_update()
        # Native value must remain unchanged when API returns None
        assert entity.native_value == 7.4

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

    def test_pv_mode_shows_api_allocated_power(self):
        """In PV mode the entity shows the dynamically allocated power from the API."""
        entity = _make_entity(chargeMode=0, set_charge_power=11.0)
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 2
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 5.6
        entity._handle_coordinator_update()
        assert entity.native_value == 5.6

    def test_pv_mode_coordinator_data_reflects_api(self):
        """coordinator.data set_charge_power is the API value in PV mode (not patched back)."""
        entity = _make_entity(chargeMode=0, set_charge_power=11.0)
        entity.coordinator.data[SAMPLE_SN]["chargeMode"] = 1
        entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 5.6
        entity._handle_coordinator_update()
        assert entity.coordinator.data[SAMPLE_SN]["set_charge_power"] == 5.6


# ---------------------------------------------------------------------------
# Helpers for mode-param entities
# ---------------------------------------------------------------------------

_SAMPLE_MODE_DATA = {
    "sn": SAMPLE_SN,
    "chargeMode": 0,
    "set_charge_power": 7.4,
    "max_energy": 0,
    "min_energy": 0,
    "charge_target_soc": 0,
    "finish_time": "0",
    "name": "My Wallbox",
}


def _make_hass():
    hass = MagicMock()
    hass.async_create_task = MagicMock()

    async def fake_executor(func):
        return func()

    hass.async_add_executor_job = fake_executor
    return hass


def _make_mode_entity(entity_cls, chargeMode=0, max_energy=0, min_energy=0, soc=0, charge_power=7.4):
    data = {
        **_SAMPLE_MODE_DATA,
        "chargeMode": chargeMode,
        "set_charge_power": charge_power,
        "max_energy": max_energy,
        "min_energy": min_energy,
        "charge_target_soc": soc,
    }
    coordinator = _FakeCoordinator({SAMPLE_SN: data})
    coordinator.schedule_delayed_refresh = MagicMock()
    api = MagicMock()
    api.set_charge_mode_gen2 = MagicMock(return_value=True)
    entity = entity_cls(coordinator, SAMPLE_SN, api)
    entity.hass = _make_hass()
    entity.async_write_ha_state = MagicMock()
    return entity


# ---------------------------------------------------------------------------
# Tests: SemsMaxEnergyNumber
# ---------------------------------------------------------------------------

class TestSemsMaxEnergyNumber:


    def test_available_in_fast_mode(self):
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=0)
        assert e.available is True

    def test_available_in_pvbat_mode(self):
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=2)
        assert e.available is True

    def test_available_in_pv_mode(self):
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=1)
        assert e.available is True

    def test_native_value_from_data(self):
        e = _make_mode_entity(SemsMaxEnergyNumber, max_energy=80)
        assert e.native_value == 80.0

    @pytest.mark.asyncio
    async def test_set_in_fast_mode_sends_mode0(self):
        """Setting max energy in Fast mode sends set_charge_mode_gen2 with mode=0."""
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=0, charge_power=7.4, max_energy=0, soc=45)
        await e.async_set_native_value(50.0)
        e.api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, 0, 7.4, None,
            max_energy=50, soc_target=45
        )

    @pytest.mark.asyncio
    async def test_set_in_pvbat_mode_sends_mode2_no_charge_power(self):
        """Setting max energy in PV+BAT mode sends mode=2 without chargeMaxPower."""
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=2, charge_power=5.0, max_energy=0, min_energy=10, soc=30)
        await e.async_set_native_value(60.0)
        e.api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, 2, None, None,
            max_energy=60, min_energy=10, soc_target=30, finish_time="0"
        )

    @pytest.mark.asyncio
    async def test_set_schedules_refresh(self):
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=0)
        await e.async_set_native_value(50.0)
        e.coordinator.schedule_delayed_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_reverts_on_failure(self):
        e = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=0, max_energy=20)
        e.api.set_charge_mode_gen2 = MagicMock(return_value=False)
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await e.async_set_native_value(50.0)
        # pending_value should be cleared after failure
        assert e._pending_value is None


# ---------------------------------------------------------------------------
# Tests: SemsTargetSocNumber
# ---------------------------------------------------------------------------

class TestSemsTargetSocNumber:


    def test_available_in_fast_mode(self):
        assert _make_mode_entity(SemsTargetSocNumber, chargeMode=0).available is True

    def test_available_in_pvbat_mode(self):
        assert _make_mode_entity(SemsTargetSocNumber, chargeMode=2).available is True

    def test_unavailable_in_pv_mode(self):
        assert _make_mode_entity(SemsTargetSocNumber, chargeMode=1).available is False

    def test_native_value_from_data(self):
        e = _make_mode_entity(SemsTargetSocNumber, soc=45)
        assert e.native_value == 45.0

    @pytest.mark.asyncio
    async def test_set_overrides_soc_target(self):
        """Setting target SOC sends soc_target=new value, max_energy/min_energy preserved."""
        e = _make_mode_entity(SemsTargetSocNumber, chargeMode=0, charge_power=7.4, max_energy=80, soc=45)
        await e.async_set_native_value(60.0)
        e.api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, 0, 7.4, None,
            max_energy=80, soc_target=60
        )


# ---------------------------------------------------------------------------
# Tests: SemsMinEnergyNumber
# ---------------------------------------------------------------------------

class TestSemsMinEnergyNumber:


    def test_unavailable_in_fast_mode(self):
        assert _make_mode_entity(SemsMinEnergyNumber, chargeMode=0).available is False

    def test_available_in_pv_mode(self):
        assert _make_mode_entity(SemsMinEnergyNumber, chargeMode=1).available is True

    def test_available_in_pvbat_mode(self):
        assert _make_mode_entity(SemsMinEnergyNumber, chargeMode=2).available is True

    def test_native_value_from_data(self):
        e = _make_mode_entity(SemsMinEnergyNumber, min_energy=15)
        assert e.native_value == 15.0

    @pytest.mark.asyncio
    async def test_set_in_pvbat_mode_sends_mode2(self):
        """Setting min energy sends set_charge_mode_gen2 with mode=2 and min_energy overridden."""
        e = _make_mode_entity(SemsMinEnergyNumber, chargeMode=2, max_energy=50, min_energy=10, soc=30)
        await e.async_set_native_value(20.0)
        e.api.set_charge_mode_gen2.assert_called_once_with(
            SAMPLE_SN, 2, None, None,
            max_energy=50, min_energy=20, soc_target=30, finish_time="0"
        )


# ---------------------------------------------------------------------------
# Tests: SemsOutputPowerLimitNumber
# ---------------------------------------------------------------------------

def _make_power_limit_entity(rated_max=11.0, hw_max=None):
    data = {
        **_SAMPLE_MODE_DATA,
        "rated_max_charge_power": rated_max,
        "hw_max_charge_power": hw_max,
    }
    coordinator = _FakeCoordinator({SAMPLE_SN: data})
    coordinator.schedule_delayed_refresh = MagicMock()
    api = MagicMock()
    api.set_config_gen2 = MagicMock(return_value=True)
    entity = SemsOutputPowerLimitNumber(coordinator, SAMPLE_SN, api)
    entity.hass = _make_hass()
    entity.async_write_ha_state = MagicMock()
    return entity


class TestSemsOutputPowerLimitNumber:


    def test_native_value_from_data(self):
        e = _make_power_limit_entity(rated_max=7.0)
        assert e.native_value == 7.0

    def test_native_value_none_when_missing(self):
        e = _make_power_limit_entity()
        e.coordinator.data[SAMPLE_SN]["rated_max_charge_power"] = None
        assert e.native_value is None

    def test_always_available(self):
        e = _make_power_limit_entity()
        assert e.available is True

    def test_native_max_value_from_hw_max(self):
        e = _make_power_limit_entity(rated_max=7.0, hw_max=11.0)
        assert e.native_max_value == 11.0

    def test_native_max_value_from_max_charge_power_fallback(self):
        e = _make_power_limit_entity(rated_max=11.0)
        e.coordinator.data[SAMPLE_SN]["max_charge_power"] = 11.0
        assert e.native_max_value == 11.0

    def test_native_max_value_from_rated_max_fallback(self):
        e = _make_power_limit_entity(rated_max=11.0)
        assert e.native_max_value == 11.0

    def test_native_max_value_defaults_to_22_when_no_hw_data(self):
        e = _make_power_limit_entity(rated_max=None)
        assert e.native_max_value == 22.0

    def test_unavailable_when_coordinator_failed(self):
        e = _make_power_limit_entity()
        e.coordinator.last_update_success = False
        assert e.available is False

    @pytest.mark.asyncio
    async def test_set_calls_set_config_with_ratedMaxiChargePower(self):
        e = _make_power_limit_entity()
        await e.async_set_native_value(7.0)
        e.api.set_config_gen2.assert_called_once_with(e.sn, ratedMaxiChargePower=7.0)

    @pytest.mark.asyncio
    async def test_set_schedules_refresh(self):
        e = _make_power_limit_entity()
        await e.async_set_native_value(7.0)
        e.coordinator.schedule_delayed_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_reverts_pending_on_failure(self):
        e = _make_power_limit_entity()
        e.api.set_config_gen2 = MagicMock(return_value=False)
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await e.async_set_native_value(7.0)
        assert e._pending_value is None


@pytest.mark.parametrize("factory,unique_id,key", [
    (_make_entity, f"{SAMPLE_SN}_number_set_charge_power", "charge_power"),
    (lambda: _make_mode_entity(SemsMaxEnergyNumber), f"{SAMPLE_SN}-number-max-energy", "max_session_energy"),
    (lambda: _make_mode_entity(SemsTargetSocNumber), f"{SAMPLE_SN}-number-target-soc", "charge_target_soc"),
    (lambda: _make_mode_entity(SemsMinEnergyNumber), f"{SAMPLE_SN}-number-min-energy", "min_session_energy"),
    (_make_power_limit_entity, f"{SAMPLE_SN}-number-output-power-limit", "output_power_limit"),
])
def test_number_identity_contract(factory, unique_id, key):
    """Preserve registry identity and translation keys across refactoring."""
    entity = factory()
    assert entity.unique_id == unique_id
    assert entity._attr_translation_key == key


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("enabled,desired,expected", [
    (True, 4.2, 4.2), (False, 4.2, 5.6), (True, None, 5.6),
])
def test_public_power_separates_saved_intent_from_report(mode, enabled, desired, expected):
    """A report must not silently replace the active user's power preference."""
    from types import SimpleNamespace

    entity = _make_entity(chargeMode=mode, set_charge_power=11.0)
    entity.coordinator.charge_mode_policy = SimpleNamespace(
        enabled=enabled, desired_power=desired,
    )
    entity.coordinator.data[SAMPLE_SN]["set_charge_power"] = 5.6
    entity._handle_coordinator_update()
    assert entity.native_value == expected
    assert entity.extra_state_attributes["reported_power_limit"] == 5.6
    assert entity.coordinator.charge_mode_policy.desired_power == desired


@pytest.mark.parametrize("field", ["min_energy", "finish_time", "charge_target_soc"])
async def test_mode_target_does_not_overwrite_unreported_siblings(field):
    entity = _make_mode_entity(SemsMaxEnergyNumber, chargeMode=2)
    entity.coordinator.data[SAMPLE_SN][field] = None
    from homeassistant.exceptions import HomeAssistantError
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(20)
    entity.api.set_charge_mode_gen2.assert_not_called()
