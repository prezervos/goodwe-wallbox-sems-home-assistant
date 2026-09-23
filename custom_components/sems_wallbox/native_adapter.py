"""Adapt native Socket A transport to the remembered-mode Start policy."""

from __future__ import annotations

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


class NativeModeAdapter:
    """Use independent TCP observations for every charging-policy operation."""

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

    async def read(self) -> ModeObservation:
        """Request fresh native telemetry without consulting optimistic HA state."""
        state = await self._command("status")
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
        before = await self._command("status")
        requested = before.limit_kw if power is None else power
        if not before.stopped or before.mode not in (0, 1, 2):
            raise ModeVerificationError(
                "Start requires a stopped wallbox and a supported mode"
            )
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
