"""Read timestamped measured telemetry from the established SEMS v3 API.

SEMS+ detail supplies configuration but lacks this device report timestamp. Keep
its configured limit separate from v3 set_charge_power, which can stay at 4.2 kW
while the configured ceiling changes. This Android-client telemetry session is
separate from the single shared SEMS+ web session used for MQTT and controls.
"""

from __future__ import annotations

import json
import threading

import requests

from .operation_budget import request_timeout, serialized_request

LOGIN_URL = "https://www.semsportal.com/api/v3/Common/CrossLogin"
STATUS_URL = "https://www.semsportal.com/api/v3/EvCharger/GetCurrentChargeinfo"


class CloudAuthenticationError(ConnectionError):
    """Credentials were rejected; transport switching cannot repair the account."""


class CloudObservationReader:
    """Supply report freshness that the SEMS Plus configuration endpoint lacks."""

    def __init__(self, username, password, *, session=None):
        self._username = username
        self._password = password
        self._owns_session = session is None
        self._session = requests.Session() if session is None else session
        self._closed = False
        self._token = None
        self._lock = threading.Lock()

    def _login(self):
        response = self._session.post(
            LOGIN_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "token": json.dumps(
                    {"version": "", "client": "semsPlusAndroid", "language": "en"}
                ),
            },
            json={"account": self._username, "pwd": self._password},
            timeout=request_timeout(15),
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise CloudAuthenticationError("SEMS authentication was rejected")
        response.raise_for_status()
        payload = response.json()
        token = payload.get("data")
        if (
            payload.get("hasError")
            or payload.get("code") not in (0, "0", None)
            or not isinstance(token, dict)
            or not token.get("token")
        ):
            raise CloudAuthenticationError("Cannot authenticate timestamped SEMS status reader")
        self._token = dict(token, api=payload.get("api"))

    def read(self, serial):
        """Read device telemetry, allowing one expired-token refresh and no writes."""
        with serialized_request(self._lock):
            if self._closed:
                raise ConnectionError("SEMS observation reader is closed")
            for attempt in range(2):
                if self._token is None:
                    self._login()
                response = self._session.post(
                    STATUS_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "token": json.dumps(self._token),
                    },
                    json={"sn": serial},
                    timeout=request_timeout(15),
                    allow_redirects=False,
                )
                if response.status_code in (401, 403):
                    self._token = None
                    if attempt == 0:
                        continue
                    raise CloudAuthenticationError("SEMS authentication was rejected")
                response.raise_for_status()
                payload = response.json()
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
                    self._token = None
                    if attempt == 0:
                        continue
                    raise CloudAuthenticationError("SEMS telemetry session was rejected after renewal")
                raise ConnectionError(
                    "Missing or mismatched timestamped SEMS device report"
                )
        raise ConnectionError("Timestamped SEMS observation failed")

    def close(self) -> None:
        """Wait for the current read, then close only a session owned here.

        Injected sessions remain the caller's responsibility. Run in an executor;
        the lock may wait for a synchronous HTTP request to finish.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._token = None
            if self._owns_session:
                self._session.close()
