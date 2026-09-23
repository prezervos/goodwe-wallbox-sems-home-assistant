"""Supplemental cloud events trigger reads; polling and device reports stay authoritative."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import aiomqtt

from .cloud_push_polling import CloudPushPolling

_LOGGER = logging.getLogger(__name__)


class CloudPush:
    """Own one optional MQTT connection and coalesce validated refresh hints.

    Args:
        owner: Home Assistant coordinator whose normal refresh remains authoritative.
        serial: Configured charger serial, used for exact topic and payload matching.
        settings: Synchronous callable fetching fresh broker credentials.
    """

    DEBOUNCE = 0.5
    MIN_REFRESH_INTERVAL = 2.0
    CHECK_INTERVAL = 1.0
    RETRY_MIN = 15.0
    RETRY_MAX = 300.0

    def __init__(self, owner, serial, settings):
        self.owner = owner
        self.serial = serial
        self.settings = settings
        self.topics = (
            f"/goodwe/second-data/device/{serial}",
            f"/sems/event/chargingPile/{serial}",
        )
        self.task = None
        self._refresh_task = None
        self._closed = False
        self._seen = OrderedDict()
        self._last_refresh = 0.0
        self.connected = False
        self.last_event_at = None
        self.refresh_count = 0
        self.event_counts = {"telemetry": 0, "charging": 0}
        self.polling = CloudPushPolling(self)

    def _eligible(self):
        return not (
            self._closed
            or getattr(self.owner, "_closed", False)
            or getattr(self.owner, "local", False)
            or getattr(self.owner, "transitioning", False)
            or getattr(self.owner, "cloud_restored_at", None) is not None
        )

    def _epoch(self):
        return getattr(self.owner, "routing_epoch", 0)

    def start(self):
        """Start asynchronously without delaying entity setup or first polling."""
        if self.task is None and not self._closed:
            self.task = self.owner.hass.async_create_background_task(
                self._run(), "GoodWe cloud event listener"
            )

    async def close(self):
        """Cancel pending hints and disconnect before unloading or restoring routing."""
        self._closed = True
        tasks = [t for t in (self.task, self._refresh_task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self._refresh_task = None
        self.connected = False
        self._seen.clear()

    def event(self, topic, payload, *, retained=False, epoch=None):
        """Accept only bounded, non-retained hints for the configured device.

        Returns:
            Whether a valid new event was accepted. Payload values never set state.
        """
        if (
            not self._eligible()
            or retained
            or topic not in self.topics
            or epoch != self._epoch()
            or len(payload) > 32768
        ):
            return False
        try:
            message = json.loads(payload)
            if not isinstance(message, dict):
                return False
            # The SEMS frontend supports both direct and enveloped event payloads.
            if "message" in message or "msg" in message:
                message = message.get("message", message.get("msg"))
                if isinstance(message, str):
                    message = json.loads(message)
            if not isinstance(message, dict) or message.get("sn") != self.serial:
                return False
        except (ValueError, TypeError, UnicodeError):
            return False
        tid = message.get("tid")
        if tid is not None:
            if not isinstance(tid, str) or len(tid) > 256:
                return False
            key = (topic, tid)
            if key in self._seen:
                return False
            self._seen[key] = None
            if len(self._seen) > 128:
                self._seen.popitem(last=False)
        kind = "telemetry" if topic == self.topics[0] else "charging"
        self.event_counts[kind] += 1
        if topic == self.topics[0]:
            self.polling.telemetry_hint()
        self.last_event_at = time.monotonic()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self.owner.hass.async_create_background_task(
                self._refresh(epoch), "GoodWe cloud event refresh"
            )
        return True

    async def _refresh(self, epoch):
        delay = max(
            self.DEBOUNCE,
            self.MIN_REFRESH_INTERVAL - (time.monotonic() - self._last_refresh),
        )
        await asyncio.sleep(delay)
        if not self._eligible() or epoch != self._epoch():
            return
        self._last_refresh = time.monotonic()
        self.refresh_count += 1
        # Normal coordinator validation, failure handling and routing guards apply.
        await self.owner.async_request_refresh()

    async def _listen(self, epoch):
        config = await self.owner.hass.async_add_executor_job(self.settings)
        if not self._eligible() or epoch != self._epoch():
            return
        parsed = urlsplit(config["brokerUrl"])
        context = await self.owner.hass.async_add_executor_job(
            ssl.create_default_context
        )
        async with aiomqtt.Client(
            hostname=parsed.hostname,
            port=parsed.port or 443,
            username=config["userName"],
            password=config["password"],
            identifier=config["clientId"],
            transport="websockets",
            websocket_path=parsed.path or "/mqtt",
            tls_context=context,
            timeout=15,
            keepalive=60,
            clean_session=True,
            max_queued_incoming_messages=32,
        ) as client:
            await client.subscribe([(topic, 0) for topic in self.topics])
            self.connected = True
            _LOGGER.debug("Cloud event subscription established")
            async for message in client.messages:
                self.event(
                    str(message.topic),
                    message.payload,
                    retained=message.retain,
                    epoch=epoch,
                )

    def _check_polling(self):
        """Bound silent MQTT operation by the configured normal polling interval."""
        if self.polling.check() and (
            self._refresh_task is None or self._refresh_task.done()
        ):
            self._refresh_task = self.owner.hass.async_create_background_task(
                self._refresh(self._epoch()), "GoodWe cloud backup refresh"
            )

    async def _run(self):
        retry = self.RETRY_MIN
        next_attempt = 0.0
        while not self._closed:
            self._check_polling()
            if not self._eligible() or time.monotonic() < next_attempt:
                await asyncio.sleep(self.CHECK_INTERVAL)
                continue
            epoch = self._epoch()
            listener = asyncio.create_task(self._listen(epoch))
            started = time.monotonic()
            failed = False
            try:
                while (
                    not listener.done() and self._eligible() and epoch == self._epoch()
                ):
                    self._check_polling()
                    await asyncio.sleep(self.CHECK_INTERVAL)
                if listener.done():
                    await listener
                    failed = True
            except (aiomqtt.MqttError, OSError, ValueError, RuntimeError, KeyError):
                # Optional push failures never change coordinator or fallback health.
                failed = True
                _LOGGER.debug("Cloud events unavailable; periodic polling continues")
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
                self.connected = False
                self._check_polling()
            if failed:
                if time.monotonic() - started >= 60:
                    retry = self.RETRY_MIN
                next_attempt = time.monotonic() + retry
                retry = min(retry * 2, self.RETRY_MAX)
            else:
                next_attempt = 0.0
                retry = self.RETRY_MIN
