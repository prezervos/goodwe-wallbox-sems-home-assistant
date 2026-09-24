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
