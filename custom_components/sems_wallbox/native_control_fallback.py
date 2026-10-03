"""Verify cloud control routing and reconcile uncertain Start before TCP recovery."""

from __future__ import annotations

import asyncio
import logging
import time

from .charge_mode_policy import RequestSuperseded
from .cloud_observation import CloudAuthenticationError
from .cloud_command import CloudCommandError
from .write_confirmation import matches
from .operation_budget import CURRENT_BUDGET, OperationBudget, async_execute

_LOGGER = logging.getLogger(__name__)
_PREFLIGHT_TIMEOUT = 20.0
_START_RECONCILIATION = 15.0


class ControlFallback:
    """Verify the route; recover Start only after independent local state checks.

    Args:
        owner: Native coordinator owning the existing intent queue and write lock.
    """

    def __init__(self, owner):
        self.owner = owner
        self.task = None
        self.preparing = False
        self.closed = False
        self.error = None

    def submit(self, value, operation):
        """Defer an opted-in cloud charging request until its route is verified."""
        owner = self.owner
        fallback = owner.automatic_fallback
        if (self.closed or owner._closed or owner.local or owner.cloud is None
                or not fallback.enabled or fallback.paused or fallback.blocked
                or owner.pending_intent.blackout or owner.pending_intent.pending
                or owner.pending_intent.executing is not None):
            return False
        self.preparing = True
        self.error = None
        try:
            owner.pending_intent.submit("charging", value, operation, force=True)
            self.task = owner.hass.async_create_background_task(
                self._prepare(owner.routing_epoch), "GoodWe cloud control preflight"
            )
        except BaseException:
            # Roll back the readiness gate if submission or task creation fails.
            self.preparing = False
            raise
        owner.async_update_listeners()
        return True

    def _check(self, epoch):
        owner = self.owner
        fallback = owner.automatic_fallback
        if (self.closed or owner._closed or owner.local or owner.transitioning
                or epoch != owner.routing_epoch or not fallback.enabled
                or fallback.paused or fallback.blocked
                or not owner.pending_intent.pending
                or owner.pending_intent.batch_deadline is None
                or time.monotonic() >= owner.pending_intent.batch_deadline):
            raise ConnectionError("Cloud control preflight was superseded")

    async def _prepare(self, epoch):
        owner = self.owner
        fallback = owner.automatic_fallback
        try:
            try:
                budget = OperationBudget(_PREFLIGHT_TIMEOUT)
                token = CURRENT_BUDGET.set(budget)
                try:
                    async with asyncio.timeout(_PREFLIGHT_TIMEOUT):
                        data = await async_execute(
                            owner.hass, owner.cloud.get_data_gen2, owner.serial
                        )
                finally:
                    budget.cancelled.set()
                    CURRENT_BUDGET.reset(token)
                healthy = isinstance(data, dict) and data.get("sn") == owner.serial
            except CloudAuthenticationError:
                fallback.blocked = True
                fallback.reason = "authentication_failed"
                owner.entry.async_start_reauth(owner.hass)
                raise
            except (OSError, ValueError, RuntimeError):
                healthy = False
            self._check(epoch)
            if healthy:
                return
            if time.monotonic() < fallback.next_attempt:
                raise ConnectionError("TCP handover retry is delayed")
            _LOGGER.info("Cloud control unavailable; switching to native TCP")

            started = asyncio.Event()

            async def handover():
                self._check(epoch)
                started.set()
                fallback.next_attempt = time.monotonic() + fallback.RETRY_DELAY
                await owner._set_local(True)
                await owner.connection_intent.async_automatic(True)

            # New user intent may supersede the lock wait, but not the need for
            # a usable route. Retry only before the handover has begun; never
            # repeat a potentially delivered transport or charging operation.
            async with asyncio.timeout(owner.charge_mode_policy.timeout):
                while True:
                    self._check(epoch)
                    try:
                        await owner.charge_mode_policy.async_setting_write(handover)
                        break
                    except RequestSuperseded:
                        if started.is_set() or owner.charge_mode_policy._closed:
                            raise
                        await asyncio.sleep(0)
            await owner.async_refresh()
            if (owner._closed or not owner.local or not owner.last_update_success
                    or not owner.transport.available):
                raise ConnectionError("TCP control could not be verified")
            fallback.reason = "cloud_control_unavailable"
            fallback.trial_deadline = None
            fallback.next_attempt = time.monotonic() + fallback.return_delay
            _LOGGER.info("Native TCP control verified after cloud control failure")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Background service acceptance must never hide a failed handover.
            _LOGGER.exception("Cloud control preflight or TCP handover failed")
            self.error = "cloud_control_failed"
            await owner.pending_intent._fail("operation_failed")
        finally:
            self.preparing = False
            owner.async_update_listeners()

    async def recover_start(self, error, request, version):
        """Reconcile an uncertain cloud Start before a single verified TCP attempt.

        Args:
            error: Original service failure, possibly wrapped for HA translation.
            request: The still-current deferred charging request.
            version: Latest-intent generation captured before cloud dispatch.

        Returns:
            True if charging was observed or a verified local attempt completed.
            False if this failure is ineligible for automatic transport recovery.
        """
        cause = error
        while cause is not None and not isinstance(cause, CloudCommandError):
            cause = cause.__cause__
        owner = self.owner
        fallback = owner.automatic_fallback
        if (not isinstance(cause, CloudCommandError) or cause.action != "start"
                or not cause.cloud_command_uncertain or cause.code not in ("R0305", "C0001", "transport_error")
                or owner.local or not fallback.enabled or fallback.paused or fallback.blocked):
            return False
        epoch = owner.routing_epoch
        pending = owner.pending_intent

        def check():
            if (self.closed or owner._closed or owner.transitioning
                    or owner.routing_epoch != epoch or pending.closed
                    or pending.version != version or not fallback.enabled
                    or fallback.paused or fallback.blocked
                    or pending.batch_deadline is None
                    or time.monotonic() >= pending.batch_deadline):
                raise RequestSuperseded("Cloud Start recovery was superseded")

        check()
        # Reuse the existing readback scheduler instead of starting parallel HTTP
        # polling. Observed charging cancels recovery even if its origin is unknown.
        deadline = time.monotonic() + _START_RECONCILIATION
        while time.monotonic() < deadline:
            check()
            values = (owner.data or {}).get(owner.serial, {})
            if owner.last_update_success and matches("charging", True, values):
                return True
            await asyncio.sleep(0.2)
        check()
        if time.monotonic() < fallback.next_attempt:
            return False

        async def handover():
            check()
            fallback.next_attempt = time.monotonic() + fallback.RETRY_DELAY
            await owner._set_local(True)
            await owner.connection_intent.async_automatic(True)

        _LOGGER.warning("Cloud Start was not confirmed; verifying native TCP before recovery")
        await owner.charge_mode_policy.async_setting_write(handover)
        epoch = owner.routing_epoch
        check()
        await owner.async_refresh()
        check()
        if not owner.local or not owner.last_update_success or not owner.transport.available:
            raise ConnectionError("TCP Start recovery could not verify the device")
        fallback.reason = "cloud_control_unavailable"
        fallback.trial_deadline = None
        fallback.next_attempt = time.monotonic() + fallback.return_delay
        status = await owner.transport.async_command("status")
        check()
        if status.charging:
            return True
        # Zero power alone cannot establish idle or a safe new charging session.
        if not status.stopped or status.fault_code != 0 or status.connection != 2:
            raise ConnectionError("TCP Start recovery requires confirmed idle, connected, fault-free state")
        await request.operation()
        return True

    async def close(self):
        """Cancel preflight before disposing pending intent or restoring routing."""
        self.closed = True
        # Close queued intent before cancellation releases the readiness gate.
        await self.owner.pending_intent.close()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.preparing = False
