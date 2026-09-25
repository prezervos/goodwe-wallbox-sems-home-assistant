"""Bounded readback after controls; never replay writes or manufacture state."""

import asyncio
from dataclasses import dataclass
from functools import wraps
import math
import logging
import time

from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)


def async_call_later(hass, delay, callback):
    """Load HA scheduling only when a live coordinator arms a timer."""
    from homeassistant.helpers.event import async_call_later as schedule

    return schedule(hass, delay, callback)


@dataclass
class PendingRead:
    """Keep the latest accepted choice and its independent readback deadline."""

    value: object
    epoch: int
    started: float
    source: str
    last_read: float


def matches(field, expected, data):
    """Compare actual configuration or explicit session state, never measured watts."""
    if field == "charging":
        raw = data.get("modbus_status_raw")
        if raw is not None:
            return raw == 3 if expected else raw in (0, 1, 4, 5, 7, 8, 10)
        status = str(data.get("status", "")).lower()
        if expected:
            return status in ("charging", "evdetail_status_title_charging") or (
                not status and data.get("last_charge_work_status") == 6
            )
        # Zero watts and a missing status do not prove that a session was stopped.
        return status in (
            "standby",
            "idle",
            "waiting",
            "available",
            "evdetail_status_title_waiting",
        )
    actual = (
        data.get("_reported_charge_mode", data.get(field))
        if field == "chargeMode"
        else data.get(field)
    )
    if actual is None:
        return False
    if isinstance(expected, bool):
        return type(actual) is bool and actual == expected
    try:
        left, right = float(actual), float(expected)
    except (ValueError, TypeError):
        return actual == expected
    return math.isfinite(left) and math.isfinite(right) and abs(left - right) < 0.001


class WriteConfirmation:
    """Share one bounded read scheduler across all controls of a wallbox.

    Reads already performed by normal polling or MQTT satisfy pending choices.
    A failed read ends accelerated polling and leaves recovery to the coordinator.
    """

    def __init__(self, owner, *, modbus=False):
        self.owner = owner
        self.modbus = modbus
        self.pending = {}
        self.tokens = {}
        self.results = {}
        self._cancel = None
        self._busy = False
        self._closed = False

    def begin(self, field):
        """Supersede an older choice before awaiting a potentially slow write."""
        token = object()
        self.tokens[field] = token
        self.results[field] = "writing"
        self.pending.pop(field, None)
        self._schedule()
        return token, getattr(self.owner, "routing_epoch", 0)

    def accepted(self, field, value, ticket, *, source="telemetry"):
        """Arm readback only for the newest successfully completed command."""
        token, epoch = ticket
        if self.tokens.get(field) is not token:
            return
        self.tokens.pop(field, None)
        if (
            self._closed
            or getattr(self.owner, "local", False)
            or getattr(self.owner, "transitioning", False)
            or epoch != getattr(self.owner, "routing_epoch", 0)
        ):
            self.results[field] = "cancelled"
            return
        now = time.monotonic()
        cancel = getattr(self.owner, "_pending_refresh_cancel", None)
        if cancel is not None:
            cancel()
            self.owner._pending_refresh_cancel = None
        self.pending[field] = PendingRead(value, epoch, now, source, now)
        self.results[field] = "pending"
        self._schedule()

    def rejected(self, field, ticket):
        """Discard a failed command without erasing a newer accepted choice."""
        if self.tokens.get(field) is ticket[0]:
            self.tokens.pop(field, None)
            self.results[field] = "write_failed"

    def observed(self, data, *, source="telemetry", read_started=None):
        """Consume a successful authoritative read, not an optimistic UI update."""
        now = time.monotonic()
        for field, target in list(self.pending.items()):
            if target.source != source or (
                read_started is not None and read_started < target.started
            ):
                continue
            target.last_read = now
            if (
                field == "charging"
                and target.value is True
                and data.get("modbus_status_raw") in (5, 8)
            ):
                self.pending.pop(field)
                self.results[field] = "device_rejected"
            elif matches(field, target.value, data):
                self.pending.pop(field)
                self.results[field] = "confirmed"
        self._schedule()

    def failed(self):
        """Yield to normal authentication, rate-limit and communication recovery."""
        for field in self.pending:
            self.results[field] = "read_failed"
        self.pending.clear()
        self._schedule()

    def cancel(self):
        """Invalidate waiting commands and reads on a transport change or unload."""
        for field in self.pending.keys() | self.tokens.keys():
            self.results[field] = "cancelled"
        self.pending.clear()
        self.tokens.clear()
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    def close(self):
        """Cancel owned timers without cancelling an in-flight coordinator read."""
        self._closed = True
        self.cancel()

    def _schedule(self):
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        now = time.monotonic()
        due = []
        slots = range(5, 61, 5) if self.modbus else (5, 10, 20, 35, 60)
        for field, target in list(self.pending.items()):
            if getattr(self.owner, "local", False) or target.epoch != getattr(
                self.owner, "routing_epoch", 0
            ):
                self.pending.pop(field)
                self.results[field] = "cancelled"
                continue
            deadline = target.started + 60
            if now > deadline:
                self.pending.pop(field)
                self.results[field] = "unconfirmed"
                continue
            candidates = [
                target.started + slot
                for slot in slots
                if target.started + slot > target.last_read
            ]
            if not candidates:
                self.pending.pop(field)
                self.results[field] = "unconfirmed"
                continue
            due.append(min(deadline, max(candidates[0], target.last_read + 5)))
        if due and not self._busy and not self._closed:
            self._cancel = async_call_later(
                self.owner.hass, max(0, min(due) - now), self._wake
            )

    @callback
    def _wake(self, _now):
        self._cancel = None
        if self._busy or self._closed or not self.pending:
            return
        self._busy = True
        self.owner.hass.async_create_task(self._refresh())

    async def _refresh(self):
        attempted = set()
        try:
            if getattr(self.owner, "_closed", False) or getattr(
                self.owner, "transitioning", False
            ):
                self.cancel()
                return
            now = time.monotonic()
            due_sources = {
                target.source
                for target in self.pending.values()
                if now - target.last_read >= 5
            }
            if "telemetry" in due_sources:
                attempted.add("telemetry")
                await self.owner.async_request_refresh()
                if not self.owner.last_update_success:
                    self.failed()
            settings = getattr(self.owner, "cloud_settings", None)
            if settings is not None and "settings" in due_sources and self.pending:
                attempted.add("settings")
                # Reuse an active settings worker. Otherwise read only configuration;
                # a power-limit write must not double every check with a v3 request.
                if settings._task is not None and not settings._task.done():
                    await settings._task
                else:
                    await settings.refresh()
        except asyncio.CancelledError:
            self.cancel()
            raise
        except Exception:
            # Isolate scheduler failures from HA background-task execution.
            self.failed()
            _LOGGER.debug(
                "Control readback interrupted; normal recovery remains active",
                exc_info=True,
            )
        finally:
            self._busy = False
            # Advance unanswered slots too (e.g. HA disabled polling/debounced read).
            now = time.monotonic()
            for target in self.pending.values():
                if target.source in attempted:
                    target.last_read = max(target.last_read, now)
            self._schedule()

    def diagnostics(self):
        """Return field names and outcomes only, without account or device data."""
        return dict(self.results)


