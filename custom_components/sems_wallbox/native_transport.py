"""Async native TCP listener with observed-state command confirmation.

Network redirection and ownership recovery belong to a separate manager. This
listener never changes a wallbox endpoint or replays Start/settings. Protective
Stop alone may be retried up to three times within bounded recovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from collections.abc import Callable

from .native_faults import decode_fault_report
from .native_energy import NativeEnergy, decode_energy, energy_request
from .native_configuration import (
    NativeConfiguration, auto_start_request, configuration_request, decode_configuration,
)
from .native_power_limits import power_tenths
from .operation_budget import CURRENT_BUDGET
from .native_protocol import (
    NativeDecoder,
    NativeStatus,
    acknowledgement,
    check_serial,
    decode_status,
    encode_command,
    identifies,
    login,
)
from .native_session_guard import NativeSessionGuard
from .native_start_verification import StartObservationWindow

_LOGGER = logging.getLogger(__name__)


class NativePowerLimitError(RuntimeError):
    """A Start exceeded its requested ceiling; a fresh status confirmed Stop."""


class NativeStartSuperseded(ValueError):
    """A newer explicit intent cancelled preparation before Start delivery."""


class NativeTransport:
    """Own one configured peer and serialize commands across session epochs."""

    def __init__(self, serial: str, peer: str, *, stale_after: float = 45) -> None:
        check_serial(serial)
        self.expected_disconnect = False
        self.serial = serial
        self.peer = peer
        self.stale_after = stale_after
        self.latest: NativeStatus | None = None
        self.observed_at = 0.0
        self._fault_report = None
        self._fault_observed_at = None
        self._fault_rejected = 0
        self.on_observation = None
        self.epoch = 0
        self._identity: bytes | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._pending: asyncio.Future | None = None
        self._predicate: Callable[[NativeStatus], bool] | None = None
        self._closed = False
        self.accepting = True
        self._decoder: NativeDecoder | None = None
        self._last_receive = 0.0
        self._queued = 0
        self._energy_pending = None
        self._snapshot_decoder = decode_energy
        self._energy_uncertain = False
        self._start_limit: float | None = None
        self._safety_stop = False
        self._safety_stop_attempts = 0
        self._safety_stop_at = 0.0
        self._start_window = StartObservationWindow()
        self.session_guard = NativeSessionGuard(self._guard_stop)

    async def _guard_stop(self) -> NativeStatus:
        """Use the normal serialized wire path for post-Start protection."""
        return await self.async_command("stop", timeout=20, protective=True)

    @property
    def available(self) -> bool:
        """Return whether a current peer has recently reported valid telemetry."""
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self.latest is not None
            and time.monotonic() - self.observed_at < self.stale_after
        )

    @property
    def port(self) -> int:
        """Return the actual listener port, including an ephemeral test port."""
        if self._server is None:
            raise RuntimeError("Native listener is not running")
        return self._server.sockets[0].getsockname()[1]

    async def async_listen(self, host: str, port: int) -> None:
        """Start listening without changing device settings.

        Args:
            host: Local bind address.
            port: Listener port; zero allocates an ephemeral test port.
        """
        if self._server is not None or self._closed:
            raise RuntimeError("Native listener already started or closed")
        self._server = await asyncio.start_server(self._accept, host, port)

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _invalidate(self, reason: str) -> None:
        self.epoch += 1
        self.latest = None
        self._identity = None
        self._fault_report = None
        self._fault_observed_at = None
        self._fault_rejected = 0
        self._energy_uncertain = False
        if self._energy_pending is not None and not self._energy_pending.done():
            self._energy_pending.set_exception(ConnectionError(reason))
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(ConnectionError(reason))

    def fault_diagnostics(self, *, active: bool) -> dict:
        """Return cached report metadata without polling or exposing identifiers.

        Args:
            active: Whether TCP is selected and no transport switch is underway.

        Returns:
            Last observation and its age, never an assertion of current health.
        """
        visible = active and self._writer is not None and self._identity is not None
        report = self._fault_report if visible else None
        return {
            "reference_mapping": "original_hca_3_0",
            "complete_fault_coverage": False,
            "session_usable": bool(visible and self.available),
            "received": report is not None,
            "age_seconds": max(0, time.monotonic() - self._fault_observed_at)
            if report is not None else None,
            "rejected_reports": self._fault_rejected if visible else 0,
            "last_report": asdict(report) if report is not None else None,
        }

    async def _send(self, action: str, **values) -> None:
        if self._writer is None or self._identity is None:
            raise ConnectionError("No enrolled native session")
        self._sequence = (self._sequence + 1) & 255
        self._writer.write(
            encode_command(action, self._identity, self._sequence, **values)
        )
        await self._writer.drain()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if (
            self._closed
            or not self.accepting
            or writer.get_extra_info("peername")[0] != self.peer
            or self._writer is not None
        ):
            _LOGGER.debug(
                "Rejected native peer %s", writer.get_extra_info("peername")[0]
            )
            writer.close()
            await writer.wait_closed()
            return
        self._writer = writer
        self._invalidate("Native peer connected; old commands invalidated")
        decoder = self._decoder = NativeDecoder()
        try:
            while not self._closed:
                data = await asyncio.wait_for(reader.read(8192), self.stale_after)
                if not data:
                    break
                self._last_receive = time.monotonic()
                for packet in decoder.feed(data):
                    identifying = packet.command in (102, 104, 106, 202, 2104)
                    if identifying and not identifies(packet, self.serial):
                        raise ValueError("Unexpected native device identity")
                    if self._identity is None and not identifying:
                        continue
                    if self._identity is not None and packet.identity != self._identity:
                        raise ValueError("Native session envelope changed")
                    first = self._identity is None
                    if packet.command == 106 and not first:
                        self._invalidate("Device registered again; delivery uncertain")
                    self._identity = packet.identity
                    if packet.command == 202 and first:
                        writer.write(login(packet.identity))
                    ack = acknowledgement(packet)
                    if ack is not None:
                        writer.write(ack)
                    await writer.drain()
                    if packet.command == 108:
                        try:
                            self._fault_report = decode_fault_report(packet, self.serial)
                            self._fault_observed_at = time.monotonic()
                        except ValueError:
                            # Optional diagnostics must not break status/control.
                            # Clear older details rather than implying they are current.
                            self._fault_report = None
                            self._fault_observed_at = None
                            self._fault_rejected += 1
                    if packet.command == 602 and self._energy_pending is not None:
                        if not self._energy_pending.done():
                            try:
                                self._energy_pending.set_result(self._snapshot_decoder(packet, self.serial))
                            except ValueError as exc:
                                # Optional storage diagnostics must not disconnect
                                # status/control or independent load supervision.
                                self._energy_pending.set_exception(exc)
                    if packet.command in (104, 2104):
                        self.latest = decode_status(packet, self.serial)
                        self.observed_at = time.monotonic()
                        self.session_guard.observe(self.latest)
                        if self.on_observation is not None:
                            try:
                                self.on_observation(self.latest, self.observed_at)
                            except Exception:
                                # UI update failures must not terminate device protection.
                                _LOGGER.exception(
                                    "Failed to publish native wallbox telemetry"
                                )
                        if self._pending is not None and not self._pending.done():
                            if (
                                self._start_limit is not None
                                and not self._safety_stop
                                and self.latest.power_kw > self._start_limit + 0.5
                            ):
                                self._safety_stop = True
                                self.session_guard.report_issue(
                                    "Measured power exceeded the Start limit; Stop pending",
                                    self._start_limit,
                                    self.latest.power_kw,
                                )
                                # The command loop sends Stop after an input quiet gap.
                                # Do not append a control frame directly to this status ACK.
                            elif self._safety_stop and self.latest.stopped:
                                self.session_guard.error = "Measured power exceeded the Start limit; Stop confirmed"
                                self._pending.set_exception(
                                    NativePowerLimitError(
                                        "Measured power exceeded the Start limit; Stop confirmed"
                                    )
                                )
                            elif (
                                not self._safety_stop
                                and self._predicate is not None
                                and self._predicate(self.latest)
                            ):
                                if self._start_limit is None:
                                    self._pending.set_result(self.latest)
                                elif self._start_window.observe(
                                    packet.command, self.observed_at, True
                                ):
                                    self._pending.set_result(self.latest)
                            elif self._start_limit is not None:
                                self._start_window.observe(
                                    packet.command, self.observed_at, False
                                )
                    elif first or packet.command == 106:
                        await self._send("status")
        except (OSError, ValueError, TimeoutError) as exc:
            # The finally block invalidates pending commands; preserve the cause
            # in diagnostics without logging frames or credentials.
            _LOGGER.debug("Native peer session ended: %s", exc)
        finally:
            if self._writer is writer:
                await self.session_guard.close(expected=self.expected_disconnect)
                self._writer = None
                self._decoder = None
                self._invalidate(
                    "Native peer disconnected; delivery uncertain, do not replay"
                )
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    def _check_start_allowed(start_allowed) -> None:
        if start_allowed is not None and not start_allowed():
            raise NativeStartSuperseded(
                "Charging request superseded; Start was not sent"
            )

    async def _wait_start_preparation(self, start_allowed) -> None:
        """Allow newer intent to interrupt a missing preparation report."""
        while not self._pending.done():
            self._check_start_allowed(start_allowed)
            await asyncio.wait({self._pending}, timeout=0.1)
        self._check_start_allowed(start_allowed)
        await self._pending

    async def _prepare_start(
        self, tenths_kw: int, epoch: int, start_allowed=None, *, mode=0
    ) -> NativeStatus:
        """Restore power under the command lock before sending Start.

        Matching telemetry confirms the reported request only. It does not prove
        effective current or firmware finalization; Start still needs load checks.
        End-state telemetry (3) cannot authorize a new Start.
        """
        self._pending = asyncio.get_running_loop().create_future()
        try:
            # A session-end report is stopped electrically but can precede the
            # firmware's effective-current reset. Observe idle before writing.
            self._predicate = lambda state: (
                state.state == 0
                and state.stopped
                and state.mode == mode
                and state.fault_code == 0
            )
            await self._send("status")
            await self._wait_start_preparation(start_allowed)
            while time.monotonic() - self._last_receive < 0.3:
                self._check_start_allowed(start_allowed)
                await asyncio.sleep(0.02)
            if epoch != self.epoch or not self.available:
                raise ConnectionError("Native session changed before power restoration")
            if not self._predicate(self.latest):
                raise ValueError("Wallbox left idle before power restoration")
            self._pending = asyncio.get_running_loop().create_future()
            self._predicate = lambda state: (
                state.state == 0
                and state.stopped
                and state.mode == mode
                and state.fault_code == 0
                and state.limit_kw == tenths_kw / 10
            )
            self._check_start_allowed(start_allowed)
            await self._send("power", tenths_kw=tenths_kw)
            await asyncio.sleep(0.3)
            await self._send("status")
            await self._wait_start_preparation(start_allowed)
            # Drain paired telemetry/ACK traffic before the next control frame.
            while time.monotonic() - self._last_receive < 0.3:
                self._check_start_allowed(start_allowed)
                await asyncio.sleep(0.02)
            if epoch != self.epoch or not self.available:
                raise ConnectionError("Native session changed during Start preparation")
            if not self._predicate(self.latest):
                raise ValueError(
                    "Wallbox left the requested idle mode during Start preparation"
                )
            return self.latest
        except NativeStartSuperseded:
            # No Start was emitted; preserve the healthy connection for newer intent.
            raise
        except BaseException:
            # An interrupted write has uncertain delivery; do not continue with Start.
            if self._writer is not None:
                self._writer.close()
            raise
        finally:
            if not self._pending.done():
                self._pending.cancel()
            self._pending = None
            self._predicate = None

    async def _stop_failed_start(self, epoch: int) -> bool:
        """Attempt Stop within an additional 20-second recovery budget.

        The caller retains the command lock. No Start is replayed, and a changed
        connection cannot authorize either a new command or confirmation.
        """
        if self._pending is not None:
            if not self._pending.done():
                self._pending.cancel()
            elif not self._pending.cancelled():
                self._pending.exception()  # Consume any earlier disconnect failure.
        self._pending = asyncio.get_running_loop().create_future()
        self._start_limit = None
        self._safety_stop = False
        attempts = self._safety_stop_attempts
        last_stop = self._safety_stop_at
        self._predicate = lambda state: attempts > 0 and state.stopped
        self.session_guard.disarm()
        self.session_guard.error = "Start was not verified; protective Stop pending"
        try:
            async with asyncio.timeout(20):
                while True:
                    if epoch != self.epoch or not self.available:
                        raise ConnectionError(
                            "Native session lost during Start recovery"
                        )
                    if self._pending.done():
                        await self._pending
                        self.session_guard.error = (
                            "Start was not verified; Stop confirmed"
                        )
                        return True
                    if attempts < 3 and time.monotonic() - last_stop >= 5:
                        while time.monotonic() - self._last_receive < 0.35:
                            await asyncio.sleep(0.02)
                        if epoch != self.epoch or not self.available:
                            raise ConnectionError(
                                "Native session changed before recovery Stop"
                            )
                        attempts += 1
                        last_stop = time.monotonic()
                        await self._send("stop")
                    done, _ = await asyncio.wait({self._pending}, timeout=2)
                    if not done:
                        await self._send("status")
        except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
            if self._pending.done() and not self._pending.cancelled():
                self._pending.exception()
            self.session_guard.error = (
                "Start was not verified; Stop remains unconfirmed: "
                + (str(exc) or type(exc).__name__)
            )
            return False

    async def _recover_failed_start(self, epoch: int) -> bool:
        """Finish bounded recovery even if the caller cancels a second time."""
        recovery = asyncio.create_task(self._stop_failed_start(epoch))
        while True:
            try:
                return await asyncio.shield(recovery)
            except asyncio.CancelledError:
                # Keep ownership until the bounded cleanup finishes. The original
                # cancellation/timeout is re-raised by async_command afterwards.
                if recovery.done():
                    return recovery.result()

    async def async_command(
        self,
        action: str,
        *,
        timeout: float = 30,
        protective: bool = False,
        background: bool = False,
        start_mode: int = 0,
        start_allowed: Callable[[], bool] | None = None,
        intent_allowed: Callable[[], bool] | None = None,
        **values,
    ) -> NativeStatus:
        """Send once and require fresh matching telemetry within a total deadline.

        Args:
            action: status, mode, power, minimum_power, start or stop.
            timeout: Queue and confirmation budget; an emitted failed Start may
                add at most 20 seconds for protective Stop recovery.
            protective: Permit up to three Stop attempts during load protection.
            start_mode: Observed mode to preserve (0 Fast, 1 PV, 2 PV and battery).
            background: Release after Start delivery; independent supervision observes
                charging. The returned state is the pre-Start observation, not proof
                of charging.
            start_allowed: Optional current-intent check before Start delivery.
            intent_allowed: Optional fence for a setting superseded while queued.
            **values: Arguments accepted by encode_command.
        Returns:
            Matching telemetry from the same session, or the pre-Start status
            when background delivery delegates verification to the session guard.
        Raises:
            ValueError: Invalid operation or value.
            ConnectionError: Unavailable/replaced peer or uncertain delivery.
            TimeoutError: No matching observation within the deadline.
        """
        if type(start_mode) is not int or start_mode not in (0, 1, 2):
            raise ValueError("Unsupported Start mode")
        if action == "start" and start_mode != 0 and not background:
            raise ValueError("PV Start requires background monitoring")
        if background and (action != "start" or "tenths_kw" not in values):
            raise ValueError("Background Start requires an explicit power limit")
        if protective and action != "stop":
            raise ValueError("Only protective Stop may be retried")
        encode_command(action, bytes(22), 0, **values)
        if action == "start" and "tenths_kw" in values:
            encode_command("power", bytes(22), 0, tenths_kw=values["tenths_kw"])
        if action in ("power", "start") and "tenths_kw" in values:
            power_tenths(values["tenths_kw"] / 10, self.serial)
        if not 0 < timeout <= 90:
            raise ValueError("Invalid native command timeout")
        if self._queued >= 16:
            raise ConnectionError("Native command queue full")
        epoch = self.epoch
        self._queued += 1
        try:
            async with asyncio.timeout(timeout), self._lock:
                if epoch != self.epoch or not self.available:
                    raise ConnectionError(
                        "A fresh unchanged native session is required"
                    )
                # Allow already buffered input to drain before setting a confirmation boundary.
                while (
                    self._decoder is not None
                    and self._decoder.buffer
                    or time.monotonic() - self._last_receive < 0.3
                ):
                    await asyncio.sleep(0.02)
                    if epoch != self.epoch or not self.available:
                        raise ConnectionError(
                            "Native session changed before transmission"
                        )
                if (
                    action not in ("status", "stop")
                    and self.session_guard.task is not None
                    and not self.session_guard.task.done()
                ):
                    raise ConnectionError("Protective Stop is pending")
                if action == "start" and self.session_guard.phase in (
                    "starting",
                    "waiting",
                ):
                    raise ValueError("A native Start is already awaiting charging")
                if intent_allowed is not None and not intent_allowed():
                    raise ValueError("Setting superseded; command was not sent")
                before = self.latest
                if action == "minimum_power" and (
                    not before.stopped or before.state != 0
                    or before.mode not in (0, 1, 2) or before.minimum_power is None
                    or self.session_guard.phase in ("starting", "waiting", "charging")
                ):
                    raise ValueError("Minimum-power write requires idle mode with a known flag")
                if action == "mode" and (
                    not before.stopped
                    or self.session_guard.phase in ("starting", "waiting", "charging")
                ):
                    raise ValueError("Stop charging before changing native mode")
                if action == "start" and (
                    not before.stopped or before.mode != start_mode
                ):
                    raise ValueError(
                        "Native Start requires idle state in the requested mode"
                    )
                if action == "start":
                    power_tenths(
                        values.get("tenths_kw", before.limit_kw * 10) / 10, self.serial
                    )
                    self._check_start_allowed(start_allowed)
                if action == "start" and ("tenths_kw" in values or start_mode != 0):
                    before = await self._prepare_start(
                        values.get("tenths_kw"), epoch, start_allowed, mode=start_mode
                    )
                if action == "start":
                    self._check_start_allowed(start_allowed)
                self._start_limit = before.limit_kw if action == "start" else None
                self._safety_stop = False
                self._safety_stop_attempts = 0
                self._safety_stop_at = 0.0
                self._start_window = StartObservationWindow()
                self._predicate = self._confirmation(action, values, before)
                self._pending = asyncio.get_running_loop().create_future()
                try:
                    if action == "power":
                        # Raise the guard ceiling before a permitted increase can
                        # appear on the receive path; reductions have bounded grace.
                        requested = values["tenths_kw"] / 10
                        if self.session_guard.limit is None and before.charging:
                            # A session may have started in cloud before TCP takeover.
                            # The first explicit local power write must also be supervised.
                            self.session_guard.adopt(
                                requested, max(before.limit_kw, before.power_kw), mode=before.mode
                            )
                        else:
                            self.session_guard.update_limit(requested)
                    if background:
                        self._start_limit = (
                            None  # Independent guard owns all load checks.
                        )
                        self.session_guard.begin(before.limit_kw, mode=start_mode)
                    await self._send(action, **values)
                    if background:
                        return before
                    if protective:
                        self._safety_stop_attempts = 1
                        self._safety_stop_at = time.monotonic()
                    if action != "status":
                        await asyncio.sleep(0.3)
                        await self._send("status")
                    # Ordinary Stop may outlive the first status query. Recheck
                    # observations without replaying the control command; the
                    # device need not push its final idle state spontaneously.
                    while (
                        action in ("start", "stop") or protective
                    ) and not self._pending.done():
                        if (
                            (self._safety_stop or protective)
                            and self._safety_stop_attempts < 3
                            and time.monotonic() - self._safety_stop_at >= 5
                        ):
                            # Match ordinary command delivery: leave the ACK receive path
                            # and wait for a quiet gap before sending protective Stop.
                            while time.monotonic() - self._last_receive < 0.35:
                                await asyncio.sleep(0.02)
                            if not self._pending.done():
                                await self._send("stop")
                                self._safety_stop_attempts += 1
                                self._safety_stop_at = time.monotonic()
                                await asyncio.sleep(0.3)
                        done, _ = await asyncio.wait({self._pending}, timeout=2)
                        if not done:
                            await self._send("status")
                    result = await self._pending
                    if action == "start":
                        self.session_guard.arm(self._start_limit)
                    elif action == "stop":
                        self.session_guard.disarm()
                    return result
                except NativePowerLimitError:
                    # Stop was independently confirmed; keep the session for recovery.
                    raise
                except BaseException:
                    # A possibly emitted Start requires bounded Stop recovery before
                    # fencing. Retain the original error; never turn failure into success.
                    stopped = action == "start" and await self._recover_failed_start(
                        epoch
                    )
                    if not stopped and self._writer is not None:
                        self._writer.close()
                    raise
                finally:
                    if self._pending is not None and not self._pending.done():
                        self._pending.cancel()
                    self._pending = None
                    self._predicate = None
                    self._start_limit = None
                    self._safety_stop = False
        finally:
            self._queued -= 1

    def _confirmation(
        self, action: str, values: dict, before: NativeStatus
    ) -> Callable[[NativeStatus], bool]:
        if action == "status":
            return lambda state: True
        if action == "mode":
            # A mode change can reset the ceiling. The adapter restores saved power.
            return lambda state: state.mode == values["mode"]
        if action == "minimum_power":
            return lambda state: (
                state.minimum_power is values["minimum_power"]
                and state.mode == before.mode and state.stopped
                and state.limit_kw == before.limit_kw
            )
        if action == "power":
            return lambda state: (
                state.mode == before.mode and state.limit_kw == values["tenths_kw"] / 10
            )
        if action == "start":
            return lambda state: (
                state.charging
                and state.fault_code == 0
                and state.mode == before.mode
                and state.limit_kw == before.limit_kw
                and state.power_kw <= before.limit_kw + 0.5
            )
        return lambda state: state.stopped

    @property
    def optional_read_busy(self) -> bool:
        """Give controls priority and avoid retrying uncertain storage responses."""
        return (self._lock.locked() or self._queued > 0 or self._energy_uncertain
                or time.monotonic() - self.observed_at > 4)

    async def async_read_energy(self, *, timeout: float = 5) -> NativeEnergy:
        """Read one cumulative block from a fresh idle session without any ACK.

        Args:
            timeout: Total wait including the shared command lock, at most 10 s.

        Returns:
            Verified lifetime counters; no cached value or synthetic zero.

        Raises:
            ValueError: Invalid timeout or corrupt snapshot.
            ConnectionError: Changed, active or previously uncertain session.
            TimeoutError: No complete valid response within the bounded wait.
        """
        return await self._async_read_snapshot(energy_request, decode_energy, timeout)

    async def async_read_configuration(self, *, timeout: float = 5) -> NativeConfiguration:
        """Read verified original-HCA settings using the shared snapshot lock."""
        return await self._async_read_snapshot(
            configuration_request, decode_configuration, timeout, idle_only=False
        )

    async def _async_read_snapshot(self, request, decoder, timeout, *, idle_only=True):
        if not 0 < timeout <= 10:
            raise ValueError("Invalid storage snapshot timeout")
        epoch = self.epoch
        async with asyncio.timeout(timeout), self._lock:
            return await self._read_snapshot_locked(request, decoder, epoch, idle_only=idle_only)

    def _require_snapshot_session(self, epoch, *, idle_only=True):
        if (epoch != self.epoch or not self.available or self._energy_uncertain
                or time.monotonic() - self.observed_at > 5):
            raise ConnectionError("Storage operation requires a fresh unchanged session")
        if idle_only and (not self.latest.stopped or self.latest.state != 0
                or self.session_guard.phase in ("starting", "waiting", "charging")):
            raise ConnectionError("Storage operation requires a fresh unchanged idle session")

    async def _snapshot_quiet(self, epoch, *, idle_only=True):
        self._require_snapshot_session(epoch, idle_only=idle_only)
        while (self._decoder is not None and self._decoder.buffer
               or time.monotonic() - self._last_receive < 0.35):
            await asyncio.sleep(0.02)
        self._require_snapshot_session(epoch, idle_only=idle_only)

    async def _read_snapshot_locked(self, request, decoder, epoch, *, idle_only=True):
        sent = False
        pending = None
        try:
            await self._snapshot_quiet(epoch, idle_only=idle_only)
            pending = self._energy_pending = asyncio.get_running_loop().create_future()
            self._snapshot_decoder = decoder
            self._sequence = (self._sequence + 1) & 255
            sent = True
            self._writer.write(request(self._identity, self._sequence))
            await self._writer.drain()
            result = await pending
            if epoch != self.epoch:
                raise ConnectionError("Storage response belongs to a previous session")
            return result
        except BaseException:
            # Responses do not echo the request sequence. After uncertain delivery,
            # disallow ALL storage snapshots until reconnect, including cancellation.
            if sent and epoch == self.epoch:
                self._energy_uncertain = True
            raise
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                elif not pending.cancelled():
                    pending.exception()
                if self._energy_pending is pending:
                    self._energy_pending = None

    async def async_set_auto_start(self, enabled: bool) -> NativeConfiguration:
        """Write once and independently verify without changing charging intent.

        Configuration snapshots are separate from idle-only cumulative energy.
        Never replay an uncertain write.

        Args:
            enabled: Requested Auto start flag.

        Returns:
            Fresh configuration confirming the request.

        Raises:
            ValueError: Invalid value, active schedule or unconfirmed write.
            ConnectionError: Stale, changed or uncertain session.
            TimeoutError: The bounded operation did not complete.
            BudgetCancelled: A newer intent superseded the request before writing.
        """
        if type(enabled) is not bool:
            raise ValueError("Auto start requires a boolean")
        epoch = self.epoch
        budget = CURRENT_BUDGET.get()
        async with asyncio.timeout(15), self._lock:
            if budget is not None:
                budget.remaining()
            before = await self._read_snapshot_locked(configuration_request, decode_configuration, epoch, idle_only=False)
            if budget is not None:
                budget.remaining()
            if before.auto_start == enabled:
                return before
            if enabled and before.scheduled:
                raise ValueError("Disable the wallbox schedule before changing Auto start")
            await self._snapshot_quiet(epoch, idle_only=False)
            # A newer Stop/setting may arrive during the preflight read or quiet
            # wait. Reject obsolete work immediately before the sole device write.
            if budget is not None:
                budget.remaining()
            self._sequence = (self._sequence + 1) & 255
            self._writer.write(auto_start_request(self._identity, self._sequence, self.serial, enabled))
            await self._writer.drain()
            await asyncio.sleep(1)
            # Once sent, finish independent readback even if a newer intent
            # supersedes this request; never turn an uncertain write into a replay.
            after = await self._read_snapshot_locked(configuration_request, decode_configuration, epoch, idle_only=False)
            if after.scheduled != before.scheduled:
                raise ValueError("Wallbox schedule changed during Auto start update")
            if after.auto_start != enabled:
                raise ValueError("Auto start change was not confirmed by the wallbox")
            return after

    async def async_disconnect(self, *, expected: bool = False) -> None:
        """Release the current client and reject reconnects until explicitly resumed."""
        self.accepting = False
        await self.session_guard.close(expected=expected)
        if self._writer is not None:
            self._writer.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def async_close(self) -> None:
        """Close the listener/peer and finish all connection tasks."""
        self._closed = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        # Python 3.14 waits for clients in Server.wait_closed(). Close readers first.
        await self.async_disconnect()
        if server is not None:
            await server.wait_closed()
