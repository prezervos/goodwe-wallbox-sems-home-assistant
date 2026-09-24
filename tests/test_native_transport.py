"""Verify native wire behavior against an independent loopback device."""

import asyncio
import importlib
import struct
import sys
import time
import types
from pathlib import Path

import pytest

PACKAGE = "sems_native_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "sems_wallbox")
]
sys.modules[PACKAGE] = package
protocol = importlib.import_module(PACKAGE + ".native_protocol")
transport = importlib.import_module(PACKAGE + ".native_transport")
SERIAL = "5011KHCA-TEST-123"
IDENTITY = bytes(range(22))


def report(
    mode: int = 0,
    state: int = 0,
    limit: int = 42,
    power: int = 0,
    serial: str = SERIAL,
    command: int = 104,
) -> bytes:
    """Build an independent synthetic status with known field positions."""
    raw = bytearray(239)
    raw[:2] = b"\xaa\xf5"
    struct.pack_into("<HBBH", raw, 2, len(raw), 16, 7, command)
    raw[8:30] = IDENTITY
    raw[34:66] = serial.encode().ljust(32, b"\0")
    raw[67] = 1
    raw[69] = state
    raw[75] = 2 if state == 2 else 1
    for pos in (97, 99, 101):
        struct.pack_into("<H", raw, pos, 2310)
    for pos in (103, 105, 107):
        struct.pack_into("<H", raw, pos, 55 if power else 0)
    struct.pack_into("<I", raw, 183, power)
    struct.pack_into("<I", raw, 187, limit)
    raw[234] = mode
    raw[-1] = sum(raw[6:-1]) & 255
    return bytes(raw)


@pytest.mark.parametrize("split", [1, 3, 30, 120, 238])
def test_fragmented_coalesced_frames(split: int) -> None:
    decoder = protocol.NativeDecoder()
    raw = report()
    assert decoder.feed(raw[:split]) == []
    packets = decoder.feed(raw[split:] + raw)
    assert len(packets) == 2
    status = protocol.decode_status(packets[0], SERIAL)
    assert status.limit_kw == 4.2
    assert status.power_kw == 0
    assert status.voltages_v == (231.0, 231.0, 231.0)
    assert status.stopped


@pytest.mark.parametrize("offset", [0, 2, 4, 69, 115, 238])
def test_corruption_rejected(offset: int) -> None:
    raw = bytearray(report())
    raw[offset] ^= 255
    with pytest.raises(ValueError):
        protocol.NativeDecoder().feed(raw)


@pytest.mark.parametrize("mode", [-1, 3, 65536, True, 1.0])
def test_mode_bounds(mode: object) -> None:
    with pytest.raises(ValueError):
        protocol.encode_command("mode", IDENTITY, 0, mode=mode)


@pytest.mark.parametrize("power", [0, 13, 221, True, 42.0])
def test_power_scope(power: object) -> None:
    with pytest.raises(ValueError):
        protocol.encode_command("power", IDENTITY, 0, tenths_kw=power)


def test_wrong_serial_rejected() -> None:
    packet = protocol.NativeDecoder().feed(report(serial="OTHER"))[0]
    with pytest.raises(ValueError):
        protocol.decode_status(packet, SERIAL)


def test_bill_never_acknowledged() -> None:
    raw = bytearray(319)
    packet = protocol.NativeFrame(202, 0, IDENTITY, bytes(raw))
    assert protocol.acknowledgement(packet) is None


class Device:
    """Loopback device emulator; command decoding is independent of encoders."""

    def __init__(self, behavior: str = "apply") -> None:
        self.behavior = behavior
        self.mode = 0
        self.state = 0
        self.power = 0
        self.limit = 42
        self.writes = []
        self.writer = None
        self.last_report = 0.0
        self.reader = None
        self.energy_raw = 123456
        self.energy_reads = 0

    async def connect(self, port: int) -> None:
        """Connect and announce fresh idle telemetry."""
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        await self.status()

    async def status(self) -> None:
        """Send an independently serialized status report."""
        self.last_report = time.monotonic()
        self.writer.write(report(self.mode, self.state, self.limit, self.power))
        await self.writer.drain()

    async def run(self) -> None:
        """Read command boundaries and simulate reported device state."""
        try:
            while True:
                head = await self.reader.readexactly(4)
                size = int.from_bytes(head[2:4], "little")
                raw = head + await self.reader.readexactly(size - 4)
                command = int.from_bytes(raw[6:8], "little")
                if command in (101, 103, 105):
                    continue
                body = raw[30:-1]
                if command == 1 and body[4] == 0:
                    await self.status()
                    continue
                if command == 5 and int.from_bytes(body[6:10], "little") == 27:
                    from tests.test_native_energy import snapshot
                    assert len(body) == 53 and int.from_bytes(body[13:17], "little") == 2
                    self.energy_reads += 1
                    self.writer.write(snapshot(raw_energy=self.energy_raw))
                    await self.writer.drain()
                    continue
                self.writes.append(command)
                if self.behavior == "disconnect":
                    self.writer.close()
                    return
                if self.behavior in (
                    "apply",
                    "excess_power",
                    "ignore_first_stop",
                    "ignore_stops",
                    "ignore_coalesced_stop",
                ):
                    if command == 1:
                        parameter = int.from_bytes(body[5:9], "little")
                        value = int.from_bytes(body[12:16], "little")
                        if parameter == 48:
                            self.mode = value
                            if value == 0:
                                self.limit = 110
                        elif parameter == 47:
                            self.limit = value
                    elif command == 7:
                        self.state, self.power = (
                            2,
                            108 if self.behavior != "apply" else 38,
                        )
                    elif command == 5:
                        if (
                            self.behavior != "ignore_stops"
                            and (
                                self.behavior != "ignore_first_stop"
                                or self.writes.count(5) > 1
                            )
                            and (
                                self.behavior != "ignore_coalesced_stop"
                                or time.monotonic() - self.last_report >= 0.3
                            )
                        ):
                            self.state, self.power = 0, 0
                await self.status()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def close(self) -> None:
        """Close the simulated connection."""
        self.writer.close()
        await self.writer.wait_closed()


