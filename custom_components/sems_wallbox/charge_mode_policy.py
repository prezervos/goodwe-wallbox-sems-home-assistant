"""Persist explicit charging-mode intent and verify it before HA Start."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, replace
from functools import wraps

from .operation_budget import BudgetCancelled, CURRENT_BUDGET, OperationBudget

CONF_REMEMBER_MODE = "remember_charge_mode"
MODE_OPTIONS = {0: "Fast", 1: "PV priority", 2: "PV and battery"}


class ModeVerificationError(RuntimeError):
    """The requested operation could not be verified safely."""


class RequestSuperseded(ModeVerificationError):
    """Preparation was superseded before any Start could be delivered."""


@dataclass(frozen=True)
class ModeObservation:
    """Independent transport read, never an optimistic coordinator value."""

    mode: int
    power: float | None = None
    minimum_power: float | None = None
    maximum_power: float | None = None
    report_marker: str | None = None
    requires_advance: bool = False
    active: bool = False


class ChargeModePolicy:
    """Coordinate mode selection, persistence, Start and Stop for one device.

    The adapter provides async read, write_mode, start and stop methods. The
    store provides async_load/async_save. Neither construction nor load writes
    to the device. Intent and confirmed observations are deliberately separate.
    """

    def __init__(
        self,
        adapter,
        store,
        *,
        enabled=False,
        remember_mode=True,
        initial_mode=None,
        timeout=60.0,
        interval=5.0,
    ):
        if initial_mode is not None and (
            type(initial_mode) is not int or initial_mode not in MODE_OPTIONS
        ):
            raise ValueError("Invalid initial charging mode")
        self.adapter = adapter
        self.store = store
        self.enabled = enabled
        self.remember_mode = remember_mode
        self.desired_mode = initial_mode
        self.desired_power = None
        self.timeout = timeout
        self.interval = interval
        self._version = 0
        self._start_intent_version = 0
        self._pending_power_request = None
        self._closed = False
        self._starting = False
        self._active_start_intent = None
        self._lock = asyncio.Lock()
        self._budget = None

    async def async_load(self):
        """Restore saved intent without touching the wallbox."""
        saved = await self.store.async_load()
        if saved is not None:
            mode = saved.get("mode")
            if mode is not None and (type(mode) is not int or mode not in MODE_OPTIONS):
                raise ModeVerificationError("Stored charging mode is invalid")
            self.desired_mode = mode
            if "power" in saved:
                self.desired_power = self._valid_power(saved["power"])

    @staticmethod
    def _valid_power(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModeVerificationError("Invalid saved power limit")
        if not math.isfinite(value) or value <= 0:
            raise ModeVerificationError("Invalid saved power limit")
        return float(value)

    async def _save_intent(self):
        data = {"mode": self.desired_mode}
        if self.desired_power is not None:
            data["power"] = self.desired_power
        await self.store.async_save(data)

    async def async_seed_power(self, value):
        """Import the existing HA limit once; telemetry never replaces saved intent."""
        if self.desired_power is None and value is not None:
            self.desired_power = self._valid_power(value)
            await self._save_intent()

    def invalidate(self, *, cancel_start=True):
        """Invalidate preparation; power-only changes retain explicit Start intent."""
        self._version += 1
        if self._budget is not None:
            self._budget.cancelled.set()
        if cancel_start:
            self._start_intent_version += 1

    def _check(self, version):
        if self._closed or self._version != version:
            raise RequestSuperseded("Charging request superseded; Start was not sent")

    async def _run_budgeted(self, operation, *, timeout_message="Operation budget expired"):
        """Bound preparation and signal executor retries without orphaning writes."""
        budget = OperationBudget(self.timeout)
        self._budget = budget
        token = CURRENT_BUDGET.set(budget)
        scope = asyncio.timeout(self.timeout)
        try:
            async with scope:
                return await operation()
        except BudgetCancelled as error:
            raise RequestSuperseded("Charging request superseded; Start was not sent") from error
        except TimeoutError as error:
            raise ModeVerificationError(
                timeout_message if scope.expired() else
                str(error) or "Wallbox response timed out"
            ) from error
        finally:
            budget.cancelled.set()
            CURRENT_BUDGET.reset(token)
            self._budget = None

    async def _verify(self, target, version, *, allow_active=False):
        return await self._run_budgeted(
            lambda: self._verify_inner(target, version, allow_active=allow_active),
            timeout_message="No fresh confirmation of charging mode; Start was not sent",
        )

    async def _verify_inner(self, target, version, *, allow_active=False):
        self._check(version)
        deadline = time.monotonic() + self.timeout
        before = await self.adapter.read()
        self._check(version)
        if time.monotonic() >= deadline:
            raise ModeVerificationError("Mode read exceeded verification deadline")
        if target is None:
            target = before.mode
        if type(target) is not int or target not in MODE_OPTIONS:
            raise ModeVerificationError("Device did not report a valid charging mode")
        if before.active and not allow_active:
            raise ModeVerificationError(
                "Wallbox already active; automatic mode change refused"
            )
        preserve_power = (
            target == 0
            or getattr(self.adapter, "preserves_power_all_modes", False) is True
        )
        requested = (
            replace(before, power=self.desired_power)
            if preserve_power and self.desired_power is not None
            else before
        )
        power_mismatch = (
            preserve_power
            and self.desired_power is not None
            and (
                before.power is None or abs(before.power - self.desired_power) >= 0.001
            )
        )
        if before.mode != target or power_mismatch:
            if await self.adapter.write_mode(target, requested) is not True:
                raise ModeVerificationError(
                    "Wallbox rejected the requested charging mode"
                )
            self._check(version)
        elif not before.requires_advance:
            return
        while time.monotonic() < deadline:
            observed = await self.adapter.read()
            self._check(version)
            if time.monotonic() >= deadline:
                break
            advanced = not observed.requires_advance or (
                before.report_marker is not None
                and observed.report_marker is not None
                and observed.report_marker.split(":", 1)[0]
                == before.report_marker.split(":", 1)[0]
                and observed.report_marker > before.report_marker
            )
            if observed.active and not allow_active:
                raise ModeVerificationError(
                    "Wallbox became active during mode verification"
                )
            power_preserved = (
                not preserve_power
                or requested.power is None
                or (
                    observed.power is not None
                    and abs(observed.power - requested.power) < 0.001
                )
            )
            if observed.mode == target and advanced and power_preserved:
                return
            await asyncio.sleep(self.interval)
        raise ModeVerificationError(
            "No fresh confirmation of charging mode; Start was not sent"
        )

    async def async_adopt_current_mode(self):
        """Seed restoration from reported mode without writing to the wallbox."""
        self.invalidate()
        version = self._version
        async with self._lock:
            self._check(version)

            async def capture():
                observation = await self.adapter.read()
                self._check(version)
                if type(observation.mode) is not int or observation.mode not in MODE_OPTIONS:
                    raise ModeVerificationError("Device did not report a valid charging mode")
                previous = self.desired_mode
                self.desired_mode = observation.mode
                try:
                    await self._save_intent()
                except BaseException:
                    # Restore memory on cancellation or failed persistence as well.
                    self.desired_mode = previous
                    raise

            await self._run_budgeted(capture)

    async def async_remember_mode(self, mode):
        """Persist an explicit selection without enabling Start restoration."""
        if type(mode) is not int or mode not in MODE_OPTIONS:
            raise ValueError("Invalid charging mode")
        self.invalidate()
        version = self._version
        async with self._lock:
            self._check(version)
            self.desired_mode = mode
            await self._save_intent()
            self._check(version)

    async def async_select_mode(self, mode):
        """Remember explicit HA intent, then apply and verify it.

        Failed application retains intent, but never reports it as confirmed.
        A newer selection or Stop invalidates any pending Start preparation.
        """
        if type(mode) is not int or mode not in MODE_OPTIONS:
            raise ValueError("Invalid charging mode")
        self.invalidate()
        version = self._version
        async with self._lock:
            self._check(version)
            self.desired_mode = mode
            await self._save_intent()
            self._check(version)
            await self._verify(mode, version, allow_active=True)

    async def async_start(self):
        """Verify saved mode and send one Start, rejecting duplicate requests."""
        if self._starting:
            raise ModeVerificationError("A Start request is already pending")
        self._starting = True
        start_intent = self._start_intent_version
        self._active_start_intent = start_intent
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                version = self._version
                try:
                    async with self._lock:
                        if self._closed or start_intent != self._start_intent_version:
                            raise RequestSuperseded(
                                "Charging request superseded; Start was not sent"
                            )
                        await self._verify(
                            self.desired_mode if self.remember_mode else None, version
                        )
                        self._check(version)
                        # Keep cancellation active through executor and shared-lock waits,
                        # including the last check before the mutating request.
                        if await self._run_budgeted(self.adapter.start) is not True:
                            raise ModeVerificationError(
                                "Start was not acknowledged; it was not retried"
                            )
                        return
                except RequestSuperseded:
                    # Only this typed pre-delivery outcome permits preparation again.
                    # Timeout, uncertain delivery and actual Start failures never retry.
                    if self._closed or start_intent != self._start_intent_version:
                        raise
                    if time.monotonic() >= deadline:
                        raise ModeVerificationError(
                            "Power kept changing; Start preparation expired"
                        )
                    # Release the lock so queued power intent is applied first.
                    await asyncio.sleep(0)
        finally:
            self._starting = False
            self._active_start_intent = None

    async def async_stop(self):
        """Cancel pending Start preparation and serialize a single Stop."""
        self.invalidate()
        async with self._lock:
            if await self.adapter.stop() is not True:
                raise ModeVerificationError(
                    "Stop was not acknowledged; check actual device state"
                )

    async def async_setting_write(
        self, operation, *, desired_mode=None, desired_power=None
    ):
        """Serialize another setting write with mode preparation and Start.

        Args:
            operation: Async callable performing the existing entity write.
            desired_mode: Explicit mode implied by the action, if any.
            desired_power: Explicit HA power request, never device telemetry.
        """
        if desired_power is not None:
            desired_power = self._valid_power(desired_power)
            latest_power = (
                self._pending_power_request[1]
                if (
                    self._pending_power_request is not None
                    and self._pending_power_request[0] == self._version
                )
                else self.desired_power
            )
            if (
                self._starting
                and self._active_start_intent == self._start_intent_version
                and desired_power == latest_power
                and desired_mode in (None, self.desired_mode)
            ):
                # Repeated automation values must not starve identical preparation.
                return
        self.invalidate(cancel_start=desired_power is None)
        version = self._version
        marker = (version, desired_power) if desired_power is not None else None
        if marker is not None:
            self._pending_power_request = marker
        try:
            async with self._lock:
                self._check(version)
                if desired_mode is not None:
                    self.desired_mode = desired_mode
                if desired_power is not None:
                    self.desired_power = desired_power
                if desired_mode is not None or desired_power is not None:
                    await self._save_intent()
                    self._check(version)
                return await self._run_budgeted(operation)
        finally:
            if marker is not None and self._pending_power_request == marker:
                self._pending_power_request = None

    async def async_close(self):
        """Prevent late Start on reload and await an in-flight transport call."""
        self._closed = True
        self.invalidate()
        async with self._lock:
            pass


async def async_apply_policy(coordinator, operation, *args):
    """Use the optional policy, translating failures into a visible HA error."""
    policy = getattr(coordinator, "charge_mode_policy", None)
    if policy is None:
        return False
    from .ui_errors import operation_error

    try:
        if not policy.enabled:
            if operation == "select_mode":
                await policy.async_remember_mode(*args)
            return False
        await getattr(policy, "async_" + operation)(*args)
    except ModeVerificationError as err:
        raise operation_error(err) from err
    coordinator.schedule_delayed_refresh(1.0)
    return True


def mode_setting_write(function=None, *, desired_mode=None, remember_power=False):
    """Serialize legacy setting writes when the optional policy is enabled."""
    if function is None:
        return lambda selected: mode_setting_write(
            selected, desired_mode=desired_mode, remember_power=remember_power
        )

    @wraps(function)
    async def wrapped(entity, *args, **kwargs):
        policy = getattr(entity.coordinator, "charge_mode_policy", None)
        if policy is None or not policy.enabled:
            return await function(entity, *args, **kwargs)
        from .ui_errors import operation_error

        try:
            return await policy.async_setting_write(
                lambda: function(entity, *args, **kwargs),
                desired_mode=desired_mode,
                desired_power=(args[0] if args else kwargs["value"])
                if (remember_power or getattr(entity, "_remember_charge_power", False))
                else None,
            )
        except ModeVerificationError as err:
            raise operation_error(err) from err

    return wrapped
