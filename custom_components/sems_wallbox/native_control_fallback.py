"""Check cloud controls before releasing the latest charging intent."""

from __future__ import annotations

import asyncio
import logging
import time

from .charge_mode_policy import RequestSuperseded
from .cloud_observation import CloudAuthenticationError
from .operation_budget import CURRENT_BUDGET, OperationBudget, async_execute

_LOGGER = logging.getLogger(__name__)
_PREFLIGHT_TIMEOUT = 20.0


class ControlFallback:
    """Use a read-only preflight, never retry an uncertain charging command.

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

    async def close(self):
        """Cancel preflight before disposing pending intent or restoring routing."""
        self.closed = True
        # Close queued intent before cancellation releases the readiness gate.
        await self.owner.pending_intent.close()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.preparing = False
