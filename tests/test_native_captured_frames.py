"""Replay sanitized physical cable observations with independent expectations."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "captured_native_protocol", ROOT / "custom_components/sems_wallbox/native_protocol.py"
)
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)
CAPTURE = json.loads((ROOT / "tests/fixtures/hca_idle_cable.json").read_text())


@pytest.mark.parametrize("record", CAPTURE["frames"],
                         ids=lambda row: f"{row['phase']}-{row['command']}")
def test_physical_idle_cable_report(record):
    """Confirmed cable phases must survive parsing of real device payloads."""
    decoder = protocol.NativeDecoder()
    frames = decoder.feed(bytes.fromhex(record["hex"]))
    assert len(frames) == 1 and not decoder.buffer
    frame = frames[0]
    assert frame.command == record["command"]
    status = protocol.decode_status(frame, CAPTURE["serial"])
    assert status.connection == record["expected_connection"]
    assert status.state == record["expected_state"]
    assert status.power_kw == record["expected_power_kw"]
    assert status.currents_a == (0.0, 0.0, 0.0)
    assert status.stopped and not status.charging
