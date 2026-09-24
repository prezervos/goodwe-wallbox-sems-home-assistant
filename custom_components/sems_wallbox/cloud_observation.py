"""Read timestamped SEMS telemetry using the shared SEMS+ web session.

Device report time remains independent from HTTP receipt time. The existing
SEMS+ token is accepted by GetCurrentChargeinfo; do not create another Android
login that can fail independently or compete with controls and MQTT discovery.
"""

from __future__ import annotations

import json
import threading
import time

import requests

from .cloud_http import SEMS_USER_AGENT
from .cloud_rate_limit import CloudRequestGate
from .operation_budget import request_timeout, serialized_request

STATUS_URL = "https://www.semsportal.com/api/v3/EvCharger/GetCurrentChargeinfo"


class CloudAuthenticationError(ConnectionError):
    """Credentials were rejected; transport switching cannot repair the account."""


class CloudObservationReader:
    """Supply report freshness that the SEMS Plus configuration endpoint lacks."""

    def __init__(self, token_provider, *, token_rejected, session=None, request_gate=None):
        """Bind telemetry reads to the integration's shared authentication.

        Args:
            token_provider: Return a valid shared token, renewing it if necessary.
            token_rejected: Invalidate the shared session after a rejected read.
            session: Optional caller-owned HTTP session.
            request_gate: Shared rate-limit and request-budget gate.
        """
        self._request_gate = request_gate if request_gate is not None else CloudRequestGate()
        self._token_provider = token_provider
        self._token_rejected = token_rejected
        self._owns_session = session is None
        self._session = requests.Session() if session is None else session
        self._closed = False
        self._lock = threading.Lock()
        self._retry_at = 0.0

    def read(self, serial):
        """Read device telemetry, allowing one expired-token refresh and no writes."""
        with serialized_request(self._lock):
            if self._closed:
                raise ConnectionError("SEMS observation reader is closed")
            if time.monotonic() < self._retry_at:
                raise ConnectionError("Timestamped SEMS telemetry is temporarily unavailable")
            for attempt in range(2):
                token = self._token_provider()
                if not isinstance(token, dict) or not token.get("token"):
                    raise ConnectionError("Shared SEMS session is unavailable")
                response = self._request_gate.request(self._session.post,
                    STATUS_URL,
                    headers={
                        "User-Agent": SEMS_USER_AGENT,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "token": json.dumps(token),
                    },
                    json={"sn": serial},
                    timeout=request_timeout(15),
                    allow_redirects=False,
                )
                if response.status_code in (401, 403):
                    if attempt == 0:
                        self._token_rejected()
                        continue
                    self._reject_telemetry()
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ConnectionError("Invalid timestamped SEMS response")
                data = payload.get("data")
                if (
                    not payload.get("hasError")
                    and payload.get("code") in (0, "0", None)
                    and isinstance(data, dict)
                    and data.get("sn") == serial
                ):
                    if not data.get("lastUpdate"):
                        raise ConnectionError(
                            "SEMS status has no device report timestamp"
                        )
                    return data
                # The official legacy frontend treats both codes as an expired
                # login. Match codes as well as text; messages can be localized.
                expired = str(payload.get("code")) in ("100001", "100002") or (
                    "authorization has expired" in str(payload.get("msg", "")).lower()
                )
                if expired:
                    if attempt == 0:
                        self._token_rejected()
                        continue
                    self._reject_telemetry()
                raise ConnectionError(
                    "Missing or mismatched timestamped SEMS device report"
                )
        raise ConnectionError("Timestamped SEMS observation failed")

    def _reject_telemetry(self):
        # A renewed web login succeeded. A legacy endpoint rejection does not
        # prove bad credentials or justify invalidating controls/MQTT again.
        self._retry_at = time.monotonic() + 30.0
        raise ConnectionError("Timestamped SEMS telemetry was rejected after session renewal")

    def close(self) -> None:
        """Wait for the current read, then close only a session owned here.

        Injected sessions remain the caller's responsibility. Run in an executor;
        the lock may wait for a synchronous HTTP request to finish.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_session:
                self._session.close()
