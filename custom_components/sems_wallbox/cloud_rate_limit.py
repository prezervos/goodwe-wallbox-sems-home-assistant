"""Share server-directed cooldown across one integration's cloud transports."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import math
from http.client import RemoteDisconnected

import requests
import threading
import time

from .operation_budget import request_timeout, serialized_request


class CloudRateLimitedError(ConnectionError):
    """A cloud request was refused or deferred by the shared cooldown."""

    def __init__(self, seconds: float):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__(f"Cloud requests paused; retry in {self.retry_after} seconds")


class CloudRequestGate:
    """Serialize HTTP dispatch and reject calls during a server cooldown.

    Shared by SEMS+ settings/controls/MQTT discovery and v3 telemetry. The gate
    never sleeps or retries a command. A request already sent before a response
    cannot be recalled; serialization prevents new requests racing that response.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self._delay = 30.0

    def request(self, send, *args, raise_on_limit=True, retry_read=False, **kwargs):
        """Send within the shared budget, optionally retrying a read-only peer reset.

        Args:
            send: HTTP callable, kept injectable for existing request mocks.
            *args: Positional HTTP arguments.
            raise_on_limit: False lets the login-specific policy inspect the response.
            retry_read: Permit one retry of an explicitly read-only observation
                after the peer closes/resets the connection, within its timeout.
            **kwargs: Keyword HTTP arguments, including a numeric timeout.

        Returns:
            The HTTP response when no cooldown was requested.

        Raises:
            CloudRateLimitedError: Rate limited or a cooldown is still active.
        """
        with serialized_request(self._lock):
            remaining = self._retry_at - time.monotonic()
            if remaining > 0:
                raise CloudRateLimitedError(remaining)
            # Lock acquisition can consume the caller's operation budget.
            if isinstance(kwargs.get("timeout"), (int, float)):
                kwargs["timeout"] = request_timeout(kwargs["timeout"])
            started = time.monotonic()
            timeout = kwargs.get("timeout")
            try:
                response = send(*args, **kwargs)
            except requests.exceptions.ConnectionError as error:
                if (not retry_read or isinstance(error, requests.exceptions.SSLError)
                        or not _peer_closed(error)):
                    raise
                if not isinstance(timeout, (int, float)):
                    raise
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise
                # Recheck cancellation/budget before the sole read retry. The
                # shared lock still prevents interleaved requests/cooldowns.
                kwargs["timeout"] = request_timeout(remaining)
                response = send(*args, **kwargs)
            status = response.status_code
            value = response.headers.get("Retry-After") if status in (429, 503) else None
            delay = None
            if isinstance(value, str):
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(value).timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        pass
                if delay is not None and (not math.isfinite(delay) or delay < 0):
                    delay = None
            if status == 429 or (status == 503 and delay is not None):
                wait = self._delay if delay is None else max(1.0, delay)
                self._retry_at = time.monotonic() + wait
                self._delay = min(self._delay * 2, 300.0)
                if raise_on_limit:
                    raise CloudRateLimitedError(wait)
            if isinstance(status, int) and 200 <= status < 300:
                self._delay = 30.0
            return response


def _peer_closed(error: BaseException) -> bool:
    """Recognize nested requests/urllib3 peer resets without matching error text."""
    pending = [error]
    seen = set()
    while pending:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, (RemoteDisconnected, ConnectionResetError)):
            return True
        pending.extend(arg for arg in cause.args if isinstance(arg, BaseException))
        if cause.__cause__ is not None:
            pending.append(cause.__cause__)
    return False
