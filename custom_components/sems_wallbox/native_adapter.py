"""Adapt native Socket A transport to the remembered-mode Start policy."""

from __future__ import annotations

import asyncio
import uuid

from .charge_mode_policy import (
    ModeObservation,
    ModeVerificationError,
    RequestSuperseded,
)
from .native_power_limits import power_tenths
from .native_transport import (
    NativePowerLimitError,
    NativeStartSuperseded,
    NativeTransport,
)
from .operation_budget import request_timeout


class NativeModeAdapter:
    """Use independent TCP observations for every charging-policy operation."""

    IDLE_READ_RETRIES = 2
    IDLE_READ_INTERVAL = 1.0
    IDLE_READ_TIMEOUT = 2.0

    def __init__(self, transport: NativeTransport) -> None:
        self.transport = transport

    def _power_tenths(self, value):
        try:
            return power_tenths(value, self.transport.serial)
        except ValueError as exc:
            raise ModeVerificationError(str(exc)) from exc

    async def _command(self, action: str, **values):
        try:
            return await self.transport.async_command(action, **values)
        except NativeStartSuperseded as exc:
            raise RequestSuperseded(str(exc)) from exc
        except (
            ConnectionError,
            TimeoutError,
            ValueError,
            NativePowerLimitError,
        ) as exc:
            raise ModeVerificationError(
                str(exc) or "Native command could not be verified"
            ) from exc

    async def _read_consistent_status(self):
        """Recheck idle/zero-power reports with residual current before acting.

        Only status reads are retried. Active states, nonzero power and a pending
        supervised Start remain authoritative. Two bounded reads allow transient
        phase measurements to settle without accepting an inconsistent idle report.
        """
        state = await self._command("status")
        for _ in range(self.IDLE_READ_RETRIES):
            phase = getattr(getattr(self.transport, "session_guard", None), "phase", None)
            if (
                state.stopped
                or state.state not in (0, 3)
                or state.power_kw != 0
                or not any(state.currents_a)
                or phase in ("starting", "waiting")
            ):
                break
            # A newer Stop/power intent must fence the next read as well as Start.
            request_timeout(self.IDLE_READ_TIMEOUT)
            await asyncio.sleep(self.IDLE_READ_INTERVAL)
            state = await self._command(
                "status", timeout=request_timeout(self.IDLE_READ_TIMEOUT)
            )
        return state

    async def read(self) -> ModeObservation:
        """Request fresh native telemetry without consulting optimistic HA state."""
        state = await self._read_consistent_status()
        return ModeObservation(
            state.mode,
            state.limit_kw,
            None,
            None,
            None,
            False,
            not state.stopped
            or getattr(getattr(self.transport, "session_guard", None), "phase", None)
            in ("starting", "waiting"),
            charging=state.state == 2 and state.power_kw > 0 and any(state.currents_a),
        )

    preserves_power_all_modes = True

    async def write_mode(self, mode: int, before: ModeObservation) -> bool:
        """Restore the explicit HA ceiling after any stopped mode change."""
        if before.active:
            raise ModeVerificationError(
                "Stop charging before changing native mode settings"
            )
        tenths = self._power_tenths(before.power)
        if before.mode != mode:
            await self._command("mode", mode=mode)
        state = await self._command("power", tenths_kw=tenths)
        return state.mode == mode and state.limit_kw == before.power

    async def start(self, *, power: float | None = None, start_allowed=None) -> bool:
        """Preserve the selected mode and restore the explicit saved ceiling."""
        before = await self._read_consistent_status()
        requested = before.limit_kw if power is None else power
        if not before.stopped:
            raise ModeVerificationError("Wallbox stop state not confirmed; Start was not sent")
        if before.mode not in (0, 1, 2):
            raise ModeVerificationError("Start requires a supported charging mode")
        values = {"tenths_kw": self._power_tenths(requested)}
        await self._command(
            "start",
            **values,
            session_id="ha-" + uuid.uuid4().hex[:28],
            timeout=60,
            background=True,
            start_allowed=start_allowed,
            start_mode=before.mode,
        )
        # Accepted delivery is separate from observed charging in the coordinator.
        return True

    async def stop(self) -> bool:
        """Stop once and confirm zero phase currents and measured power."""
        return (await self._command("stop")).stopped