async def ready(server) -> None:
    """Wait for real socket telemetry, bounded to one second."""
    async with asyncio.timeout(1):
        while not server.available:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_live_socket_mode_power_start_stop() -> None:
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        assert (await server.async_command("mode", mode=1)).mode == 1
        pv = await server.async_command("power", tenths_kw=43)
        assert pv.mode == 1 and pv.limit_kw == 4.3
        assert (await server.async_command("mode", mode=0)).mode == 0
        assert (await server.async_command("power", tenths_kw=43)).limit_kw == 4.3
        assert (await server.async_command("start", session_id="test-1")).charging
        with pytest.raises(ValueError):
            await server.async_command("start", session_id="duplicate")
        assert (await server.async_command("stop")).stopped
        async with asyncio.timeout(1):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        assert device.writes.count(7) == 1
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior,error", [("ack_only", TimeoutError), ("disconnect", ConnectionError)]
)
async def test_unconfirmed_write_never_replayed(behavior: str, error: type) -> None:
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device(behavior)
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(error):
            await server.async_command("mode", mode=1, timeout=1)
        assert device.writes == [1]
        with pytest.raises(ConnectionError):
            await server.async_command("start", session_id="must-not-send")
        assert device.writes == [1]
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_start_excess_power_stops_and_fails() -> None:
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("excess_power")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(transport.NativePowerLimitError):
            await server.async_command("start", session_id="excess-power", timeout=2)
        assert device.writes == [7, 5]
        assert device.power == 0
    finally:
        await server.async_close()
        await device.close()
        await task


discovery = importlib.import_module(PACKAGE + ".native_discovery")
adapter_module = importlib.import_module(PACKAGE + ".native_adapter")
policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")


@pytest.mark.parametrize(
    "data,peer",
    [
        (b"192.0.2.1,AABBCCDDEEFF,TEST", ("192.0.2.2", 48899)),
        (b"192.0.2.1,invalid,TEST", ("192.0.2.1", 48899)),
        (b"192.0.2.1,AABBCCDDEEFF,TEST", ("192.0.2.1", 1234)),
        (b"192.0.2.1,AABBCCDDEEFF,", ("192.0.2.1", 48899)),
        (b"garbage", ("192.0.2.1", 48899)),
    ],
)
def test_discovery_rejects_invalid(data: bytes, peer: tuple[str, int]) -> None:
    with pytest.raises(ValueError):
        discovery.parse_discovery(data, peer)


class ProbeResponder(asyncio.DatagramProtocol):
    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        assert data == b"HF-A11ASSISTHREAD"
        self.transport.sendto(b"127.0.0.1,AABBCCDDEEFF,TEST", addr)


@pytest.mark.asyncio
async def test_discovery_real_udp() -> None:
    responder, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        ProbeResponder, local_addr=("127.0.0.1", 48899)
    )
    try:
        result = await discovery.async_discover("127.0.0.1", timeout=0.1)
        assert result == [
            discovery.DiscoveredWallbox("127.0.0.1", "AABBCCDDEEFF", "TEST")
        ]
    finally:
        responder.close()


class MemoryStore:
    async def async_load(self) -> dict:
        return {"mode": 0}

    async def async_save(self, data: dict) -> None:
        self.data = data


@pytest.mark.asyncio
async def test_native_policy_restores_external_pv_before_start() -> None:
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.mode = 1
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        adapter = adapter_module.NativeModeAdapter(server)
        policy = policy_module.ChargeModePolicy(adapter, MemoryStore(), enabled=True)
        await policy.async_load()
        await policy.async_start()
        async with asyncio.timeout(1):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        assert device.mode == 0
        assert device.limit == 42
        assert device.writes == [1, 1, 1, 7]
        await policy.async_stop()
        assert device.power == 0
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_cancelled_write_is_not_replayed() -> None:
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ack_only")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        command = asyncio.create_task(server.async_command("mode", mode=1))
        async with asyncio.timeout(2):
            while not device.writes:
                await asyncio.sleep(0.01)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert device.writes == [1]
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_native_adapter_restores_power_after_mode_reset():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".native_adapter")
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    fake = SimpleNamespace(
        serial=SERIAL,
        async_command=AsyncMock(
            side_effect=[
                SimpleNamespace(mode=0, limit_kw=11),
                SimpleNamespace(mode=0, limit_kw=4.2),
            ]
        ),
    )
    adapter = adapter_module.NativeModeAdapter(fake)
    assert await adapter.write_mode(0, policy_module.ModeObservation(1, power=4.2))
    assert fake.async_command.call_args_list[0].args == ("mode",)
    assert fake.async_command.call_args_list[1].kwargs == {"tenths_kw": 42}


@pytest.mark.asyncio
async def test_native_active_fast_transition_refused_before_write():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".native_adapter")
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    fake = SimpleNamespace(serial=SERIAL, async_command=AsyncMock())
    with pytest.raises(policy_module.ModeVerificationError, match="Stop charging"):
        await adapter_module.NativeModeAdapter(fake).write_mode(
            0, policy_module.ModeObservation(1, power=4.2, active=True)
        )
    fake.async_command.assert_not_called()


