"""Minimum-power native encoding and independent socket confirmation tests."""

import asyncio
import struct
import pytest
from tests.test_native_transport import (
    protocol,
    transport,
    report,
    ready,
    SERIAL,
    IDENTITY,
)
from dataclasses import replace


def status(flag, mode=1):
    raw = bytearray(report(mode=mode))
    raw[237] = flag
    raw[-1] = sum(raw[6:-1]) & 255
    return bytes(raw)


def test_minimum_power_wire_values_and_unknown_readback():
    for flag, expected in ((0, False), (170, True), (1, None), (255, None)):
        value = protocol.decode_status(
            protocol.NativeDecoder().feed(status(flag))[0], SERIAL
        )
        assert value.minimum_power is expected
    for flag in (False, True):
        raw = protocol.encode_command("minimum_power", IDENTITY, 9, minimum_power=flag)
        body = protocol.NativeDecoder().feed(raw)[0].raw[30:-1]
        assert struct.unpack("<HBBBIBHI", body) == (
            0,
            0,
            1,
            1,
            49,
            1,
            4,
            170 if flag else 0,
        )
    for invalid in (None, 0, 1, 170, "on"):
        try:
            protocol.encode_command("minimum_power", IDENTITY, 9, minimum_power=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid boolean accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [True, False])
@pytest.mark.parametrize("mode", [0, 1, 2])
async def test_minimum_power_requires_fresh_matching_report(apply, mode):
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(status(170, mode))
    await writer.drain()
    writes = []

    async def device():
        flag = 170
        try:
            while True:
                head = await reader.readexactly(4)
                raw = head + await reader.readexactly(
                    int.from_bytes(head[2:4], "little") - 4
                )
                if int.from_bytes(raw[6:8], "little") != 1:
                    continue
                body = raw[30:-1]
                if body[4] == 1:
                    assert int.from_bytes(body[5:9], "little") == 49
                    requested = int.from_bytes(body[12:16], "little")
                    writes.append(requested)
                    if apply:
                        flag = requested
                writer.write(status(flag, mode))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    task = asyncio.create_task(device())
    try:
        await ready(server)
        before = server.latest
        predicate = server._confirmation(
            "minimum_power", {"minimum_power": False}, before
        )
        assert not predicate(before)
        assert not predicate(replace(before, minimum_power=False, mode=(mode + 1) % 3))
        assert not predicate(replace(before, minimum_power=False, limit_kw=11))
        assert not predicate(replace(before, minimum_power=None))
        try:
            actual = await server.async_command(
                "minimum_power", minimum_power=False, timeout=1.5
            )
            assert apply and actual.minimum_power is False
            assert actual.mode == mode and actual.limit_kw == 4.2 and actual.stopped
        except TimeoutError:
            assert not apply
        assert writes == [0], writes
    finally:
        await server.async_close()
        writer.close()
        await writer.wait_closed()
        await task
