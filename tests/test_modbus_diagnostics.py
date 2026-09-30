"""Modbus breaker validation and privacy-safe cached protocol diagnostics."""

import importlib
import json
from unittest.mock import Mock

import pytest

from tests.test_charge_mode_policy import PACKAGE

module = importlib.import_module(PACKAGE + ".wallbox_modbus")


@pytest.mark.parametrize("value", [0, 6, 32, 63, 63.0, 2000])
def test_household_breaker_current_uses_documented_range_without_clamping(value):
    client = module.WallboxModbusClient("private-host")
    client._write = Mock(return_value=True)
    assert client.write_breaker_current(value)
    client._write.assert_called_once_with(10026, int(value))


@pytest.mark.parametrize("value", [-1, 2001, 63.5, float("nan"), float("inf"), True, "63", None])
def test_invalid_breaker_input_never_connects_or_writes(value):
    client = module.WallboxModbusClient("private-host")
    client._make_client = Mock()
    with pytest.raises(ValueError, match="whole number"):
        client.write_breaker_current(value)
    client._make_client.assert_not_called()
    assert client.diagnostics()["write_requests"] == 0


def test_cached_diagnostics_are_bounded_detached_and_hide_register_payloads():
    serial = "PRIVATE-SERIAL"
    client = module.WallboxModbusClient("private-host", expected_serial=serial)
    wire = Mock()
    wire.read_holding_registers.return_value.isError.return_value = False
    wire.read_holding_registers.return_value.registers = [123, 456]
    for _ in range(80):
        assert client._read(wire, 10040, 2) == [123, 456]
    result = client.diagnostics()
    assert result["read_requests"] == 80 and result["write_requests"] == 0
    assert len(result["recent_events"]) == 128
    assert all(item["age_seconds"] >= 0 for item in result["recent_events"])
    assert all(set(item) <= {"age_seconds", "event", "function", "address", "count", "outcome"}
               for item in result["recent_events"])
    encoded = json.dumps(result)
    assert all(value not in encoded for value in (serial, "private-host", "registers"))
    result["recent_events"].clear()
    assert len(client.diagnostics()["recent_events"]) == 128
    assert wire.read_holding_registers.call_count == 80


@pytest.mark.parametrize("failure", ["error", "exception"])
def test_failed_write_is_traced_once_without_replay(failure):
    client = module.WallboxModbusClient("private-host")
    wire = Mock()
    client._make_client = Mock(return_value=wire)
    client._verify_write_identity = Mock()
    if failure == "exception":
        wire.write_register.side_effect = OSError("private error text")
    else:
        wire.write_register.return_value.isError.return_value = True
    assert not client.write_breaker_current(63)
    wire.write_register.assert_called_once_with(10026, 63, device_id=247)
    trace = client.diagnostics()
    assert trace["write_requests"] == 1
    assert [(e["address"], e["value"]) for e in trace["recent_events"]
            if e["event"] == "write_request"] == [(10026, 63)]
    assert "private error text" not in json.dumps(trace)
    wire.close.assert_called_once()


def test_poll_trace_keeps_only_numeric_status_and_never_writes():
    client = module.WallboxModbusClient("private-host", expected_serial="private-serial")
    wire = Mock()
    client._make_client = Mock(return_value=wire)
    data = {"sn": "private-serial", "account": "private-account",
            "modbus_status_raw": 3, "modbus_power": 3.3,
            "modbus_breaker_current": 63, "modbus_fault_01": "private-text"}
    client._read_all_inner = Mock(return_value=data)
    assert client.read_all() is data
    observation = client.diagnostics()["recent_events"][0]
    assert observation["event"] == "observation"
    assert observation["modbus_breaker_current"] == 63
    assert observation["modbus_power"] == 3.3
    assert "private-" not in json.dumps(client.diagnostics())
    wire.write_register.assert_not_called()
    wire.close.assert_called_once()


