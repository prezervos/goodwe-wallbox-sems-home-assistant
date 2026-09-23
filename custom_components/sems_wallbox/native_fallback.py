"""Bounded cloud fallback policy; all device writes use existing ownership routing."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .charge_mode_adapter import _marker
from .cloud_observation import CloudAuthenticationError
from .observed_state import charging_active

_LOGGER = logging.getLogger(__name__)


def report_age(data, zone, now):
    """Return report age in seconds, rejecting missing or future timestamps."""
    marker = _marker(data.get("lastUpdate"))
    if marker is None:
        raise ValueError("Cloud report lacks a timestamp")
    stamp = datetime.fromisoformat(marker.split(":", 1)[1])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(zone))
    age = now - stamp.timestamp()
    if age < -5:
        raise ValueError("Cloud report timestamp is in the future")
    return max(0, age)


class AutomaticFallback:
    """Debounce failure, serialize handovers and verify controlled cloud trials.

    Args:
        owner: Native coordinator owning the endpoint and control lock.
        enabled: Explicit user opt-in; manual switching pauses until reload.
    """

    FAILURE_DELAY = 90
    TRIAL_DELAY = 1800
    TRIAL_TIMEOUT = 60
    RETRY_DELAY = 300

    def __init__(self, owner, enabled=False):
        self.owner = owner
        self.enabled = enabled
        self.paused = False
        self.reason = None
        self.failures = 0
        self.failed_since = None
        self.local_failures = 0
        self.local_failed_since = None
        self.next_attempt = 0
        self.trial_deadline = None
        self.return_delay = self.TRIAL_DELAY
        self.task = None
        self.closed = False
        self.blocked = False
        self.recovery_retry = 0

    def observation(self, success, reason=None, *, blocked=False):
        """Record one completed cloud poll, without acting inside its refresh lock."""
        if success:
            self.failures = 0
            self.failed_since = None
            self.blocked = False
            if self.trial_deadline is not None:
                self.trial_deadline = None
                self.return_delay = self.TRIAL_DELAY
            self.reason = None
            return
        self.blocked = blocked
        self.reason = reason
        self.failures += 1
        if self.failed_since is None:
            self.failed_since = time.monotonic()

    def local_observation(self, success):
        """Track failed local status polls separately from cloud failures."""
        if success:
            self.local_failures = 0
            self.local_failed_since = None
        else:
            self.local_failures += 1
            if self.local_failed_since is None:
                self.local_failed_since = time.monotonic()

    def pause(self):
        """Pause automatic actions; persisted manual TCP reapplies this after reload."""
        self.paused = True
        self.reason = "manual_override"

    def start(self):
        """Start independent supervision without blocking HA startup."""
        if self.enabled and self.owner.cloud is not None:
            self.task = self.owner.hass.async_create_background_task(
                self._run(), "GoodWe automatic transport fallback"
            )

    async def close(self):
        """Cancel supervision before coordinator teardown restores ownership."""
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self):
        while not self.closed:
            delay = 5 if self.trial_deadline is None else min(
                5, max(0.05, self.trial_deadline - time.monotonic())
            )
            await asyncio.sleep(delay)
            try:
                await self.tick()
            except Exception:
                # Keep the recovery supervisor alive; never treat an unexpected
                # exception as permission to retry a write immediately.
                _LOGGER.exception("Automatic transport supervision failed")
                self.reason = "handover_failed"
                self.next_attempt = time.monotonic() + self.RETRY_DELAY

    async def tick(self):
        """Perform at most one eligible transition using the existing write lock."""
        owner = self.owner
        now = time.monotonic()
        if (not self.enabled or self.paused or self.closed or self.blocked
                or owner._closed or owner.transitioning):
            return
        if not owner.local and self.trial_deadline is not None:
            remaining = self.trial_deadline - time.monotonic()
            if remaining > 0:
                try:
                    # Do not wait for the normal idle polling interval. Bound this
                    # wait so a slow cloud call cannot consume the whole blackout.
                    async with asyncio.timeout(min(10, remaining)):
                        await owner.async_refresh()
                except TimeoutError:
                    pass
            now = time.monotonic()
            if self.trial_deadline is None:
                return
        local_failed = (owner.local and self.local_failures >= 3
                        and self.local_failed_since is not None
                        and now - self.local_failed_since >= 15)
        if now < self.next_attempt and not local_failed:
            return
        if local_failed:
            if now < self.recovery_retry:
                return
            self.recovery_retry = now + self.RETRY_DELAY
            self.reason = "local_unavailable"
            self.trial_deadline = now + self.TRIAL_TIMEOUT
            # Recovery of a failed local connection is not a discretionary trial.
            await self._handover(False, recovery=True)
            self.local_observation(True)
            self.next_attempt = 0
            return
        if owner.local:
            values = (owner.data or {}).get(owner.serial, {})
            # Unknown or stale state is not evidence that a return trial is safe.
            if (not owner.last_update_success or not owner.transport.available
                    or charging_active(values, local=True) is not False
                    or owner.transport.session_guard.phase in ("starting", "waiting")):
                return
            # Keep local controls and polling available throughout the probe.
            epoch = owner.routing_epoch
            try:
                reachable = await owner.async_cloud_preflight()
            except CloudAuthenticationError:
                self.blocked = True
                self.reason = "authentication_failed"
                owner.entry.async_start_reauth(owner.hass)
                owner.async_update_listeners()
                return
            except (OSError, ValueError, RuntimeError):
                reachable = False
            # The probe held no write lock: discard its result after any user
            # handover, shutdown or manual override, even if it succeeded.
            if (self.paused or self.closed or owner._closed or not owner.local
                    or owner.transitioning or epoch != owner.routing_epoch):
                return
            if not reachable:
                self.reason = "cloud_preflight_unavailable"
                self.next_attempt = time.monotonic() + self.RETRY_DELAY
                owner.async_update_listeners()
                return
            self.reason = "cloud_return_trial"
            self.trial_deadline = time.monotonic() + self.TRIAL_TIMEOUT
            await self._handover(False)
            self.next_attempt = 0
            return
        trial_failed = self.trial_deadline is not None and now >= self.trial_deadline
        outage = (self.trial_deadline is None and self.failures >= 3
                  and self.failed_since is not None
                  and now - self.failed_since >= self.FAILURE_DELAY)
        if not trial_failed and not outage:
            return
        if trial_failed:
            self.return_delay = min(self.return_delay * 2, 7200)
            self.reason = "cloud_return_failed"
        if not await self._handover(True):
            return
        self.trial_deadline = None
        self.next_attempt = time.monotonic() + self.return_delay

    async def _handover(self, local, *, recovery=False):
        owner = self.owner

        async def write():
            # Recheck after acquiring the setting lock: a manual selection or
            # shutdown may have superseded the automatic decision while waiting.
            if self.paused or self.closed:
                raise RuntimeError("Automatic handover superseded")
            if local and self.trial_deadline is None and self.failures < 3:
                self.next_attempt = 0
                return False
            if not local and not recovery:
                values = (owner.data or {}).get(owner.serial, {})
                if (not owner.last_update_success or not owner.transport.available
                        or charging_active(values, local=True) is not False
                        or owner.transport.session_guard.phase in ("starting", "waiting")):
                    self.trial_deadline = None
                    raise RuntimeError("Charging changed before cloud return")
            await owner._set_local(local)
            if local:
                await owner.connection_intent.async_automatic(True)
            return True

        self.next_attempt = time.monotonic() + self.RETRY_DELAY
        changed = await owner.charge_mode_policy.async_setting_write(write)
        if not local and self.trial_deadline is not None:
            remaining = self.trial_deadline - time.monotonic()
            if remaining > 0:
                try:
                    async with asyncio.timeout(min(10, remaining)):
                        await owner.async_refresh()
                except TimeoutError:
                    pass
        else:
            await owner.async_refresh()
        return changed

    def diagnostics(self):
        """Return bounded, identifier-free policy state."""
        return {
            "enabled": self.enabled,
            "paused_by_manual_control": self.paused,
            "reason": self.reason,
            "consecutive_failures": self.failures,
            "local_failures": self.local_failures,
            "retry_in_seconds": max(0, round(self.next_attempt - time.monotonic())),
            "cloud_trial_pending": self.trial_deadline is not None,
            "repair_required": self.blocked,
        }
