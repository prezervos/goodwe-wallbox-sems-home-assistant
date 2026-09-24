"""Passive fault details: evidence-backed decoding and real loopback lifecycle."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from tests.test_native_transport import IDENTITY, SERIAL, protocol, report, transport, ready

faults = importlib.import_module(transport.__package__ + ".native_faults")
diagnostics = importlib.import_module(transport.__package__ + ".diagnostics")


def fault_frame(*bits, serial=SERIAL, connector=1, marker=1):
    """Build the documented command-108 layout independently of the decoder."""
    body = bytearray(68)
    body[3] = connector
    body[4:36] = serial.encode().ljust(32, b"\0")
    body[39] = marker
    for bit in bits:
        body[36 + bit // 8] |= 1 << (bit % 8)
    return protocol._frame(108, bytes(body), IDENTITY, 0)


@pytest.mark.parametrize("bit,label", [
    (33, "stop"), (38, "earth_err"), (41, "env_temp_over"),
    (42, "gun_temp_over"), (45, "relay_err"), (48, "vol_over"),
    (49, "vol_less"), (54, "curr_over"), (57, "meter_comm_err"),
    (66, "leak_curr"), (67, "curr_less"), (68, "meter_fault"),
])
def test_reference_labels(bit, label):
    packet = protocol.NativeDecoder().feed(fault_frame(bit))[0]
    assert faults.decode_fault_report(packet, SERIAL).reference_labels == (label,)
    assert protocol.acknowledgement(packet) is None


def test_marker_is_not_fault_and_unknown_bits_survive():
    packet = protocol.NativeDecoder().feed(fault_frame(32, 70, 73, 162))[0]
    result = faults.decode_fault_report(packet, SERIAL)
    assert result.reference_labels == ()
    assert result.unlabelled_condition_bits == (32, 70)
    assert result.auxiliary_bits == (73,)
    assert result.unknown_bits == (162,)


@pytest.mark.parametrize("kwargs", [
    {"serial": "OTHER"}, {"connector": 2}, {"marker": 0}, {"marker": 2},
])
def test_unrecognized_reports_rejected(kwargs):
    packet = protocol.NativeDecoder().feed(fault_frame(**kwargs))[0]
    with pytest.raises(ValueError):
        faults.decode_fault_report(packet, SERIAL)


@pytest.mark.parametrize("command,length", [(104, 68), (108, 67), (108, 69)])
def test_wrong_command_or_length(command, length):
    packet = protocol.NativeDecoder().feed(protocol._frame(command, bytes(length), IDENTITY, 0))[0]
    with pytest.raises(ValueError):
        faults.decode_fault_report(packet, SERIAL)


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def read_frame(reader):
    head = await reader.readexactly(4)
    return head + await reader.readexactly(int.from_bytes(head[2:4], "little") - 4)


async def test_passive_report_lifecycle_and_diagnostic_export(monkeypatch):
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        # A diagnostic report cannot enroll a connection or refresh telemetry.
        writer.write(fault_frame(48) + report())
        await writer.drain()
        await ready(server)
        assert not server.fault_diagnostics(active=True)["received"]
        assert int.from_bytes((await asyncio.wait_for(read_frame(reader), 2))[6:8], "little") == 103
        observed_at = server.observed_at
        writer.write(fault_frame(48, 162))
        await writer.drain()
        await wait_until(lambda: server.fault_diagnostics(active=True)["received"])
        snapshot = server.fault_diagnostics(active=True)
        assert snapshot["last_report"]["reference_labels"] == ("vol_over",)
        assert snapshot["last_report"]["unknown_bits"] == (162,)
        assert snapshot["complete_fault_coverage"] is False
        assert server.observed_at == observed_at
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.05)
        # Returned dictionaries cannot mutate the cache.
        snapshot["last_report"]["reference_labels"] = ("fabricated",)
        assert server.fault_diagnostics(active=True)["last_report"]["reference_labels"] == ("vol_over",)
        original_clock = transport.time.monotonic
        with monkeypatch.context() as patch:
            now = original_clock()
            patch.setattr(transport.time, "monotonic", lambda: now + 100)
            aged = server.fault_diagnostics(active=True)
            assert aged["age_seconds"] >= 100
            assert not aged["session_usable"]
        owner = SimpleNamespace(data={}, last_update_success=True, local=True,
                                transitioning=False, transport=server,
                                endpoint=SimpleNamespace(journal=None), cloud_restored_at=None)
        entry = SimpleNamespace(entry_id="test", data={"wallbox_serial_No": SERIAL})
        hass = SimpleNamespace(data={"sems_wallbox": {"test": {"coordinator": owner}}})
        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        assert result["native"]["fault_details"]["received"]
        assert SERIAL not in json.dumps(result)
        assert "127.0.0.1" not in json.dumps(result)
        for local, transitioning in [(False, False), (True, True)]:
            owner.local, owner.transitioning = local, transitioning
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
            assert result["native"]["fault_details"]["last_report"] is None
        # Invalid optional layout clears previous details but preserves status.
        writer.write(fault_frame(marker=2))
        await writer.drain()
        await wait_until(lambda: server.fault_diagnostics(active=True)["rejected_reports"] == 1)
        assert not server.fault_diagnostics(active=True)["received"]
        assert server.available
        writer.write(fault_frame(serial="OTHER"))
        await writer.drain()
        await wait_until(lambda: server.fault_diagnostics(active=True)["rejected_reports"] == 2)
        assert server.available
        # A later valid empty report replaces the old details, without declaring health.
        writer.write(fault_frame())
        await writer.drain()
        await wait_until(lambda: server.fault_diagnostics(active=True)["received"])
        assert server.fault_diagnostics(active=True)["last_report"]["reference_labels"] == ()
        # Re-registration invalidates diagnostics even on the same socket.
        body = bytearray(91)
        body[4:36] = SERIAL.encode().ljust(32, b"\0")
        writer.write(protocol._frame(106, bytes(body), IDENTITY, 0))
        await writer.drain()
        await wait_until(lambda: not server.fault_diagnostics(active=True)["received"])
        assert server.fault_diagnostics(active=True)["rejected_reports"] == 0
        writer.write(report() + fault_frame(45))
        await writer.drain()
        await wait_until(lambda: server.fault_diagnostics(active=True)["received"])
        await server.async_disconnect(expected=True)
        assert server.fault_diagnostics(active=True)["last_report"] is None
        server.accepting = True
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            writer2.write(report())
            await writer2.drain()
            await ready(server)
            assert not server.fault_diagnostics(active=True)["received"]
        finally:
            writer2.close()
            await writer2.wait_closed()
    finally:
        await server.async_close()
        writer.close()
        await writer.wait_closed()
