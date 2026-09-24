"""Auto start wire integrity, real socket verification and failed delivery."""

import asyncio
import importlib
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.test_native_energy import snapshot as energy_snapshot
from tests.test_native_transport import (
    PACKAGE,
    IDENTITY,
    report as status_report,
    protocol,
    transport,
)

SERIAL = "5011KHCA00000000"


def report(**kwargs):
    return status_report(serial=SERIAL, **kwargs)


configuration = importlib.import_module(PACKAGE + ".native_configuration")
policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")


def snapshot(enabled=False, scheduled=False):
    """Build an independent, privacy-free type-1 reply with actual wire offsets."""
    raw = bytearray(700)
    raw[:2] = b"\xaa\xf5"
    struct.pack_into("<HBBH", raw, 2, 700, 16, 91, 602)
    raw[8:30] = IDENTITY
    raw[34:66] = SERIAL.encode().ljust(32, b"\0")
    raw[66] = 1
    struct.pack_into("<HHH", raw, 67, 1, 1, 626)
    raw[600] = int(enabled)
    raw[602] = int(scheduled)
    struct.pack_into("<H", raw, 697, sum(raw[73:697]) & 65535)
    raw[-1] = sum(raw[6:-1]) & 255
    return bytes(raw)


def decode(raw):
    return configuration.decode_configuration(
        protocol.NativeDecoder().feed(bytes(raw))[0], SERIAL
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_snapshot_and_local_envelope_contract(enabled):
    assert decode(snapshot(enabled)) == configuration.NativeConfiguration(
        enabled, False
    )
    raw = configuration.auto_start_request(IDENTITY, 7, SERIAL, enabled)
    assert raw[:2] == b"\xaa\xf5" and len(raw) == 59
    assert int.from_bytes(raw[6:8], "little") == 0
    assert raw[31:37] == b"#BLE#:"
    assert raw[37:53] == SERIAL.encode()
    assert raw[53:57] == bytes([1, 7, 0, 16 if enabled else 17])
    assert raw[-2] == sum(raw[36:-2]) & 255 and raw[-1] == 13
    request = configuration.configuration_request(IDENTITY, 3)
    assert request[30:-1] == bytes.fromhex(
        "0000000001011b00000001040001000000"
    ) + bytes(36)


@pytest.mark.parametrize("offset", [34, 66, 67, 69, 71, 600, 602, 697])
def test_corrupt_or_unrecognized_config_rejected(offset):
    raw = bytearray(snapshot())
    raw[offset] ^= 2
    raw[-1] = sum(raw[6:-1]) & 255
    with pytest.raises(ValueError):
        decode(raw)


@pytest.mark.parametrize("field", [600, 602])
def test_unknown_flag_with_valid_checksums_is_not_false(field):
    raw = bytearray(snapshot())
    raw[field] = 2
    struct.pack_into("<H", raw, 697, sum(raw[73:697]) & 65535)
    raw[-1] = sum(raw[6:-1]) & 255
    with pytest.raises(ValueError):
        decode(raw)


class Peer:
    """Independent stream peer; do not use the production request decoder."""

    def __init__(self, enabled=False, scheduled=False, deliver=True, active=False):
        self.active = active
        self.enabled = enabled
        self.scheduled = scheduled
        self.deliver = deliver
        self.writes = []
        self.commands = []
        self.written = asyncio.Event()
        self.snapshot_hook = None
        self.snapshot_count = 0
        self.snapshot_requests = []
        self.snapshot_reply = lambda request: snapshot(self.enabled, self.scheduled)

    async def connect(self, subject):
        await subject.async_listen("127.0.0.1", 0)
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", subject.port
        )
        self.writer.write(
            report(state=2 if self.active else 0, power=42 if self.active else 0)
        )
        await self.writer.drain()
        self.task = asyncio.create_task(self.run())
        while not subject.available:
            await asyncio.sleep(0.01)

    async def run(self):
        buffer = bytearray()
        while chunk := await self.reader.read(8192):
            buffer.extend(chunk)
            while buffer:
                if buffer.startswith(b"\xaa\xf5"):
                    if len(buffer) < 4:
                        break
                    size = int.from_bytes(buffer[2:4], "little")
                    if len(buffer) < size:
                        break
                    raw = bytes(buffer[:size])
                    del buffer[:size]
                    command = int.from_bytes(raw[6:8], "little")
                    self.commands.append(command)
                    if command == 5:
                        self.snapshot_count += 1
                        self.snapshot_requests.append(raw)
                        if self.snapshot_hook is not None:
                            await self.snapshot_hook(self.snapshot_count)
                        reply = self.snapshot_reply(raw)
                        self.writer.write(reply[:180])
                        await self.writer.drain()
                        await asyncio.sleep(0.01)
                        self.writer.write(
                            reply[180:]
                            + report(
                                state=2 if self.active else 0,
                                power=42 if self.active else 0,
                            )
                        )
                        await self.writer.drain()
                    elif command == 1:
                        self.writer.write(
                            report(
                                state=2 if self.active else 0,
                                power=42 if self.active else 0,
                            )
                        )
                        await self.writer.drain()
                elif buffer.startswith(b"#BLE#"):
                    if len(buffer) < 28:
                        break
                    raw = bytes(buffer[:28])
                    del buffer[:28]
                    assert raw[6:22] == SERIAL.encode() and raw[23:25] == b"\x07\x00"
                    assert sum(raw[5:-2]) & 255 == raw[-2]
                    value = raw[25] == 16
                    self.writes.append(value)
                    if self.deliver:
                        self.enabled = value
                    self.written.set()
                elif len(buffer) < 5:
                    break
                else:
                    raise AssertionError("Unexpected wire bytes")

    async def close(self, subject):
        await subject.async_close()
        self.writer.close()
        await self.task


