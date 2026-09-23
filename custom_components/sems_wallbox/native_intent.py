"""Bounded latest user intent while a verified transport is unavailable."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingIntent:
    """One unsent value, with a deadline that repeated clicks cannot extend."""

    value: object
    operation: object


class LatestIntent:
    """Coalesce logical controls; never persist or replay uncertain commands.

    Args:
        owner: Native coordinator with the existing control policy and transport.
    """

    EXPIRY = 120

    def __init__(self, owner):
        self.owner = owner
        self.pending = {}
        self.task = None
        self.executing = None
        self.error = None
        self.closed = False
        self.version = 0
        self.batch_deadline = None

    @property
    def blackout(self):
        """Whether a handover is running or cloud freshness is still unverified."""
        control = getattr(self.owner, "control_fallback", None)
        return bool(control is not None and control.preparing) or self.owner.transitioning or (
            not self.owner.local and self.owner.cloud_restored_at is not None
        )

    @property
    def ready(self):
        """Require verified, current transport data before releasing any intent."""
        return (not self.blackout and self.owner.last_update_success
                and not self.owner._closed)

    def submit(self, key, value, operation, *, force=False):
        """Remember the latest value and return whether execution was deferred.

        Args:
            key: Logical control: charging, mode or power.
            value: Already validated requested setting.
            operation: Existing async control operation; no transport-specific bypass.
            force: Defer while the caller verifies cloud control availability.

        Returns:
            True when retained for deferred execution, False for normal execution.
        """
        if self.closed:
            raise ConnectionError("Integration is shutting down")
        if not (force or self.blackout or self.pending or self.executing is not None):
            return False
        if key not in ("charging", "mode", "power"):
            raise ValueError("Unknown deferred control")
        now = time.monotonic()
        if self.batch_deadline is None:
            self.batch_deadline = now + self.EXPIRY
        self.pending[key] = PendingIntent(value, operation)
        self.version += 1
        self.error = None
        # New requests must also fence an older preparation already awaiting I/O.
        self.owner.charge_mode_policy.invalidate()
        self.owner.async_update_listeners()
        if self.task is None or self.task.done():
            self.task = self.owner.hass.async_create_background_task(
                self._run(), "GoodWe pending user intent"
            )
        return True

    def _next(self):
        if "charging" in self.pending and self.pending["charging"].value is False:
            return "charging"
        order = ("mode", "power", "charging") if self.owner.local else ("power", "mode", "charging")
        return next(key for key in order if key in self.pending)

    async def _run(self):
        try:
            while self.pending and not self.closed:
                if time.monotonic() >= self.batch_deadline:
                    await self._fail("expired")
                    return
                if not self.ready:
                    await asyncio.sleep(0.2)
                    continue
                key = self._next()
                request = self.pending.pop(key)
                version = self.version
                self.executing = key
                self.owner.async_update_listeners()
                try:
                    # Remove before transmission. Failure cannot replay this request.
                    await request.operation()
                except Exception as exc:
                    # User-visible background failure must be retained even though
                    # the original service call already acknowledged deferred intent.
                    if key == "charging" and request.value is True:
                        # An errored Start may already have reached the device.
                        # Keep a newer Stop, but never dispatch another queued Start.
                        stop = self.pending.get("charging")
                        if stop is not None and stop.value is False:
                            self.pending = {"charging": stop}
                            self.error = "operation_failed"
                        else:
                            await self._fail("operation_failed")
                            return
                    elif (version != self.version and self.pending
                          and getattr(exc, "translation_key", None) == "request_superseded"):
                        _LOGGER.debug("Deferred operation superseded by newer intent")
                    else:
                        _LOGGER.exception("Deferred wallbox operation failed")
                        await self._fail("operation_failed")
                        return
                finally:
                    self.executing = None
                    self.owner.async_update_listeners()
        except asyncio.CancelledError:
            self.pending.clear()
            raise
        finally:
            if not self.pending:
                self.batch_deadline = None
            elif not self.closed:
                self.task = self.owner.hass.async_create_background_task(
                    self._run(), "GoodWe pending user intent"
                )

    async def _fail(self, reason, *, notify=True):
        self.error = reason
        self.pending.clear()
        self.batch_deadline = None
        if not notify:
            self.owner.async_update_listeners()
            return
        from homeassistant.components import persistent_notification
        from homeassistant.helpers.translation import async_get_translations

        language = getattr(getattr(self.owner.hass, "config", None), "language", "en")
        messages = await async_get_translations(
            self.owner.hass, language, "exceptions", {"sems_wallbox"}
        )
        key = "pending_controls_expired" if reason == "expired" else "pending_controls_failed"
        persistent_notification.async_create(
            self.owner.hass,
            messages["component.sems_wallbox.exceptions." + key + ".message"],
            title="GoodWe Wallbox",
            notification_id=f"sems_wallbox_{self.owner.entry.entry_id}_pending_controls",
        )
        self.owner.async_update_listeners()

    def diagnostics(self):
        """Expose pending user choices separately from observed device state."""
        return {"pending_controls": {key: item.value for key, item in self.pending.items()},
                "executing_control": self.executing, "pending_control_error": self.error}

    async def close(self):
        """Discard pending intent and await cancellation; reload never replays it."""
        self.closed = True
        self.pending.clear()
        self.owner.charge_mode_policy.invalidate()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.batch_deadline = None
