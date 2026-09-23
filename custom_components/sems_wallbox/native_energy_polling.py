"""Opt-in idle-only cumulative energy polling, isolated from control availability."""

from __future__ import annotations

import asyncio
import time


class NativeEnergyPolling:
    """Poll only while its diagnostic entity is enabled and TCP is confirmed."""

    INTERVAL = 300

    def __init__(self, owner):
        self.owner = owner
        self.value = None
        self.error = None
        self.epoch = None
        self.next_read = 0
        self.was_idle = False
        self.task = None
        self.enabled = False
        self._wake = asyncio.Event()

    @property
    def available(self):
        """Never present a previous transport/session value as a current reading."""
        owner = self.owner
        return (self.enabled and self.value is not None and owner.local
                and not owner.transitioning and not owner._closed
                and owner.last_update_success and owner.transport.available
                and self.epoch == owner.transport.epoch)

    def start(self):
        """Start one background task when HA enables the entity."""
        self.enabled = True
        if self.task is None or self.task.done():
            self.task = self.owner.hass.async_create_background_task(
                self._run(), "GoodWe cumulative energy"
            )

    async def close(self):
        """Stop optional reads before releasing the transport or removing the entity."""
        self.enabled = False
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.value = None

    def wake(self):
        """Schedule a due read after fresh telemetry, avoiding polling phase lock."""
        if self.enabled:
            self._wake.set()

    async def _run(self):
        while self.enabled and not self.owner._closed:
            await self.tick()
            try:
                async with asyncio.timeout(5):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()

    async def tick(self):
        """Read at most once per five minutes, or once after a session ends."""
        owner = self.owner
        transport = owner.transport
        if not self.enabled or owner._closed:
            return
        if self.epoch != transport.epoch or not owner.local or owner.transitioning:
            self.value = None
            self.error = None
            self.epoch = transport.epoch
            self.next_read = 0
            self.was_idle = False
        if (not owner.local or owner.transitioning or not owner.last_update_success
                or not transport.available):
            return
        idle = (transport.latest.stopped and transport.latest.state == 0
                and transport.session_guard.phase not in ("starting", "waiting", "charging"))
        if not idle:
            self.was_idle = False
            return
        if not self.was_idle:
            self.next_read = 0
        self.was_idle = True
        if time.monotonic() < self.next_read or transport.optional_read_busy:
            return
        epoch = transport.epoch
        self.next_read = time.monotonic() + self.INTERVAL
        try:
            value = await transport.async_read_energy()
        except (OSError, ValueError):
            # Optional storage failure must not mark all wallbox entities offline.
            self.value = None
            self.error = "read_failed"
        else:
            if epoch == transport.epoch and owner.local and not owner.transitioning:
                self.value = value
                self.error = None
        owner.async_update_listeners()