@pytest.mark.asyncio
@pytest.mark.parametrize("deliver", [True, False])
async def test_verified_cycle_or_rejected_delivery_never_optimistic(deliver):
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer(deliver=deliver)
    await peer.connect(subject)
    try:
        if deliver:
            assert (await subject.async_set_auto_start(True)).auto_start
            assert not (await subject.async_set_auto_start(False)).auto_start
            assert peer.writes == [True, False]
        else:
            with pytest.raises(ValueError, match="not confirmed"):
                await subject.async_set_auto_start(True)
            assert peer.writes == [True]
            assert not (await subject.async_read_configuration()).auto_start
        assert set(peer.commands) <= {0, 1, 5, 103}
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,scheduled", [(True, False), (False, True)])
async def test_idempotence_and_schedule_do_not_write(enabled, scheduled):
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer(enabled, scheduled)
    await peer.connect(subject)
    try:
        if enabled:
            assert (await subject.async_set_auto_start(True)).auto_start
        else:
            with pytest.raises(ValueError, match="schedule"):
                await subject.async_set_auto_start(True)
        assert not peer.writes and 0 not in peer.commands
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
async def test_cancel_after_write_does_not_replay_and_can_read_actual_state():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer()
    await peer.connect(subject)
    try:
        task = asyncio.create_task(subject.async_set_auto_start(True))
        await asyncio.wait_for(peer.written.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert peer.writes == [True]
        assert (await subject.async_read_configuration()).auto_start
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
async def test_auto_start_read_and_write_while_charging_leave_energy_guard_and_charge_untouched():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer(active=True)
    await peer.connect(subject)
    try:
        assert not (await subject.async_read_configuration()).auto_start
        assert (await subject.async_set_auto_start(True)).auto_start
        assert not (await subject.async_set_auto_start(False)).auto_start
        assert peer.writes == [True, False]
        assert subject.latest.charging and subject.latest.power_kw == 4.2
        with pytest.raises(ConnectionError):
            await subject.async_read_energy()
        assert set(peer.commands) <= {0, 1, 5, 103}
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
async def test_reconnect_after_write_does_not_confirm_from_another_session():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer()
    await peer.connect(subject)
    try:
        task = asyncio.create_task(subject.async_set_auto_start(True))
        await asyncio.wait_for(peer.written.wait(), 3)
        peer.writer.close()
        await peer.writer.wait_closed()
        with pytest.raises(ConnectionError):
            await task
        assert peer.writes == [True]
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
async def test_disabling_auto_start_preserves_existing_schedule():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer(enabled=True, scheduled=True)
    await peer.connect(subject)
    try:
        result = await subject.async_set_auto_start(False)
        assert not result.auto_start and result.scheduled
        assert peer.writes == [False]
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preflight", "quiet", "confirmation"])
async def test_real_policy_stop_supersedes_only_unsent_auto_start(stage):
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer()
    await peer.connect(subject)
    adapter = SimpleNamespace(stop=AsyncMock(return_value=True))
    policy = policy_module.ChargeModePolicy(adapter, None)

    async def stop():
        # The same policy entry point used by HA marks cancellation before it
        # waits for the in-flight configuration operation.
        tasks.append(asyncio.create_task(policy.async_stop()))
        await asyncio.sleep(0)

    async def supersede_on_read(index):
        if (stage == "preflight" and index == 1) or (
            stage == "confirmation" and index == 2
        ):
            await stop()

    original_quiet = subject._snapshot_quiet

    async def quiet(epoch, **kwargs):
        await original_quiet(epoch, **kwargs)
        if stage == "quiet" and peer.snapshot_count == 1 and not peer.writes:
            await stop()

    subject._snapshot_quiet = quiet
    tasks = []
    peer.snapshot_hook = supersede_on_read
    try:
        if stage != "confirmation":
            with pytest.raises(policy_module.RequestSuperseded):
                await policy.async_setting_write(
                    lambda: subject.async_set_auto_start(True)
                )
            assert not peer.writes
        else:
            result = await policy.async_setting_write(
                lambda: subject.async_set_auto_start(True)
            )
            assert result.auto_start and peer.writes == [True]
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        adapter.stop.assert_awaited_once()
        assert subject.available and not subject._energy_uncertain
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
async def test_unexpected_schedule_change_during_write_is_not_success():
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer(enabled=True, scheduled=True)
    await peer.connect(subject)

    async def change_schedule(index):
        if index == 2:
            peer.scheduled = False

    peer.snapshot_hook = change_schedule
    try:
        with pytest.raises(ValueError, match="schedule changed"):
            await subject.async_set_auto_start(False)
        assert peer.writes == [False]
        assert subject.available
    finally:
        await peer.close(subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_lost_post_write_confirmation_fences_late_replies_but_keeps_status(
    interruption,
):
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer()
    await peer.connect(subject)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_confirmation(index):
        if index == 2:
            entered.set()
            await release.wait()

    peer.snapshot_hook = hold_confirmation
    task = asyncio.create_task(subject.async_set_auto_start(True))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, 0.02)
        assert peer.writes == [True]
        assert subject._energy_uncertain and subject._energy_pending is None
        release.set()
        assert (await subject.async_command("status")).stopped
        for read in (subject.async_read_configuration, subject.async_read_energy):
            with pytest.raises(ConnectionError):
                await read()
        assert peer.snapshot_count == 2
        assert peer.writes == [True] and subject.available
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await peer.close(subject)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_block", [1, 2])
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_mixed_storage_reads_serialize_and_fence_late_cross_type_reply(
    first_block, cancel_first
):
    subject = transport.NativeTransport(SERIAL, "127.0.0.1")
    peer = Peer()
    entered = asyncio.Event()
    release = asyncio.Event()

    def reply(request):
        block = int.from_bytes(request[43:47], "little")
        if block == 1:
            return snapshot(enabled=True)
        assert block == 2
        raw = energy_snapshot()
        raw[34:66] = SERIAL.encode().ljust(32, b"\0")
        raw[-1] = sum(raw[6:-1]) & 255
        return raw

    async def hold_first(index):
        if index == 1:
            entered.set()
            await release.wait()

    peer.snapshot_reply = reply
    peer.snapshot_hook = hold_first
    await peer.connect(subject)
    reads = {1: subject.async_read_configuration, 2: subject.async_read_energy}
    first = asyncio.create_task(reads[first_block]())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(reads[3 - first_block]())
        # Keep the opposite-type reader queued beyond the normal quiet interval.
        # Releasing the first reply must preserve both typed results.
        await asyncio.sleep(0.45)
        assert not second.done()
        assert peer.snapshot_count == 1
        # The peer is waiting in its hook, so inspect the idle stream directly:
        # even an unparsed second request must not have reached the socket.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(peer.reader.read(1), 0.05)
        if cancel_first:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(second, 2)
            assert subject._energy_pending is None
            assert (await subject.async_command("status")).stopped
            assert peer.snapshot_count == 1
        else:
            release.set()
            values = await asyncio.wait_for(asyncio.gather(first, second), 3)
            by_block = dict(zip([first_block, 3 - first_block], values))
            assert by_block[1].auto_start is True
            assert by_block[2].energy_kwh == 1234.56
            assert [int.from_bytes(raw[43:47], "little")
                    for raw in peer.snapshot_requests] == [first_block, 3 - first_block]
        assert subject.available and not peer.writes
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await peer.close(subject)