def confirm_write(field, *, value=None):
    """Observe an entity write using an explicit normalized data field.

    Args:
        field: Data key or callable resolving the key from the entity.
        value: Optional callable mapping entity and input to the normalized value.
    """

    def decorate(function):
        @wraps(function)
        async def wrapped(entity, *args, **kwargs):
            owner = entity.coordinator
            monitor = getattr(owner, "write_confirmation", None)
            key = field(entity) if callable(field) else field
            if monitor is None or not key or getattr(owner, "local", False):
                return await function(entity, *args, **kwargs)
            raw = args[0] if args else next(iter(kwargs.values()), None)
            expected = value(entity, raw) if value is not None else raw
            if expected is None:
                return await function(entity, *args, **kwargs)
            ticket = monitor.begin(key)
            try:
                result = await function(entity, *args, **kwargs)
            except BaseException:
                # Cancellation must invalidate only this command, never a newer one.
                monitor.rejected(key, ticket)
                raise
            source = (
                "settings"
                if hasattr(owner, "cloud_settings") and key != "charging"
                else "telemetry"
            )
            monitor.accepted(key, expected, ticket, source=source)
            return result

        return wrapped

    return decorate


def confirmation_read(function):
    """Observe coordinator results only after successful device reads complete."""

    @wraps(function)
    async def wrapped(owner, *args, **kwargs):
        monitor = getattr(owner, "write_confirmation", None)
        started = time.monotonic()
        try:
            result = await function(owner, *args, **kwargs)
        except BaseException:
            # Read cancellation also yields to the coordinator lifecycle/recovery.
            if monitor is not None:
                monitor.failed()
            raise
        if monitor is not None:
            serial = getattr(owner, "serial", None) or owner._station_id
            monitor.observed(result.get(serial, {}), read_started=started)
        return result

    return wrapped
