"""Cumulative storage integrity and independent loopback read-only coverage."""

import asyncio
import importlib
import struct

import pytest

from tests.test_native_transport import (
    IDENTITY,
    PACKAGE,
    SERIAL,
    protocol,
    report,
    transport,
)

energy = importlib.import_module(PACKAGE + ".native_energy")


def snapshot(*, raw_energy=123456, seconds=98765, sessions=70000):
    raw = bytearray(496)
    raw[:2] = b"\xaa\xf5"
    struct.pack_into("<HBBH", raw, 2, 496, 16, 91, 602)
    raw[8:30] = IDENTITY
    raw[34:66] = SERIAL.encode().ljust(32, b"\0")
    raw[66] = 2
    struct.pack_into("<HHH", raw, 67, 1, 1, 422)
    raw[73:75] = b"\xeb\x90"
    struct.pack_into("<I", raw, 76, sessions)
    struct.pack_into("<II", raw, 485, raw_energy, seconds)
    struct.pack_into("<H", raw, 493, sum(raw[73:493]) & 65535)
    raw[-1] = sum(raw[6:-1]) & 255
    return raw


def decode(raw):
    return energy.decode_energy(protocol.NativeDecoder().feed(bytes(raw))[0], SERIAL)


def test_scale_and_full_width_counter():
    assert decode(snapshot()) == energy.NativeEnergy(1234.56, 98765, 70000)
    assert decode(snapshot(raw_energy=0, seconds=0, sessions=0)).energy_kwh == 0
    assert decode(snapshot(raw_energy=4294967295)).energy_kwh == 42949672.95


@pytest.mark.parametrize("offset", [34, 66, 67, 69, 71, 73, 76, 485, 493])
def test_invalid_identity_shape_or_storage_rejected(offset):
    raw = snapshot(); raw[offset] ^= 1
    raw[-1] = sum(raw[6:-1]) & 255
    with pytest.raises(ValueError):
        decode(raw)


def test_encoder_only_requests_one_cumulative_block_and_never_acknowledges_it():
    raw = energy.energy_request(IDENTITY, 3)
    assert len(raw) == 84 and raw[5] == 3
    assert int.from_bytes(raw[6:8], "little") == 5
    assert raw[30:-1] == bytes.fromhex("0000000001011b00000001040002000000") + bytes(36)
    packet = protocol.NativeDecoder().feed(snapshot())[0]
    assert protocol.acknowledgement(packet) is None


async def connect(subject, response):
    await subject.async_listen("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", subject.port)
    writer.write(report()); await writer.drain()
    seen = []
    async def peer():
        decoder = protocol.NativeDecoder()
        while chunk := await reader.read(8192):
            for frame in decoder.feed(chunk):
                seen.append(frame.command)
                if frame.command == 5 and response is not None:
                    raw = response()
                    # Exercise fragmented storage replies, independent of status.
                    writer.write(raw[:180]); await writer.drain()
                    await asyncio.sleep(.01)
                    writer.write(raw[180:]); await writer.drain()
                elif frame.command == 1:
                    writer.write(report()); await writer.drain()
    task = asyncio.create_task(peer())
    while not subject.available:
        await asyncio.sleep(.01)
    return writer, task, seen


@pytest.mark.asyncio
async def test_loopback_snapshot_then_control_status_without_history_ack():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    writer, task, seen = await connect(subject, snapshot)
    try:
        result = await subject.async_read_energy()
        assert result.energy_kwh == 1234.56
        assert (await subject.async_command("status")).stopped
        assert seen.count(5) == 1 and set(seen) <= {1, 5, 103}
    finally:
        await subject.async_close(); writer.close(); await task


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "corrupt"])
async def test_optional_read_failure_keeps_controls_but_blocks_late_retry(response):
    def corrupt():
        raw = snapshot(); raw[485] ^= 1; raw[-1] = sum(raw[6:-1]) & 255
        return raw
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    writer, task, seen = await connect(subject, corrupt if response else None)
    try:
        with pytest.raises((TimeoutError, ValueError)):
            await subject.async_read_energy(timeout=.7)
        assert subject.available
        with pytest.raises(ConnectionError):
            await subject.async_read_energy()
        assert (await subject.async_command("status")).stopped
        assert seen.count(5) == 1
    finally:
        await subject.async_close(); writer.close(); await task


@pytest.mark.asyncio
async def test_cancellation_after_send_keeps_status_path_alive():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    writer, task, seen = await connect(subject, None)
    try:
        request = asyncio.create_task(subject.async_read_energy())
        while 5 not in seen:
            await asyncio.sleep(.01)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert subject.available and subject._energy_pending is None
        assert (await subject.async_command("status")).stopped
    finally:
        await subject.async_close(); writer.close(); await task


@pytest.mark.asyncio
async def test_active_session_rejected_before_any_storage_command():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    writer, task, seen = await connect(subject, snapshot)
    try:
        writer.write(report(state=2, power=42)); await writer.drain()
        while not subject.latest.charging:
            await asyncio.sleep(.01)
        with pytest.raises(ConnectionError):
            await subject.async_read_energy()
        assert 5 not in seen
    finally:
        await subject.async_close(); writer.close(); await task