@pytest.mark.asyncio
async def test_native_every_start_reapplies_limit_even_when_report_matches():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    idle = SimpleNamespace(stopped=True, mode=0, limit_kw=4.2)
    charging = SimpleNamespace(charging=True)
    fake = SimpleNamespace(
        serial=SERIAL,
        async_command=AsyncMock(side_effect=[idle, charging, idle, charging]),
    )
    adapter = adapter_module.NativeModeAdapter(fake)
    assert await adapter.start()
    assert await adapter.start()
    assert [call.args[0] for call in fake.async_command.call_args_list] == [
        "status",
        "start",
        "status",
        "start",
    ]
    assert fake.async_command.call_args_list[1].kwargs["tenths_kw"] == 42
    assert fake.async_command.call_args_list[3].kwargs["tenths_kw"] == 42


@pytest.mark.asyncio
async def test_listener_unload_closes_idle_peer_without_waiting_for_stale_timeout():
    server = transport.NativeTransport(SERIAL, "127.0.0.1", stale_after=45)
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    await ready(server)
    try:
        async with asyncio.timeout(1):
            await server.async_close()
        assert not server.available
        await device.reader.read()  # Drain an already queued status acknowledgement.
        assert device.reader.at_eof()
    finally:
        await device.close()


@pytest.mark.asyncio
async def test_native_start_preparation_failure_is_reported():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    idle = SimpleNamespace(stopped=True, mode=0, limit_kw=4.2)
    fake = SimpleNamespace(
        serial=SERIAL,
        async_command=AsyncMock(
            side_effect=[idle, TimeoutError("No power confirmation")]
        ),
    )
    with pytest.raises(policy_module.ModeVerificationError):
        await adapter_module.NativeModeAdapter(fake).start()
    assert [call.args[0] for call in fake.async_command.call_args_list] == [
        "status",
        "start",
    ]


@pytest.mark.asyncio
async def test_handover_disconnects_peer_and_listener_can_be_reused():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    first = Device()
    await first.connect(server.port)
    await ready(server)
    async with asyncio.timeout(1):
        await server.async_disconnect()
    assert not server.available
    assert not server.accepting
    await first.close()
    server.accepting = True
    second = Device()
    await second.connect(server.port)
    task = asyncio.create_task(second.run())
    try:
        await ready(server)
        assert (await server.async_command("status")).serial == SERIAL
    finally:
        await server.async_close()
        await second.close()
        await task


@pytest.mark.asyncio
async def test_ignored_early_safety_stop_is_retried_and_confirmed():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ignore_first_stop")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(transport.NativePowerLimitError, match="Stop confirmed"):
            await server.async_command("start", session_id="early-stop", timeout=12)
        assert device.writes == [7, 5, 5]
        assert server.available and server.latest.stopped
        assert (await server.async_command("status")).stopped
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_unconfirmed_safety_stop_retries_are_bounded_without_start_replay():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ignore_stops")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(TimeoutError):
            await server.async_command("start", session_id="ignored-stop", timeout=14)
        assert device.writes == [7, 5, 5, 5]
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_protective_stop_is_separated_from_status_ack_receive_path():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ignore_coalesced_stop")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(transport.NativePowerLimitError, match="Stop confirmed"):
            await server.async_command("start", session_id="separate-stop", timeout=5)
        assert device.writes == [7, 5]
        assert server.available and server.latest.stopped
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.parametrize("power", [42, 43, 55])
def test_power_frame_contains_only_one_value_without_mode(power):
    """Match the reference firmware single-value parameter 47 layout."""
    raw = protocol.encode_command("power", IDENTITY, 19, tenths_kw=power)
    assert int.from_bytes(raw[2:4], "little") == 47
    assert int.from_bytes(raw[6:8], "little") == 1
    assert raw[30:-1] == struct.pack("<HBBBIBHI", 0, 0, 1, 1, 47, 1, 4, power)
    assert raw[-1] == sum(raw[6:-1]) & 255


@pytest.mark.asyncio
async def test_native_fast_power_mismatch_does_not_rewrite_mode():
    """Restore a saved limit without retriggering a mode transition."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    fake = SimpleNamespace(
        serial=SERIAL,
        async_command=AsyncMock(return_value=SimpleNamespace(mode=0, limit_kw=4.2)),
    )
    adapter = adapter_module.NativeModeAdapter(fake)
    assert await adapter.write_mode(0, policy_module.ModeObservation(0, power=4.2))
    fake.async_command.assert_awaited_once_with("power", tenths_kw=42)


@pytest.mark.asyncio
async def test_start_transaction_keeps_power_and_start_ahead_of_queued_mode():
    """A concurrent setting cannot enter between restoration and Start."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        start = asyncio.create_task(
            server.async_command("start", tenths_kw=43, session_id="atomic-start")
        )
        async with asyncio.timeout(2):
            while not device.writes:
                await asyncio.sleep(0.01)
        mode = asyncio.create_task(server.async_command("mode", mode=1))
        assert (await start).charging
        with pytest.raises(
            ValueError, match="Stop charging before changing native mode"
        ):
            await mode
        assert device.writes == [1, 7]
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_end_state_cannot_authorize_prepared_start():
    """Zero load during session completion is not idle readiness."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.state = 3
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(TimeoutError):
            await server.async_command(
                "start", tenths_kw=42, session_id="ending", timeout=1
            )
        assert device.writes == []
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_matching_requested_limit_cannot_hide_ineffective_restoration():
    """A reset/ignored effective-current write must fail measured verification."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("excess_power")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(transport.NativePowerLimitError, match="Stop confirmed"):
            await server.async_command(
                "start", tenths_kw=42, session_id="ineffective-limit", timeout=5
            )
        assert device.limit == 42
        assert device.writes == [1, 7, 5]
        assert device.power == 0
    finally:
        await server.async_close()
        await device.close()
        await task


verification = importlib.import_module(PACKAGE + ".native_start_verification")


