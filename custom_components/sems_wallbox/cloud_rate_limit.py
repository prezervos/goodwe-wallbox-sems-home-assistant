"""Share server-directed cooldown across one integration's cloud transports."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import math
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

    def request(self, send, *args, raise_on_limit=True, **kwargs):
        """Send once unless cooling down, and record a server-directed pause.

        Args:
            send: HTTP callable, kept injectable for existing request mocks.
            *args: Positional HTTP arguments.
            raise_on_limit: False lets the login-specific policy inspect the response.
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
