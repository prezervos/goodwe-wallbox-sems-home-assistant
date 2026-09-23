"""Continue measured-power supervision after native Start returns."""

from __future__ import annotations

import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)


class NativeSessionGuard:
    """Latch excess load and request bounded, independently confirmed Stop."""

    def __init__(self, stop, *, reduction_grace: float = 10.0) -> None:
        self._stop = stop
        self.reduction_grace = reduction_grace
        self.limit: float | None = None
        self._settling_limits: list[tuple[float, float]] = []
        self.on_change = None
        self._error: str | None = None
        self.requested_at_violation: float | None = None
        self.measured_at_violation: float | None = None
        self.task: asyncio.Task | None = None
        self._deadline_task: asyncio.Task | None = None
        self.phase = "idle"
        self.mode = 0

    @property
    def error(self) -> str | None:
        """Return the latest protection outcome, including unresolved failures."""
        return self._error

    @error.setter
    def error(self, value: str | None) -> None:
        if value != self._error:
            self._error = value
            self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                # UI listeners must never prevent the protective Stop itself.
                _LOGGER.exception("Failed to publish wallbox protection update")

    def begin(self, limit: float, *, timeout: float = 45, mode: int = 0) -> None:
        """Arm protection immediately without blocking subsequent commands.

        Args:
            limit: Explicit requested limit in kW.
            timeout: Fast-mode deadline for observing actual charging.
            mode: Native mode; PV modes may wait indefinitely without load.
        """
        self.arm(limit)
        self.mode = mode
        self.phase = "starting" if mode == 0 else "waiting"
        self._notify()
        if mode == 0:
            self._deadline_task = asyncio.create_task(self._wait_for_charging(timeout))

    async def _wait_for_charging(self, timeout: float) -> None:
        await asyncio.sleep(timeout)
        if self.phase == "starting" and self.error is None and self.limit is not None:
            self.phase = "stopping"
            self.report_issue(
                "Charging was not observed before the deadline; Stop pending",
                self.limit,
            )
            self.task = asyncio.create_task(
                self._protect("Charging was not observed before the deadline")
            )

    def _cancel_deadline(self) -> None:
        if self._deadline_task is not None and not self._deadline_task.done():
            self._deadline_task.cancel()

    def report_issue(self, message: str, requested=None, measured=None) -> None:
        """Publish protection evidence before sending corrective commands.

        Args:
            message: Current protection outcome, without claiming an unverified Stop.
            requested: Explicit requested limit in kW, if known.
            measured: Observed power in kW, if known.
        """
        self.requested_at_violation = requested
        self.measured_at_violation = measured
        self.error = message

    def arm(self, limit: float) -> None:
        """Supervise a newly verified session at the explicit requested limit."""
        if self.task is not None and not self.task.done():
            raise RuntimeError("Protective Stop is still pending")
        self._cancel_deadline()
        self.mode = 0
        self.limit = limit
        self._settling_limits.clear()
        self.requested_at_violation = self.measured_at_violation = None
        self.error = None

    def adopt(self, limit: float, previous_ceiling: float, *, mode: int) -> None:
        """Supervise a power change in a session already charging before TCP Start.

        Args:
            limit: Newly requested HA ceiling in kW.
            previous_ceiling: Observed pre-command ceiling for reduction settling.
            mode: Verified current mode; PV sessions may resume after zero load.
        """
        self.arm(previous_ceiling)
        self.mode = mode
        self.phase = "charging"
        self.update_limit(limit)
        self._notify()

    def update_limit(self, limit: float) -> None:
        """Apply explicit intent without prolonging earlier reduction allowances.

        Args:
            limit: Newly requested power ceiling in kW.
        """
        if self.limit is None or limit == self.limit:
            return
        now = time.monotonic()
        self._settling_limits = [
            (ceiling, until) for ceiling, until in self._settling_limits if until > now
        ]
        if limit < self.limit:
            self._settling_limits.append((self.limit, now + self.reduction_grace))
        self.limit = limit

    def observe(self, state) -> None:
        """Check measured load without treating the reported setpoint as proof."""
        if self.limit is None or self.error is not None:
            return
        now = time.monotonic()
        self._settling_limits = [
            (ceiling, until) for ceiling, until in self._settling_limits if until > now
        ]
        ceiling = max([self.limit, *(value for value, _ in self._settling_limits)])
        if state.power_kw > ceiling + 0.5:
            self.phase = "stopping"
            self._cancel_deadline()
            reason = "Measured power exceeded the session limit"
            self.report_issue(reason + "; Stop pending", self.limit, state.power_kw)
            self.task = asyncio.create_task(self._protect(reason))
        elif self.phase in ("starting", "waiting") and getattr(
            state, "charging", False
        ):
            self.phase = "charging"
            self._cancel_deadline()
            self._notify()

        elif (
            self.mode == 0 and self.phase == "charging" and state.mode == 0
            and state.state == 3 and state.stopped
        ):
            # A verified terminal report ends Fast intent. Zero load or idle
            # alone can be an EV pause and must retain supervision.
            self.disarm()
        elif self.mode != 0 and self.phase == "charging" and state.power_kw == 0:
            self.phase = "waiting"
            self._notify()

    async def _protect(
        self, reason="Measured power exceeded the session limit"
    ) -> None:
        try:
            # The transport independently confirms stopped state, zero power and
            # zero phase currents. A delivery failure is never reported as Stop.
            state = await self._stop()
            if not state.stopped:
                raise RuntimeError("Stop did not return a stopped observation")
            self.phase = "stopped"
            self.limit = None
            self.error = reason + "; Stop confirmed"
        except (ConnectionError, TimeoutError, ValueError, RuntimeError) as exc:
            self.phase = "unverified"
            self.error = "Power protection could not confirm Stop: " + str(exc)

    def disarm(self) -> None:
        """Release supervision after an explicit independently confirmed Stop."""
        self.limit = None
        self._cancel_deadline()
        self.phase = "stopped"
        self._notify()

    async def close(self, *, expected: bool = False) -> None:
        """End transport ownership while retaining any unresolved safety error."""
        if self.limit is not None and (
            (self.error is None and not expected)
            or self.task is not None
            and not self.task.done()
        ):
            self.error = "TCP supervision ended; current charging state is unverified"
        self.limit = None
        self._cancel_deadline()
        if self._deadline_task is not None:
            await asyncio.gather(self._deadline_task, return_exceptions=True)
        if self.phase in ("starting", "waiting", "charging", "stopping"):
            self.phase = "handover" if expected and self.error is None else "unverified"
            self._notify()
        if self.task is not None and self.task is not asyncio.current_task():
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
