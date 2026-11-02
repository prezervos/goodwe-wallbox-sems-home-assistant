"""Verify cloud control routing without replaying uncertain configuration writes."""

from __future__ import annotations

import asyncio
import logging
import time

from .charge_mode_policy import ModeVerificationError, RequestSuperseded
from .cloud_observation import CloudAuthenticationError
from .cloud_command import CloudCommandError, CloudIdentityError, CloudPreparationUnavailable
from .write_confirmation import matches
from .operation_budget import CURRENT_BUDGET, OperationBudget, async_execute

_LOGGER = logging.getLogger(__name__)
_PREFLIGHT_TIMEOUT = 20.0
_START_RECONCILIATION = 15.0


class ControlFallback:
    """Verify control routing and recover only after independent local state checks.

    Args:
        owner: Native coordinator owning the existing intent queue and write lock.
    """

    def __init__(self, owner):
        self.owner = owner
        self.task = None
        self.preparing = False
        self.closed = False
        self.error = None

    def submit(self, value, operation, *, key="charging"):
        """Verify the route for a control with an implemented TCP operation.

        Args:
            value: Validated latest user choice.
            operation: Existing operation selecting its transport at execution time.
            key: Supported logical control, coalesced by LatestIntent.

        Returns:
            Whether the request was retained for route verification.
        """
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
            owner.pending_intent.submit(key, value, operation, force=True)
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
                healthy = data is not None
            except CloudAuthenticationError:
                fallback.blocked = True
                fallback.reason = "authentication_failed"
                owner.entry.async_start_reauth(owner.hass)
                raise
            except CloudIdentityError:
                raise
            except (OSError, ValueError, RuntimeError):
                healthy = False
            self._check(epoch)
            if healthy and (not isinstance(data, dict) or data.get("sn") != owner.serial):
                raise ModeVerificationError("Missing or mismatched cloud configuration")
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

    async def recover_control(self, error, request, version, *, key="charging"):
        """Recover an unsent preparation, or reconcile an uncertain charging command.

        Args:
            error: Original service failure, possibly wrapped for HA translation.
            request: The still-current deferred control request.
            version: Latest-intent generation captured before cloud dispatch.
            key: Logical control with a verified native TCP implementation.

        Returns:
            True if the requested state was observed or a verified TCP operation completed.
            False if this failure is ineligible for automatic transport recovery.
        """
        cause = error
        while cause is not None and not isinstance(cause, (CloudCommandError, CloudPreparationUnavailable)):
            cause = cause.__cause__
        owner = self.owner
        fallback = owner.automatic_fallback
        unsent = isinstance(cause, CloudPreparationUnavailable)
        action = "start" if request.value is True else "stop" if request.value is False else None
        uncertain = (
            key == "charging" and isinstance(cause, CloudCommandError)
            and cause.cloud_command_uncertain and cause.action == action
            and cause.code in ("R0305", "C0001", "transport_error", "B0001")
        )
        if (key not in ("charging", "mode", "power", "minimum_power", "auto_start")
                or not (unsent or uncertain)
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
                raise RequestSuperseded("Cloud control recovery was superseded")

        check()
        # Reuse the existing readback scheduler instead of starting parallel HTTP
        # polling. Stop proceeds directly to an independent TCP read so a long
        # cloud observation wait cannot consume its remaining handover budget.
        deadline = time.monotonic() + (0 if unsent or request.value is False else _START_RECONCILIATION)
        while time.monotonic() < deadline:
            check()
            values = (owner.data or {}).get(owner.serial, {})
            if owner.last_update_success and matches("charging", request.value, values):
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

        _LOGGER.warning("Cloud control unavailable; verifying native TCP before recovery")
        await owner.charge_mode_policy.async_setting_write(handover)
        epoch = owner.routing_epoch
        check()
        await owner.async_refresh()
        check()
        if not owner.local or not owner.last_update_success or not owner.transport.available:
            raise ConnectionError("TCP control recovery could not verify the device")
        fallback.reason = "cloud_control_unavailable"
        fallback.trial_deadline = None
        fallback.next_attempt = time.monotonic() + fallback.return_delay
        status = await owner.transport.async_command("status")
        check()
        if key != "charging":
            # Only a typed preparation failure can reach this branch; no cloud
            # setting write was attempted. The normal TCP operation revalidates it.
            await request.operation()
            return True
        if request.value is False:
            if not status.stopped:
                await request.operation()
            return True
        if status.charging:
            return True
        # Owner-confirmed original HCA cable cycles identify idle state 0 with
        # connection 1 as connected (see observed_state.vehicle_state). Code 2
        # is not a verified native idle connection flag; do not reuse Modbus or
        # SEMS+ mappings here. Zero power alone cannot authorize a new Start.
        if (not status.stopped or status.fault_code != 0
                or status.state != 0 or status.connection != 1):
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
