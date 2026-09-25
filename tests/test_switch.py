"""Unit tests for switch.py -- SemsSwitch grace period logic."""

import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock
import time

import pytest

# ---------------------------------------------------------------------------
# All HA stubs are set up by conftest.py before this file is collected.
# ---------------------------------------------------------------------------

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "sems_wallbox")

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
_const.DOMAIN = "sems_wallbox"
_const.CONN_TYPE_MODBUS = "modbus"
_const.CAP_PLUG_AND_CHARGE = "plugAndCharge"
_const.CAP_DYNAMIC_LOAD_CONTROL = "Dynamic_Load_Control"
_const.CAP_PHASE_SWITCH = "Phase_Switch"
_const.CAP_ENSURE_MIN_CHARGING_POWER = "Ensure_minimum_Charging_Power"
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
    "startStatus": True,
}

STANDBY_DATA = {
    "sn": SAMPLE_SN,
    "status": "EVDetail_Status_Title_Waiting",
    "power": 0.0,
    "startStatus": False,
}

# Gen2 EU gateway equivalents
CHARGING_DATA_GEN2 = {
    "sn": SAMPLE_SN,
    "status": "charging",
    "power": 7.4,
    "startStatus": True,
}

STANDBY_DATA_GEN2 = {
    "sn": SAMPLE_SN,
    "status": "available",
    "power": 4.2,  # configured limit, not actual draw
    "startStatus": False,
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
    sw.async_write_ha_state = MagicMock()
    return sw


# ===========================================================================
# _compute_is_on_from_data -- no grace (no command issued)
# ===========================================================================

class TestComputeIsOnNoGrace:
    def test_charging_status_returns_true(self):
        sw = _make_switch(CHARGING_DATA)
        assert sw._compute_is_on_from_data(CHARGING_DATA) is True

    def test_standby_status_returns_false(self):
        sw = _make_switch(STANDBY_DATA)
        assert sw._compute_is_on_from_data(STANDBY_DATA) is False

    def test_gen2_charging_returns_true(self):
        sw = _make_switch(CHARGING_DATA_GEN2)
        assert sw._compute_is_on_from_data(CHARGING_DATA_GEN2) is True

    def test_gen2_standby_power_limit_returns_false(self):
        # power=4.2 is the configured limit, not actual draw -- must NOT be ON
        sw = _make_switch(STANDBY_DATA_GEN2)
        assert sw._compute_is_on_from_data(STANDBY_DATA_GEN2) is False

    def test_no_start_status_falls_back_to_old_api(self):
        # Gen1 data without startStatus -- use status string
        data = {"sn": SAMPLE_SN, "status": "EVDetail_Status_Title_Charging", "power": 7.4}
        sw = _make_switch(data)
        assert sw._compute_is_on_from_data(data) is True

    def test_no_start_status_standby_is_off(self):
        data = {"sn": SAMPLE_SN, "status": "EVDetail_Status_Title_Waiting", "power": 0.0}
        sw = _make_switch(data)
        assert sw._compute_is_on_from_data(data) is False


# ===========================================================================
# _compute_is_on_from_data -- within ON grace window
# ===========================================================================

class TestComputeIsOnGraceOn:
    def test_within_grace_api_standby_stays_true(self):
        """After ON command, even if API returns Waiting/power=0, we stay ON."""
        sw = _make_switch(STANDBY_DATA)
        now = time.monotonic()
        sw._last_command_target = True
        sw._last_command_ts = now - 5  # 5 s ago -- well within 130 s grace
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
# _compute_is_on_from_data -- within OFF grace window
# ===========================================================================

class TestComputeIsOnGraceOff:
    def test_within_grace_api_charging_stays_false(self):
        """After OFF command, even if API still reports Charging, we stay OFF."""
        sw = _make_switch(CHARGING_DATA)
        now = time.monotonic()
        sw._last_command_target = False
        sw._last_command_ts = now - 5  # 5 s ago -- well within 130 s grace
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
    def test_charging_switch_identity_contract(self):
        sw = _make_switch(CHARGING_DATA, current_is_on=True)
        assert sw.unique_id == f"{SAMPLE_SN}-switch-start-charging"
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
        assert ("sems_wallbox", SAMPLE_SN) in info["identifiers"]
        assert info["manufacturer"] == "GoodWe"


# Capability-gated controls must follow device declarations, not model guesses.


@pytest.mark.parametrize("dashboard,controls,expected", [
    ([], ["unrelated"], {"SemsSwitch"}),
    (["plugAndCharge"], ["unrelated"], {"SemsSwitch", "SemsPlugAndChargeSwitch"}),
    ([], ["Dynamic_Load_Control"], {"SemsSwitch", "SemsDynamicLoadSwitch"}),
    ([], ["Phase_Switch"], {"SemsSwitch", "SemsPhaseSwitchSwitch"}),
    ([], [], {"SemsSwitch", "SemsMinimumPowerSwitch"}),
])
async def test_cloud_switches_follow_declared_capabilities(dashboard, controls, expected):
    api = MagicMock()
    coordinator = _FakeCoordinator({SAMPLE_SN: STANDBY_DATA})
    hass = types.SimpleNamespace(data={"sems_wallbox": {"test": {
        "coordinator": coordinator, "connection_type": "cloud", "api": api,
        "capabilities": {"dashboard_functions": dashboard, "more_device_controls": controls},
    }}})
    entities = []
    unsubscribe = MagicMock()
    coordinator.async_add_listener = MagicMock(return_value=unsubscribe)
    entry = types.SimpleNamespace(entry_id="test", async_on_unload=MagicMock())
    await _switch_mod.async_setup_entry(hass, entry, entities.extend)
    coordinator.async_add_listener.assert_called_once()
    entry.async_on_unload.assert_called_once_with(unsubscribe)
    entry.async_on_unload.call_args.args[0]()
    unsubscribe.assert_called_once_with()
    assert {type(entity).__name__ for entity in entities} == expected
    assert api.mock_calls == [], "Platform setup must not enable charger features"


@pytest.mark.parametrize("enabled", [True, False])
async def test_rejected_command_clears_optimism_and_raises(enabled):
    from homeassistant.exceptions import HomeAssistantError
    observed = STANDBY_DATA if enabled else CHARGING_DATA
    entity = _make_switch(observed, not enabled)
    async def execute(function, *args):
        return function(*args)
    entity.hass.async_add_executor_job = execute
    entity.async_write_ha_state = MagicMock()
    entity.coordinator.schedule_delayed_refresh = MagicMock()
    entity.api.change_status_gen2.return_value = False
    with pytest.raises(HomeAssistantError) as failure:
        await (entity.async_turn_on() if enabled else entity.async_turn_off())
    assert failure.value.translation_key == ("start_unconfirmed" if enabled else "stop_unconfirmed")
    assert entity._last_command_target is None
    assert entity._attr_is_on is (not enabled)
    entity.api.change_status_gen2.assert_called_once_with(SAMPLE_SN, "start" if enabled else "stop")
    entity.coordinator.schedule_delayed_refresh.assert_called_once()


@pytest.mark.parametrize("class_name,field", [
    ("SemsDynamicLoadSwitch", "dynamicLoad"), ("SemsPhaseSwitchSwitch", "phaseSwitch"),
])
@pytest.mark.parametrize("reported", [None, False, True])
def test_configuration_switch_keeps_unknown_distinct_from_off(class_name, field, reported):
    entity = getattr(_switch_mod, class_name)(_FakeCoordinator({SAMPLE_SN: {field: reported}}), SAMPLE_SN, None)
    assert entity.is_on is reported


@pytest.mark.parametrize("enabled", [True, False])
async def test_policy_control_keeps_intent_until_reported(enabled):
    """A stale session after ACK must not bounce the accepted control."""
    from unittest.mock import AsyncMock
    observed = dict(STANDBY_DATA if enabled else CHARGING_DATA)
    entity = _make_switch(observed, not enabled)
    entity.async_write_ha_state = MagicMock()
    policy = types.SimpleNamespace(enabled=True, async_start=AsyncMock(), async_stop=AsyncMock())
    entity.coordinator.charge_mode_policy = policy
    await (entity.async_turn_on() if enabled else entity.async_turn_off())
    assert entity._attr_is_on is enabled
    assert entity._compute_is_on_from_data(observed) is enabled
    assert entity.coordinator.data[SAMPLE_SN] == observed
    entity.api.change_status_gen2.assert_not_called()
    matching = CHARGING_DATA if enabled else STANDBY_DATA
    assert entity._compute_is_on_from_data(matching) is enabled
    assert entity._last_command_target is None
    (policy.async_start if enabled else policy.async_stop).assert_awaited_once()


async def test_policy_older_start_completion_cannot_replace_stop():
    import asyncio
    from unittest.mock import AsyncMock
    entity = _make_switch(STANDBY_DATA)
    entity.async_write_ha_state = MagicMock()
    entered, release = asyncio.Event(), asyncio.Event()
    async def start():
        entered.set()
        await release.wait()
    entity.coordinator.charge_mode_policy = types.SimpleNamespace(
        enabled=True, async_start=start, async_stop=AsyncMock())
    older = asyncio.create_task(entity.async_turn_on())
    await entered.wait()
    await entity.async_turn_off()
    release.set()
    await older
    assert entity._attr_is_on is False
    assert entity._last_command_target is False


@pytest.mark.parametrize("error", [RuntimeError("rejected"), TimeoutError("uncertain")])
async def test_failed_policy_does_not_present_acknowledged_start(error):
    from unittest.mock import AsyncMock
    entity = _make_switch(STANDBY_DATA)
    entity.async_write_ha_state = MagicMock()
    entity.coordinator.charge_mode_policy = types.SimpleNamespace(
        enabled=True, async_start=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await entity.async_turn_on()
    assert entity._attr_is_on is False
    assert entity._last_command_target is None
    entity.api.change_status_gen2.assert_not_called()


@pytest.mark.parametrize("terminal", [4, 5, 8])
def test_modbus_accepted_policy_waits_for_new_terminal_report(terminal):
    data = {"modbus_status_raw": 1, "modbus_car_connected": 1}
    coordinator = _FakeCoordinator({SAMPLE_SN: data})
    entity = _switch_mod.ModbusStartStopSwitch(coordinator, SAMPLE_SN, MagicMock())
    entity._set_pending_command(True)
    assert entity.is_on is True  # Cached pre-Start idle is not a rejection.
    coordinator.data[SAMPLE_SN] = {"modbus_status_raw": terminal, "modbus_car_connected": 1}
    assert entity.is_on is False
    assert entity._pending_state is None


def test_modbus_unconfirmed_policy_intent_expires():
    coordinator = _FakeCoordinator({SAMPLE_SN: {"modbus_status_raw": 1, "modbus_car_connected": 1}})
    entity = _switch_mod.ModbusStartStopSwitch(coordinator, SAMPLE_SN, MagicMock())
    entity._set_pending_command(True)
    entity._pending_set_at -= _switch_mod._MODBUS_PENDING_TIMEOUT + 1
    assert entity.is_on is False