def test_start_window_rejects_pairs_gaps_and_fault_interruption():
    window = verification.StartObservationWindow()
    assert not window.observe(104, 0, True)
    assert not window.observe(2104, 10, True)
    assert not window.observe(104, 10, True)  # Gap restarts observation.
    assert not window.observe(104, 15, True)
    assert not window.observe(2104, 16, False)  # Either report can invalidate.
    assert not window.observe(104, 20, True)
    assert not window.observe(104, 25, True)
    assert window.observe(104, 30, True)


@pytest.mark.asyncio
async def test_slow_ramp_excess_does_not_pass_early_start_confirmation():
    """A low initial draw must not hide excess load four seconds later."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())

    async def late_ramp():
        async with asyncio.timeout(3):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        await asyncio.sleep(4)
        device.power = 108
        await device.status()

    ramp = asyncio.create_task(late_ramp())
    try:
        await ready(server)
        with pytest.raises(transport.NativePowerLimitError, match="Stop confirmed"):
            await server.async_command(
                "start", tenths_kw=42, session_id="slow-ramp", timeout=12
            )
        assert device.writes == [1, 7, 5]
        assert device.power == 0
    finally:
        ramp.cancel()
        await asyncio.gather(ramp, return_exceptions=True)
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_late_excess_after_start_return_still_stops():
    """Keep supervision after the service reports a verified initial ramp."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        assert (
            await server.async_command("start", tenths_kw=42, session_id="late-excess")
        ).charging
        device.power = 108
        await device.status()
        async with asyncio.timeout(3):
            while server.session_guard.task is None:
                await asyncio.sleep(0.01)
            await server.session_guard.task
        assert device.writes == [1, 7, 5]
        assert device.power == 0
        assert "Stop confirmed" in server.session_guard.error
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_guard_power_reduction_grace_is_bounded(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    guard_module = importlib.import_module(PACKAGE + ".native_session_guard")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(guard_module.time, "monotonic", lambda: clock.value)
    stop = AsyncMock(return_value=SimpleNamespace(stopped=True))
    guard = guard_module.NativeSessionGuard(stop)
    guard.arm(5.5)
    guard.update_limit(4.2)
    guard.observe(SimpleNamespace(power_kw=5.4))
    assert guard.task is None
    clock.value = 11
    guard.observe(SimpleNamespace(power_kw=5.4))
    await guard.task
    stop.assert_awaited_once()
    assert "Stop confirmed" in guard.error


@pytest.mark.asyncio
async def test_guard_failed_stop_is_not_reported_as_confirmed():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    guard_module = importlib.import_module(PACKAGE + ".native_session_guard")
    guard = guard_module.NativeSessionGuard(
        AsyncMock(side_effect=ConnectionError("peer lost"))
    )
    guard.arm(4.2)
    guard.observe(SimpleNamespace(power_kw=10.8))
    await guard.task
    assert "could not confirm Stop" in guard.error
    await guard.close()


@pytest.mark.asyncio
async def test_guard_repeated_reductions_cannot_extend_settling_forever(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    guard_module = importlib.import_module(PACKAGE + ".native_session_guard")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(guard_module.time, "monotonic", lambda: clock.value)
    stop = AsyncMock(return_value=SimpleNamespace(stopped=True))
    guard = guard_module.NativeSessionGuard(stop)
    guard.arm(5.5)
    clock.value = 5
    guard.update_limit(4.3)
    clock.value = 9
    guard.update_limit(4.2)
    clock.value = 16
    guard.observe(SimpleNamespace(power_kw=5.4))
    await guard.task
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_guard_allows_explicit_power_increase_before_report():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    guard_module = importlib.import_module(PACKAGE + ".native_session_guard")
    stop = AsyncMock()
    guard = guard_module.NativeSessionGuard(stop)
    guard.arm(4.2)
    guard.update_limit(5.5)
    guard.observe(SimpleNamespace(power_kw=5.4))
    assert guard.task is None
    stop.assert_not_awaited()
    await guard.close()


@pytest.mark.asyncio
async def test_session_guard_retries_ignored_stop_without_replaying_start():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ignore_first_stop")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        server.session_guard.arm(4.2)
        device.state, device.power = 2, 108
        await device.status()
        async with asyncio.timeout(12):
            while server.session_guard.task is None:
                await asyncio.sleep(0.01)
            await server.session_guard.task
        assert device.writes == [5, 5]
        assert device.power == 0
        assert "Stop confirmed" in server.session_guard.error
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_guard_disconnect_during_stop_keeps_explicit_uncertainty():
    from types import SimpleNamespace

    guard_module = importlib.import_module(PACKAGE + ".native_session_guard")
    waiting = asyncio.Event()

    async def stop():
        waiting.set()
        await asyncio.Event().wait()

    guard = guard_module.NativeSessionGuard(stop)
    guard.arm(4.2)
    guard.observe(SimpleNamespace(power_kw=10.8))
    await waiting.wait()
    await guard.close()
    assert guard.task.done()
    assert "unverified" in guard.error
    assert "pending" not in guard.error


@pytest.mark.asyncio
async def test_adapter_keeps_explicit_ha_intent_when_report_changes():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    fake = SimpleNamespace(
        serial=SERIAL,
        async_command=AsyncMock(
            side_effect=[
                SimpleNamespace(stopped=True, mode=0, limit_kw=11),
                SimpleNamespace(charging=True),
            ]
        ),
    )
    assert await adapter_module.NativeModeAdapter(fake).start(power=4.2)
    assert fake.async_command.call_args_list[1].kwargs["tenths_kw"] == 42


@pytest.mark.asyncio
async def test_explicit_start_restores_intent_from_reported_model_maximum():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.limit = 110
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        assert (
            await server.async_command(
                "start", tenths_kw=42, session_id="restore-intent"
            )
        ).charging
        assert device.limit == 42
        assert device.writes == [1, 7]
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_power_restore_waits_for_delayed_finalization():
    """No setting write is sent while the previous session remains ending."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.state = 3
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    command = None
    try:
        await ready(server)
        command = asyncio.create_task(
            server.async_command(
                "start", tenths_kw=42, session_id="delayed-finalization"
            )
        )
        await asyncio.sleep(1)
        assert device.writes == []
        # Finalization resets the limit before idle becomes observable.
        device.limit, device.state = 110, 0
        await device.status()
        assert (await command).charging
        assert device.limit == 42
        assert device.writes == [1, 7]
    finally:
        if command is not None and not command.done():
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_unverified_sent_start_stops_before_releasing_connection(cancel):
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        command = asyncio.create_task(
            server.async_command(
                "start",
                tenths_kw=42,
                session_id="unverified",
                timeout=2 if not cancel else 30,
            )
        )
        if cancel:
            async with asyncio.timeout(3):
                while 7 not in device.writes:
                    await asyncio.sleep(0.01)
            command.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await command
        assert device.writes == [1, 7, 5]
        assert device.power == 0
        assert server.available
        assert "Stop confirmed" in server.session_guard.error
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_second_cancel_does_not_interrupt_recovery_or_replay_start():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ignore_first_stop")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        command = asyncio.create_task(
            server.async_command("start", tenths_kw=42, session_id="double-cancel")
        )
        async with asyncio.timeout(3):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        command.cancel()
        async with asyncio.timeout(3):
            while 5 not in device.writes:
                await asyncio.sleep(0.01)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert device.writes == [1, 7, 5, 5]
        assert device.power == 0
        assert "Stop confirmed" in server.session_guard.error
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_disconnect_during_start_recovery_reports_unconfirmed_stop():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ignore_stops")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        command = asyncio.create_task(
            server.async_command(
                "start", tenths_kw=42, session_id="recovery-disconnect"
            )
        )
        async with asyncio.timeout(3):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        command.cancel()
        async with asyncio.timeout(3):
            while 5 not in device.writes:
                await asyncio.sleep(0.01)
        await device.close()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert device.writes == [1, 7, 5]
        assert "Stop remains unconfirmed" in server.session_guard.error
        assert not server.available
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_cancel_idle_preparation_never_sends_power_or_start():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.state = 3
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        command = asyncio.create_task(
            server.async_command("start", tenths_kw=42, session_id="cancel-idle")
        )
        await asyncio.sleep(0.5)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert device.writes == []
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_automation_repeated_target_preserves_original_reduction_grace(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = importlib.import_module(PACKAGE + ".native_session_guard")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    guard = module.NativeSessionGuard(
        AsyncMock(return_value=SimpleNamespace(stopped=True))
    )
    guard.arm(11.0)
    guard.update_limit(4.2)
    clock.value = 1
    guard.update_limit(4.2)
    guard.observe(SimpleNamespace(power_kw=10))
    assert guard.task is None
    clock.value = 11
    guard.observe(SimpleNamespace(power_kw=10))
    await guard.task
    assert "Stop confirmed" in guard.error


@pytest.mark.asyncio
async def test_small_increase_during_reduction_does_not_drop_pending_allowance(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = importlib.import_module(PACKAGE + ".native_session_guard")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    guard = module.NativeSessionGuard(
        AsyncMock(return_value=SimpleNamespace(stopped=True))
    )
    guard.arm(11.0)
    guard.update_limit(4.2)
    clock.value = 1
    guard.update_limit(4.3)
    guard.observe(SimpleNamespace(power_kw=10))
    assert guard.task is None
    clock.value = 11
    guard.observe(SimpleNamespace(power_kw=10))
    await guard.task
    assert "Stop confirmed" in guard.error


@pytest.mark.asyncio
async def test_notification_failure_cannot_prevent_protective_stop():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    module = importlib.import_module(PACKAGE + ".native_session_guard")
    stop = AsyncMock(return_value=SimpleNamespace(stopped=True))
    guard = module.NativeSessionGuard(stop)
    guard.on_change = Mock(side_effect=RuntimeError("UI listener failed"))
    guard.arm(4.2)
    guard.observe(SimpleNamespace(power_kw=10.8))
    await guard.task
    stop.assert_awaited_once()
    assert "Stop confirmed" in guard.error


@pytest.mark.asyncio
async def test_background_start_releases_power_control_before_observation_window():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        began = time.monotonic()
        before = await server.async_command(
            "start", tenths_kw=42, session_id="responsive", background=True
        )
        assert before.stopped  # Acceptance does not invent actual charging.
        assert time.monotonic() - began < 2
        began = time.monotonic()
        await server.async_command("power", tenths_kw=110)
        assert time.monotonic() - began < 2
        assert device.writes == [1, 7, 1]
        assert server.session_guard.limit == 11.0
        device.power = 108
        await device.status()
        async with asyncio.timeout(1):
            while server.latest.power_kw != 10.8:
                await asyncio.sleep(0.01)
        assert server.session_guard.phase == "charging"
        assert server.session_guard.error is None
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_background_missing_charge_stops_and_notifies():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = importlib.import_module(PACKAGE + ".native_session_guard")
    stop = AsyncMock(return_value=SimpleNamespace(stopped=True))
    guard = module.NativeSessionGuard(stop)
    guard.begin(4.2, timeout=0.01)
    await guard._deadline_task
    await guard.task
    stop.assert_awaited_once()
    assert guard.phase == "stopped"
    assert "Charging was not observed" in guard.error
    assert "Stop confirmed" in guard.error
    await guard.close()


@pytest.mark.asyncio
async def test_background_duplicate_start_rejected_while_vehicle_waits():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ack_only")
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        await server.async_command(
            "start", tenths_kw=42, session_id="pending-first", background=True
        )
        with pytest.raises(ValueError, match="already awaiting"):
            await server.async_command(
                "start", tenths_kw=42, session_id="pending-duplicate", background=True
            )
        async with asyncio.timeout(1):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        assert device.writes.count(7) == 1
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["waiting_idle", "power_sent"])
async def test_newer_intent_cancels_preparation_without_late_start(stage):
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    if stage == "waiting_idle":
        device.state = 3
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    allowed = {"value": True}
    power_sent = asyncio.Event()
    send = server._send

    async def intercept(action, **values):
        await send(action, **values)
        if action == "power":
            allowed["value"] = False
            power_sent.set()

    if stage == "power_sent":
        server._send = intercept
    command = None
    try:
        await ready(server)
        command = asyncio.create_task(
            server.async_command(
                "start",
                tenths_kw=42,
                session_id="superseded",
                background=True,
                start_allowed=lambda: allowed["value"],
            )
        )
        if stage == "waiting_idle":
            await asyncio.sleep(0.5)
            allowed["value"] = False
        else:
            await asyncio.wait_for(power_sent.wait(), 2)
        async with asyncio.timeout(0.7):
            with pytest.raises(
                transport.NativeStartSuperseded, match="Start was not sent"
            ):
                await command
        assert 7 not in device.writes
        assert server.available
        # The latest Stop can use the same connection; no reconnect penalty.
        stopped = await server.async_command("stop", timeout=2)
        assert stopped.stopped
        assert device.writes == ([5] if stage == "waiting_idle" else [1, 5])
        assert server.session_guard.error is None
    finally:
        if command is not None and not command.done():
            command.cancel()
            await asyncio.gather(command, return_exceptions=True)
        await server.async_close()
        await device.close()
        await task


@pytest.mark.parametrize(
    "key,minimum,maximum",
    [
        ("011KHCA", 4.2, 11.0),
        ("022KHCA", 4.2, 22.0),
        ("7000HCA", 1.4, 7.0),
        ("7000ACA", 1.4, 7.0),
    ],
)
def test_native_model_range_and_every_decikw_step(key, minimum, maximum):
    limits = importlib.import_module(PACKAGE + ".native_power_limits")
    serial = "5" + key + "-TEST"
    assert limits.power_bounds(serial) == (minimum, maximum)
    for raw in range(round(minimum * 10), round(maximum * 10) + 1):
        assert limits.power_tenths(raw / 10, serial) == raw
        encoded = protocol.encode_command("power", IDENTITY, 0, tenths_kw=raw)
        # Decode the value from the actual parameter body, independently.
        assert int.from_bytes(encoded[-5:-1], "little") == raw
    for invalid in (
        minimum - 0.1,
        maximum + 0.1,
        minimum + 0.01,
        float("nan"),
        float("inf"),
        True,
        "4.2",
        None,
    ):
        with pytest.raises(ValueError):
            limits.power_tenths(invalid, serial)


def test_unknown_native_model_has_no_guessed_power_range():
    limits = importlib.import_module(PACKAGE + ".native_power_limits")
    with pytest.raises(ValueError, match="Unknown native model"):
        limits.power_tenths(4.2, "UNKNOWN-TEST")


@pytest.mark.asyncio
async def test_model_excess_power_rejected_before_wire_transmission():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    # No connection is needed: validation precedes queuing or I/O.
    with pytest.raises(ValueError, match="between 4.2 and 11"):
        await server.async_command("power", tenths_kw=111)
    with pytest.raises(ValueError, match="between 4.2 and 11"):
        await server.async_command("start", tenths_kw=220, session_id="invalid")


@pytest.mark.asyncio
async def test_superseded_queued_power_never_reaches_wire():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    allowed = {"value": True}
    try:
        await ready(server)
        async with server._lock:
            command = asyncio.create_task(
                server.async_command(
                    "power", tenths_kw=110, intent_allowed=lambda: allowed["value"]
                )
            )
            await asyncio.sleep(0)
            allowed["value"] = False
        with pytest.raises(ValueError, match="Setting superseded"):
            await command
        await server.async_command("power", tenths_kw=43)
        assert device.writes == [1]
        assert device.limit == 43
        assert server.available
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [False, True])
async def test_handover_and_unexpected_disconnect_have_distinct_outcomes(expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = importlib.import_module(PACKAGE + ".native_session_guard")
    guard = module.NativeSessionGuard(
        AsyncMock(return_value=SimpleNamespace(stopped=True))
    )
    guard.begin(4.2)
    guard.observe(SimpleNamespace(power_kw=3.8, charging=True))
    await guard.close(expected=expected)
    assert guard.limit is None
    assert guard.phase == ("handover" if expected else "unverified")
    if expected:
        assert guard.error is None
    else:
        assert "unverified" in guard.error


@pytest.mark.asyncio
async def test_expected_handover_does_not_hide_unfinished_protection():
    from types import SimpleNamespace

    module = importlib.import_module(PACKAGE + ".native_session_guard")

    async def stop():
        await asyncio.Event().wait()

    guard = module.NativeSessionGuard(stop)
    guard.begin(4.2)
    guard.observe(SimpleNamespace(power_kw=10.8, charging=True))
    await asyncio.sleep(0)
    await guard.close(expected=True)
    assert guard.phase == "unverified"
    assert "unverified" in guard.error


@pytest.mark.asyncio
async def test_power_change_supervises_session_started_before_tcp_takeover():
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.state, device.limit, device.power = 2, 110, 108
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        assert server.session_guard.limit is None
        await server.async_command("power", tenths_kw=42)
        guard = server.session_guard
        assert guard.limit == 4.2 and guard.phase == "charging"
        assert guard.error is None  # Existing high load gets bounded settling time.
        assert device.writes == [1]  # No replayed Start or mode change.
        guard.reduction_grace = 0.01
        # Expire the original allowance without waiting ten seconds in the test.
        guard._settling_limits = [
            (ceiling, time.monotonic() - 1) for ceiling, _ in guard._settling_limits
        ]
        await server.async_command("status")
        async with asyncio.timeout(3):
            await guard.task
        assert device.writes == [1, 5]
        assert device.power == 0
        assert "Stop confirmed" in guard.error
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [1, 2])
async def test_pv_start_preserves_mode_and_explicit_power(mode):
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ack_only")
    device.mode = mode
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        assert await adapter_module.NativeModeAdapter(server).start(power=4.2)
        async with asyncio.timeout(1):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        assert device.writes == [1, 7]
        assert device.mode == mode
        guard = server.session_guard
        assert guard.mode == mode and guard.phase == "waiting"
        assert guard.limit == 4.2 and guard._deadline_task is None
        assert guard.error is None
        with pytest.raises(ValueError, match="already awaiting"):
            await server.async_command(
                "start",
                start_mode=mode,
                background=True,
                tenths_kw=42,
                session_id="duplicate",
            )
        # Measured load remains within the explicit user ceiling in every mode.
        device.state, device.power = 2, 40
        await device.status()
        async with asyncio.timeout(1):
            while guard.phase != "charging":
                await asyncio.sleep(0.01)
        assert guard.error is None
        device.state, device.power = 0, 0
        await device.status()
        async with asyncio.timeout(1):
            while guard.phase != "waiting":
                await asyncio.sleep(0.01)
        await server.async_command("stop")
        assert guard.phase == "stopped"
        assert device.writes == [1, 7, 5]
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [1, 2])
async def test_pv_guard_tracks_explicit_increase_and_stops_excess(mode):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = importlib.import_module(PACKAGE + ".native_session_guard")
    stop = AsyncMock(return_value=SimpleNamespace(stopped=True))
    guard = module.NativeSessionGuard(stop)
    guard.begin(4.2, mode=mode)
    guard.update_limit(11)
    guard.observe(SimpleNamespace(power_kw=10.8, charging=True))
    assert guard.phase == "charging" and guard.error is None
    guard.observe(SimpleNamespace(power_kw=11.6, charging=True))
    await guard.task
    assert "Measured power exceeded" in guard.error
    assert "Stop confirmed" in guard.error
    stop.assert_awaited_once()
    await guard.close()


@pytest.mark.parametrize("mode", [0, 1, 2])
async def test_terminal_session_releases_fast_guard_but_preserves_resumable_pv(mode):
    """Verify guard consumers using actual decoded loopback reports."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.mode = mode
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        await server.async_command("start", tenths_kw=42, session_id="terminal-test",
                                   background=True, start_mode=mode)
        async with asyncio.timeout(2):
            while server.session_guard.phase != "charging":
                await asyncio.sleep(0.01)
        # An EV pause is not a completed session, even with zero instantaneous load.
        device.power = 0
        device.state = 2
        await device.status()
        await asyncio.sleep(0.03)
        assert server.session_guard.limit == 4.2
        for state in (3, 0):
            device.state = state
            await device.status()
            await asyncio.sleep(0.03)
        if mode == 0:
            assert server.session_guard.phase == "stopped"
            assert server.session_guard.limit is None
            assert (await server.async_command("mode", mode=1)).mode == 1
            assert (await server.async_read_energy()).energy_kwh >= 0
        else:
            assert server.session_guard.phase == "waiting"
            assert server.session_guard.limit == 4.2
            with pytest.raises(ValueError, match="Stop charging"):
                await server.async_command("mode", mode=0)
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.parametrize("mode, reported_mode", [(0, 0), (1, 1), (2, 2), (0, 1), (0, 2)])
async def test_adopted_session_preserves_mode_and_pv_protection(mode, reported_mode):
    """Retain PV protection after taking over a cloud-started session."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    device.mode, device.state, device.power = mode, 2, 38
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        await server.async_command("power", tenths_kw=43)
        assert server.session_guard.mode == mode
        assert server.session_guard.limit == 4.3
        # External mode changes must not make a PV terminal report end Fast protection.
        device.mode = reported_mode
        for state in (3, 0):
            device.state, device.power = state, 0
            await device.status()
            await asyncio.sleep(0.03)
        if mode == 0 and reported_mode == 0:
            assert server.session_guard.phase == "stopped"
            assert server.session_guard.limit is None
        else:
            assert server.session_guard.phase == ("waiting" if mode != 0 else "charging")
            assert server.session_guard.limit == 4.3
            device.state, device.power = 2, 108
            await device.status()
            async with asyncio.timeout(3):
                while server.session_guard.task is None:
                    await asyncio.sleep(0.01)
                await server.session_guard.task
            assert 5 in device.writes
            assert device.power == 0
            assert "Stop confirmed" in server.session_guard.error
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,read_count,success", [
    ("settles_before_verification", 4, True),
    ("settles_before_delivery", 4, True),
    ("persistent", 3, False),
    ("active", 1, False),
    ("nonzero_power", 1, False),
    ("pending_start", 1, False),
    ("read_timeout", 2, False),
])
async def test_policy_rechecks_only_inconsistent_idle_reports(
    monkeypatch, scenario, read_count, success
):
    """Replay the observed idle/0kW/2.7A contradiction through actual Start policy."""
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    idle = protocol.NativeStatus(SERIAL, 0, 0, 4.2, 0, (0, 0, 0), (240,)*3, 0, 1, 0)
    residual = replace(idle, currents_a=(2.7, 0, 0))
    reports = {
        "settles_before_verification": [residual, residual, idle, idle],
        "settles_before_delivery": [idle, residual, residual, idle],
        "persistent": [residual]*3,
        "active": [replace(residual, state=2, power_kw=4.2)],
        "nonzero_power": [replace(idle, power_kw=0.1)],
        "pending_start": [residual],
        "read_timeout": [residual, TimeoutError("status timed out")],
    }[scenario]
    writes = []

    async def command(action, **values):
        if action == "status":
            report = reports.pop(0)
            if isinstance(report, Exception):
                raise report
            return report
        writes.append(action)
        return idle

    fake = SimpleNamespace(serial=SERIAL, async_command=AsyncMock(side_effect=command),
                           session_guard=SimpleNamespace(
                               phase="waiting" if scenario == "pending_start" else "idle"))
    adapter = adapter_module.NativeModeAdapter(fake)
    monkeypatch.setattr(adapter, "IDLE_READ_INTERVAL", 0)
    policy = policy_module.ChargeModePolicy(adapter, MemoryStore(), enabled=True)
    policy.desired_mode = 0
    policy.desired_power = 4.2
    if success or scenario == "active":
        await policy.async_start()
    else:
        with pytest.raises(policy_module.ModeVerificationError) as caught:
            await policy.async_start()
        errors = importlib.import_module(PACKAGE + ".ui_errors")
        expected_key = "operation_timeout" if scenario == "read_timeout" else "start_requires_idle"
        assert errors.operation_error(caught.value).translation_key == expected_key
    assert writes == (["start"] if success else [])
    assert sum(c.args == ("status",) for c in fake.async_command.call_args_list) == read_count
    assert not reports


@pytest.mark.asyncio
async def test_stop_supersedes_inconsistent_idle_retry(monkeypatch):
    """A Stop received during the settling delay prevents another read or Start."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    read = asyncio.Event()
    residual = protocol.NativeStatus(
        SERIAL, 0, 0, 4.2, 0, (2.7, 0, 0), (240,)*3, 0, 1, 0
    )
    writes = []

    async def command(action, **values):
        if action == "status":
            read.set()
            return residual
        writes.append(action)
        return SimpleNamespace(stopped=True)

    fake = SimpleNamespace(serial=SERIAL, async_command=AsyncMock(side_effect=command))
    adapter = adapter_module.NativeModeAdapter(fake)
    monkeypatch.setattr(adapter, "IDLE_READ_INTERVAL", 0.01)
    policy = policy_module.ChargeModePolicy(adapter, MemoryStore(), enabled=True)
    start = asyncio.create_task(policy.async_start())
    await asyncio.wait_for(read.wait(), 1)
    await policy.async_stop()
    with pytest.raises(policy_module.RequestSuperseded):
        await start
    assert writes == ["stop"]
    assert sum(c.args == ("status",) for c in fake.async_command.call_args_list) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_state,initial_power", [(1, 0), (2, 38)])
async def test_explicit_stop_rechecks_delayed_transition_without_replaying_stop(initial_state, initial_power):
    """A transitional early reply must not strand a Stop awaiting passive telemetry."""
    class DelayedStopDevice(Device):
        def __init__(self):
            super().__init__("ignore_stops")
            self.state, self.power = initial_state, initial_power
            self.stop_at = None
            self.status_reads = 0

        async def status(self):
            self.status_reads += 1
            if 5 in self.writes:
                if self.stop_at is None:
                    self.stop_at = time.monotonic() + 0.8
                if time.monotonic() >= self.stop_at:
                    self.state, self.power = 0, 0
            await super().status()

    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = DelayedStopDevice()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        result = await server.async_command("stop", timeout=4)
        assert result.stopped
        assert device.writes == [5]
        assert device.status_reads >= 4
        assert server.available
        assert server.session_guard.phase == "stopped"
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_explicit_stop_with_fresh_nonterminal_reports_is_bounded():
    """Repeated telemetry is not Stop confirmation and must not trigger replay."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ack_only")
    device.state = 1
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        with pytest.raises(TimeoutError):
            await server.async_command("stop", timeout=3)
        assert device.writes == [5]
        assert device.state == 1
        async with asyncio.timeout(1):
            while server.available:
                await asyncio.sleep(0.01)
        assert server.session_guard.phase != "stopped"
    finally:
        await server.async_close()
        await device.close()
        await task


@pytest.mark.asyncio
async def test_queued_status_timeout_does_not_break_pending_stop_or_receive_loop():
    """A status-query queue timeout is distinct from lost TCP telemetry."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device("ack_only")
    device.state = 1
    await device.connect(server.port)
    peer_task = asyncio.create_task(device.run())
    stop_task = None
    try:
        await ready(server)
        stop_task = asyncio.create_task(server.async_command("stop", timeout=4))
        async with asyncio.timeout(1):
            while 5 not in device.writes:
                await asyncio.sleep(0.01)
        with pytest.raises(TimeoutError):
            await server.async_command("status", timeout=0.1)
        assert not stop_task.done()
        assert server.available
        observed_before = server.observed_at
        device.state = 0
        await device.status()
        result = await stop_task
        assert result.stopped and server.available
        assert server.observed_at > observed_before
        assert device.writes == [5]
    finally:
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await server.async_close()
        await device.close()
        await peer_task


@pytest.mark.asyncio
async def test_closing_peer_is_unavailable_before_receive_cleanup_runs():
    """A fresh cached report must not make a fenced socket appear usable."""
    server = transport.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    task = asyncio.create_task(device.run())
    try:
        await ready(server)
        assert server.available
        server._writer.close()
        # Do not yield: verify the interval before _serve clears cached state.
        assert server.latest is not None
        assert not server.available
    finally:
        await server.async_close()
        await device.close()
        await task