def decoded_blocks(changes=None):
    """Decode full protocol blocks while keeping string registers independent."""
    blocks = {10000: [0] * 20, 10020: [0] * 20, 10040: [0] * 20,
              10060: [0] * 20, 10084: [0] * 26}
    blocks[10000][17] = 3
    blocks[10040][:8] = [0x5445, 0x5354, 0, 0, 0, 0, 0, 0]
    for address, values in (changes or {}).items():
        for index, value in values.items():
            blocks[address][index] = value
    client = module.WallboxModbusClient("private-host")
    client._read = Mock(side_effect=lambda wire, address, count: blocks[address])
    return client._read_all_inner(Mock())


@pytest.mark.parametrize("hi,lo,expected", [(0, 0, 0), (0, 0xFFFF, 6553.5),
    (0xFFFF, 0xFFFE, 429496729.4), (0xFFFF, 0xFFFF, None)])
def test_modbus_unavailable_counter_is_not_a_statistics_spike(hi, lo, expected):
    data = decoded_blocks({10060: {5: hi, 6: lo},
                           10084: {19: hi, 20: lo, 21: hi, 22: lo}})
    assert data["modbus_energy_total"] == expected
    assert data["modbus_green_energy"] == expected
    assert data["modbus_grid_energy"] == expected
    assert data["sn"] == "TEST"
    assert data["modbus_power"] == 0


def test_modbus_missing_scalars_preserve_unknown_and_healthy_fields():
    data = decoded_blocks({10000: {9: 0xFFFF, 12: 0xFFFF, 15: 0xFFFF, 16: 0xFFFF},
                           10020: {6: 0xFFFF, 7: 0xFFFF, 9: 0xFFFF},
                           10060: {3: 0xFFFF, 4: 0xFFFF}})
    for key in ("modbus_voltage_a", "modbus_current_a", "modbus_power",
                "modbus_energy_session", "last_charge_energy", "last_charge_power",
                "last_charge_duration_minutes", "modbus_breaker_current",
                "modbus_max_capacity", "set_charge_power"):
        assert data[key] is None, key
    assert data["modbus_voltage_b"] == 0
    assert data["chargeMode"] == 0


@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("kind", ["ems", "dynamic"])
async def test_switch_readback_confirms_integer_ems_without_weakening_boolean_dlm(monkeypatch, requested, kind):
    from types import SimpleNamespace
    from tests.test_switch import _switch_mod as switches
    confirmation = importlib.import_module(switches.__package__ + ".write_confirmation")
    field = "modbus_ems_dispatch" if kind == "ems" else "modbus_dynamic_load"
    block, offset = (10000, 0) if kind == "ems" else (10020, 5)
    reports = decoded_blocks({block: {offset: int(not requested)}})
    async def execute(fn, *args):
        return fn(*args)
    owner = SimpleNamespace(data={"TEST": reports}, last_update_success=True,
        routing_epoch=0, local=False, transitioning=False, _closed=False,
        schedule_delayed_refresh=Mock(), hass=SimpleNamespace(async_add_executor_job=execute))
    monitor = owner.write_confirmation = confirmation.WriteConfirmation(owner, modbus=True)
    owner.async_request_refresh = Mock()
    monkeypatch.setattr(confirmation, "async_call_later", lambda *args: lambda: None)
    write = Mock(return_value=True)
    client = SimpleNamespace(write_ems_dispatch=write, write_dynamic_load_mgmt=write)
    cls = switches.ModbusEmsDispatchSwitch if kind == "ems" else switches.ModbusDynamicLoadMgmtSwitch
    entity = cls(owner, "TEST", client)
    entity.hass = owner.hass
    entity.async_write_ha_state = Mock()
    await (entity.async_turn_on() if requested else entity.async_turn_off())
    try:
        assert type(monitor.pending[field].value) is (int if kind == "ems" else bool)
        for invalid in [None, 0xFFFF, 2, int(not requested) if kind == "ems" else not requested]:
            monitor.observed({field: invalid})
            assert field in monitor.pending
        fresh = decoded_blocks({block: {offset: int(requested)}})
        owner.data["TEST"] = fresh
        monitor.observed(fresh)
        assert monitor.results[field] == "confirmed"
        assert not monitor.pending
        assert entity.is_on is requested
        write.assert_called_once_with(requested)
    finally:
        monitor.close()
