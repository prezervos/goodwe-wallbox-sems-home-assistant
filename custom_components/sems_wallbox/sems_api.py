"""SEMS+ configuration and controls, with separate SEMS v3 telemetry.

SEMS+ gateway paths are not a uniform "v4" API: individual services use different
path versions. The legacy gen2 method names refer to charger support, not API v2.
Use SEMS+ for configured limits and commands; v3 provides timestamped measured
telemetry. A v3 reported allocation may differ from the SEMS+ configured ceiling,
and SEMS+ chargePower is not a measurement of current consumption.
"""

import base64
import hashlib
import json
import logging
import math
from email.utils import parsedate_to_datetime
import re
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

import requests
from homeassistant import exceptions

from .cloud_http import SEMS_USER_AGENT
from .cloud_rate_limit import CloudRateLimitedError, CloudRequestGate
from .cloud_observation import CloudAuthenticationError
from .operation_budget import BudgetCancelled, request_timeout, retry_delay, serialized_request

_LOGGER = logging.getLogger(__name__)

# Prefer the original SEMS+ web login. Common/CrossLogin remains a compatible
# alternative, but can report success without a token for some accounts (#21).
# Both use the shared Mozilla-format User-Agent and one semsPlusWeb session.
_LOGIN_URLS = {
    "original": "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login",
    "common": "https://www.semsportal.com/api/v3/Common/CrossLogin",
}
_LOGIN_ORDER = ("original", "common")


class _LoginFallbackEligible(Exception):
    """A login endpoint failed without rejecting credentials or throttling."""


# EU gateway base (overridden at runtime from the cross-login response)
_EuGatewayBase         = "https://eu-gateway.semsportal.com/web/sems"
# Relative paths appended to the dynamic base via self._eu_url()
_PATH_SET_CONFIG       = "sems-remote/api/ev-charger/set-config"
_PATH_SET_MODE         = "sems-remote/api/ev-charger/set-mode"
_PATH_START_CHARGE     = "sems-remote/api/ev-charger/startCharge"
_PATH_STOP_CHARGE      = "sems-remote/api/ev-charger/stopCharge"
_PATH_DETAIL           = "sems-remote/api/ev-charger/detail"
_PATH_GET_WORK_MODE    = "sems-remote/api/v2/address/remote/get-work-mode"
_PATH_GET_LAST_CHARGE  = "sems-plant/api/v1/chargePile/getLastCharge"
_PATH_CONTROL_ITEMS    = "sems-remote/api/ev-charger/control-item-content-list"
_PATH_STATIONS_PAGE    = "sems-plant/api/portal/stations/page"
_PATH_CENTRALIZED_PAGE = "sems-plant/api/web/device/centralized/page"

_RequestTimeout = 30   # seconds for status reads
_SetModeTimeout = 90   # seconds for EU gateway set-mode (device can take 60-90s to respond)
_SetModeR0305Retries = 3   # retry count on R0305 (remote_control_fail -- transient)
_SetModeR0305Delay = 2.0   # seconds between R0305 retries


def _response_json(response):
    """Keep explicit HTTP authentication rejection distinct from an outage."""
    if response.status_code in (401, 403):
        raise CloudAuthenticationError("SEMS authentication was rejected")
    response.raise_for_status()
    return response.json()


def _command_succeeded(payload):
    """Honor explicit status codes before a legacy boolean acknowledgement."""
    code = payload.get("code")
    if code is not None:
        return str(code) in ("00000", "0")
    return payload.get("data") is True


def _plug_and_charge_state(value):
    """Decode reported values without treating missing support as disabled."""
    if value in (False, 0, "0"):
        return False
    if value in (True, 1, 170, "1", "170"):
        return True
    return None


def _serialized_web_request(function):
    """Keep one SEMS+ session stable through a complete synchronous request.

    MQTT discovery and controls share this client. A second login while a command
    is in flight can invalidate its token. Reentrant locking permits discovery
    and bounded token renewal helpers called inside another operation. Callers
    run these synchronous methods in HA's executor, never on the event loop.
    """
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with serialized_request(self._web_request_lock):
            if self._closed:
                raise ConnectionError("SEMS client is closed")
            return function(self, *args, **kwargs)
    return wrapped


class SemsApi:
    """Interface to the SEMS API."""

    supports_timestamped_observation = True

    def __init__(self, hass, username, password):
        """Init SEMS API wrapper."""
        from .cloud_observation import CloudObservationReader
        self._request_gate = CloudRequestGate()
        self._observation_reader = CloudObservationReader(
            self._observation_token, token_rejected=self._invalidate_rejected_session,
            request_gate=self._request_gate
        )
        self._hass = hass
        self._username = username
        self._password = password
        self._web_request_lock = threading.RLock()
        self._closed = False
        self._control_item_ranges = {}
        self._web_login_retry_at = 0.0
        self._web_login_delay = 30.0
        self._web_retry_after = 0.0
        self._web_login_auth_error = False
        self.login_attempts = 0
        self.successful_logins = 0
        self.session_recovery_attempts = 0
        self.last_login_at = None
        self._web_token: dict | None = None  # semsPlusWeb token for EU gateway
        self._web_api_base: str = _EuGatewayBase  # overridden from login response
        # Gen2: cached plant info (auto-detected or user-supplied)
        self._plant_id: str | None = None
        self._product_model: str | None = None
        _LOGGER.debug("SEMS API wrapper initialized")

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    @_serialized_web_request
    def replace_credentials(self, username, password):
        """Replace validated credentials without changing device routing.

        The existing API object remains shared by controls, polling and MQTT.
        Run in an executor after validation; outstanding HTTP reads finish before
        the old observation session is closed.
        """
        from .cloud_observation import CloudObservationReader

        self._observation_reader.close()
        self._username, self._password = username, password
        self._web_login_retry_at = 0.0
        self._web_login_delay = 30.0
        self._web_retry_after = 0.0
        self._web_login_auth_error = False
        self._web_token = None
        self._web_api_base = _EuGatewayBase
        self._observation_reader = CloudObservationReader(
            self._observation_token, token_rejected=self._invalidate_rejected_session,
            request_gate=self._request_gate
        )

    def close(self) -> None:
        """Close owned HTTP resources after any in-flight web/telemetry request."""
        with self._web_request_lock:
            if self._closed:
                return
            self._closed = True
            self._observation_reader.close()
            self._web_token = None

    def _login_request(self, endpoint):
        """Build endpoint-specific credentials without changing the session client."""
        headers = {
            "User-Agent": SEMS_USER_AGENT,
            "Content-Type": "application/json", "Accept": "application/json",
            "token": json.dumps({"version": "", "client": "semsPlusWeb", "language": "en"}),
        }
        body = {"account": self._username, "pwd": self._password}
        if endpoint == "common":
            return _LOGIN_URLS[endpoint], headers, body
        ts = str(int(time.time() * 1000))
        digest = hashlib.sha256(f"{ts}@@".encode()).hexdigest()
        headers.update({
            "token": json.dumps({"uid": "", "timestamp": 0, "token": "",
                                 "client": "semsPlusWeb", "version": "", "language": "en"}),
            "client": "semsPlusWeb", "neutral": "0", "currentlang": "en",
            "x-signature": base64.b64encode(f"{digest}@{ts}".encode()).decode(),
        })
        body.update({
            "pwd": base64.b64encode(
                hashlib.md5(self._password.encode()).hexdigest().encode()
            ).decode(),
            "agreement": 1, "isLocal": False, "isChinese": False,
        })
        return _LOGIN_URLS[endpoint], headers, body

    def _record_login_retry_after(self, response):
        """Honor server-directed delay before considering another login endpoint."""
        value = response.headers.get("Retry-After")
        if not isinstance(value, str):
            return False
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        if math.isfinite(seconds) and seconds > 0:
            self._web_retry_after = seconds
        return True

    def _login_attempt(self, endpoint, deadline):
        """Attempt one login; reject unsafe routing and explicit authentication errors."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _LOGGER.debug("SEMS login diagnostic: endpoint=%s reason=deadline_expired", endpoint)
            return None
        url, headers, body = self._login_request(endpoint)
        resp = self._request_gate.request(requests.post, url, raise_on_limit=False, headers=headers, json=body,
                             timeout=request_timeout(min(_RequestTimeout, remaining)))
        _LOGGER.debug("SEMS login diagnostic: endpoint=%s http_status=%s", endpoint, resp.status_code)
        if resp.status_code in (401, 403):
            _LOGGER.debug("SEMS login diagnostic: endpoint=%s reason=http_auth_rejected", endpoint)
            raise CloudAuthenticationError("SEMS authentication was rejected")
        delayed = self._record_login_retry_after(resp)
        if resp.status_code == 429 or delayed:
            _LOGGER.debug("SEMS login diagnostic: endpoint=%s reason=server_backoff", endpoint)
            return None
        if resp.status_code in (404, 405, 408, 500, 502, 503, 504):
            raise _LoginFallbackEligible("Login endpoint temporarily unavailable")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as error:
            _LOGGER.debug("SEMS login diagnostic: endpoint=%s reason=non_json_response", endpoint)
            raise _LoginFallbackEligible("Login response is not JSON") from error
        if not isinstance(payload, dict):
            _LOGGER.debug(
                "SEMS login diagnostic: endpoint=%s reason=invalid_response_type response_type=%s",
                endpoint, type(payload).__name__,
            )
            raise _LoginFallbackEligible("Login response has an unexpected shape")
        code = payload.get("code")
        safe_code = (
            str(code) if type(code) in (int, str) and str(code).isascii()
            and str(code).isdigit() and len(str(code)) <= 6 else
            "absent" if code is None else "nonstandard"
        )
        _LOGGER.debug("SEMS login diagnostic: endpoint=%s business_code=%s", endpoint, safe_code)
        if code not in (0, "0", "00000", None) or payload.get("hasError"):
            _LOGGER.warning(
                "SEMS web login failed: code=%s translation=%s description=%s",
                code, payload.get("translationCode"),
                payload.get("description") or payload.get("msg"),
            )
            # Unknown business failures (including account/rate-limit errors)
            # must not fall through to a second login endpoint.
            if str(code) == "100004" and payload.get("translationCode") in (
                None, "", "parameter_error"
            ):
                raise _LoginFallbackEligible("Login protocol rejected")
            return None
        data = payload.get("data")
        # Log structure and fixed categories, never login bodies, token values or
        # arbitrary server strings (even a client field can contain private data).
        if not isinstance(data, dict):
            reason = "invalid_data_type"
            token_present = False
            client_kind = "unavailable"
        else:
            token_present = bool(data.get("token"))
            client = data.get("client", "semsPlusWeb")
            client_kind = (
                "missing_default" if "client" not in data else
                "expected" if client == "semsPlusWeb" else
                "other_string" if isinstance(client, str) else "other_type"
            )
            reason = (
                "missing_token" if not token_present else
                "unexpected_client" if client != "semsPlusWeb" else "session_shape_accepted"
            )
        _LOGGER.debug(
            "SEMS login diagnostic: endpoint=%s reason=%s data_type=%s token_present=%s client_kind=%s",
            endpoint, reason, type(data).__name__, token_present, client_kind,
        )
        # Endpoint identity does not determine its position in the login order.
        # Explicit success without a session permits the one remaining endpoint;
        # rejection, ambiguous success or another client must not trigger it.
        if (reason == "missing_token"
                and code in (0, "0", "00000")
                and client_kind in ("missing_default", "expected")):
            raise _LoginFallbackEligible("Successful login omitted its token")
        if reason != "session_shape_accepted":
            return None
        # Common API returns api at top level; original login returns it in data.
        # Require a validated region from either endpoint; never guess EU.
        gateway = urlsplit((data.get("api") if endpoint == "original" else payload.get("api")) or "")
        if (gateway.scheme != "https" or not gateway.hostname
                or not gateway.hostname.endswith(".semsportal.com")
                or gateway.username or gateway.password or gateway.query
                or gateway.fragment or gateway.port not in (None, 443)
                or gateway.path.rstrip("/") not in ("/sems", "/web/sems")):
            raise ValueError("Invalid regional SEMS gateway in login response")
        return dict(data, api=f"https://{gateway.hostname}/web/sems")

    def _fetch_web_token(self) -> dict | None:
        """Try original login, then one eligible common fallback within one deadline.

        Both endpoints feed the same serialized web session and failure backoff.
        HTTP authentication rejection, throttling, unknown business failures and
        invalid regional routing never trigger the fallback. This method does
        not retry device commands.
        """
        deadline = time.monotonic() + _RequestTimeout
        for position, endpoint in enumerate(_LOGIN_ORDER):
            try:
                return self._login_attempt(endpoint, deadline)
            except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
                raise
            except (_LoginFallbackEligible, requests.ConnectionError, requests.Timeout):
                if position + 1 < len(_LOGIN_ORDER):
                    _LOGGER.debug(
                        "SEMS %s login unavailable; trying %s login once",
                        endpoint, _LOGIN_ORDER[position + 1],
                    )
            except Exception as exc:
                # Preserve the public failure result for malformed credentials or
                # responses; unexpected failures never justify another endpoint.
                _LOGGER.warning("SEMS web login exception: %s", exc)
                return None
        return None

    @_serialized_web_request
    def _ensure_web_token(self, renew: bool = False) -> bool:
        """Share failed-login backoff across polling, MQTT and user commands."""
        if renew:
            self._web_token = None
        if self._web_token is not None:
            return True
        if self._web_login_auth_error:
            raise CloudAuthenticationError("SEMS authentication was rejected")
        if time.monotonic() < self._web_login_retry_at:
            _LOGGER.debug("SEMS login diagnostic: reason=local_backoff")
            return False
        self._web_retry_after = 0.0
        self.login_attempts += 1
        try:
            tok = self._fetch_web_token()
        except CloudAuthenticationError:
            self._web_login_auth_error = True
            raise
        if not isinstance(tok, dict) or not tok.get("token"):
            self._web_login_retry_at = time.monotonic() + max(
                self._web_login_delay, self._web_retry_after
            )
            self._web_login_delay = min(self._web_login_delay * 2, 300.0)
            return False
        self._web_token = tok
        self.successful_logins += 1
        self.last_login_at = time.monotonic()
        self._web_login_retry_at = 0.0
        self._web_login_delay = 30.0
        api = (tok.get("api") or "").rstrip("/")
        if api:
            self._web_api_base = api
        return True

    def _invalidate_rejected_session(self):
        """Count server-rejected sessions that initiate bounded recovery.

        Called under the shared request lock. Diagnostics retain counters only,
        never tokens, credentials or server response bodies.
        """
        self.session_recovery_attempts += 1
        self._web_token = None

    def _eu_url(self, path: str) -> str:
        """Build a full EU gateway URL from a relative path."""
        return f"{self._web_api_base}/{path.lstrip('/')}"

    def _build_web_headers(self) -> dict:
        """Build headers for SEMS Plus EU gateway (requires x-signature).

        Uses the shared semsPlusWeb token obtained from either login endpoint.
        Algorithm (from semsplus.goodwe.com JS bundle):
          x-signature = base64(SHA256(timestamp_ms + '@' + uid + '@' + token) + '@' + timestamp_ms)
        """
        if not self._ensure_web_token():
            raise OutOfRetries("Could not obtain SEMS Plus web token")
        ts = str(int(time.time() * 1000))
        uid = self._web_token.get("uid", "") if self._web_token else ""
        tok = self._web_token.get("token", "") if self._web_token else ""
        digest = hashlib.sha256(f"{ts}@{uid}@{tok}".encode()).hexdigest()
        x_signature = base64.b64encode(f"{digest}@{ts}".encode()).decode()
        return {
            "User-Agent": SEMS_USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "token": json.dumps(self._web_token),
            "client": "semsPlusWeb",
            "neutral": "0",
            "currentlang": "en",
            "x-signature": x_signature,
        }

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @_serialized_web_request
    def test_authentication(self) -> bool:
        """Test if we can authenticate with the EU gateway."""
        try:
            ok = self._ensure_web_token(renew=True)
            _LOGGER.debug("SEMS authentication result: %s", ok)
            return ok
        except CloudRateLimitedError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("SEMS Authentication exception: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Gen2 / SEMS-Plus EU gateway support
    # ------------------------------------------------------------------

    @property
    def cached_device_identity(self) -> dict[str, str]:
        """Return known device identity without requesting cloud data."""
        return {"plant_id": self._plant_id or "", "product_model": self._product_model or ""}

    def configure_gen2(self, plant_id: str | None, product_model: str | None = None) -> None:
        """Supply gen2 plant info from config (call this after init if available)."""
        self._plant_id = plant_id or None
        self._product_model = product_model or None
        _LOGGER.debug(
            "SEMS gen2 config: plant_id=%s, product_model=%s",
            self._plant_id,
            self._product_model,
        )

    def _try_fetch_plant_id(self) -> str | None:
        """Auto-detect plantId via EU gateway stations list (single-plant accounts only)."""
        try:
            stations = self.fetch_stations()
            if len(stations) == 1:
                return str(stations[0].get("id") or "")
            if len(stations) > 1:
                _LOGGER.info(
                    "SEMS: multiple power stations found (%d), cannot auto-detect plantId. "
                    "Set plant_id manually in integration options.",
                    len(stations),
                )
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("SEMS plantId auto-detect failed: %s", exc)
        return None

    def _ensure_plant_id(self) -> str | None:
        """Return cached plantId, fetching it first if not yet known."""
        if self._plant_id is None:
            self._plant_id = self._try_fetch_plant_id()
        return self._plant_id

    @_serialized_web_request
    def set_charge_mode_gen2(
        self,
        wallboxSn,
        mode,
        chargePower=None,
        ensure_minimum_charging_power: bool | None = None,
        renewToken: bool = False,
        maxTokenRetries: int = 1,
        max_energy: int | None = None,
        min_energy: int | None = None,
        soc_target: int | None = None,
        finish_time: str | None = None,
    ):
        """Set charge mode/power exclusively via EU gateway (Gen2 / HCA series).

        Optional per-mode params:
          max_energy  -- maxEnergy (kWh), 0 = unlimited (mode 0 and 2)
          min_energy  -- minEnergy (kWh), 0 = no minimum (mode 2 only)
          soc_target  -- soc (%), 0 = no SOC stop condition (mode 0 and 2)
          finish_time -- finishTime string: "0"=ASAP, "1".."6"=hours (mode 1 and 2)

        Skips the legacy semsportal.com SetChargeMode call entirely -- this avoids
        the wallbox being "busy" when the EU gateway set-mode arrives.
        Only EU gateway set-mode is attempted; a second encoder is not used as
        a fallback for an uncertain command.
        """
        _LOGGER.debug(
            "SEMS set_charge_mode_gen2(sn=%s, mode=%s, power=%s, ensure_min=%s, renewToken=%s, retries=%s)",
            wallboxSn,
            mode,
            chargePower,
            ensure_minimum_charging_power,
            renewToken,
            maxTokenRetries,
        )
        try:
            if maxTokenRetries < 0:
                raise OutOfRetries

            plant_id = self._ensure_plant_id()
            if not plant_id:
                _LOGGER.error(
                    "SEMS gen2: no plant_id -- cannot set charge mode without EU gateway plant_id"
                )
                return False

            if not self._ensure_web_token(renew=renewToken):
                _LOGGER.error("SEMS gen2: cannot obtain web token")
                return False

            headers = self._build_web_headers()
            payload: dict = {
                "sn": wallboxSn,
                "plantId": plant_id,
                "mode": mode,
            }
            if self._product_model:
                payload["productModel"] = self._product_model
            if chargePower is not None:
                # Send both field names -- gen1 uses chargePowerSetted, gen2 uses chargeMaxPower
                payload["chargePowerSetted"] = float(chargePower)
                payload["chargeMaxPower"] = float(chargePower)
            if ensure_minimum_charging_power is not None:
                payload["ensureMinimumChargingPower"] = ensure_minimum_charging_power
            if max_energy is not None:
                payload["maxEnergy"] = int(max_energy)
            if min_energy is not None:
                payload["minEnergy"] = int(min_energy)
            if soc_target is not None:
                payload["soc"] = int(soc_target)
            if finish_time is not None:
                payload["finishTime"] = str(finish_time)

            _eu_set_mode_url = self._eu_url(_PATH_SET_MODE)
            _LOGGER.debug(
                "SEMS gen2 set-mode (exclusive): POST %s payload=%s",
                _eu_set_mode_url, payload,
            )
            request_started = time.monotonic()
            try:
                set_success = False
                for attempt in range(1, _SetModeR0305Retries + 2):
                    resp = self._request_gate.request(requests.post,
                        _eu_set_mode_url,
                        headers=headers,
                        json=payload,
                        timeout=request_timeout(_SetModeTimeout),
                    )
                    _LOGGER.debug(
                        "SEMS gen2 set-mode (attempt %d): HTTP %s body=%s",
                        attempt, resp.status_code, resp.text,
                    )
                    rj = _response_json(resp)
                    code = str(rj.get("code") or "")
                    if _command_succeeded(rj):
                        _LOGGER.info(
                            "SEMS gen2 set-mode succeeded (sn=%s, mode=%s, power=%s, attempt=%d)",
                            wallboxSn, mode, chargePower, attempt,
                        )
                        set_success = True
                        break
                    if code == "C0602" and maxTokenRetries > 0:
                        _LOGGER.debug(
                            "SEMS gen2 set-mode C0602 (session expired), renewing web token and retrying"
                        )
                        self._invalidate_rejected_session()
                        return self.set_charge_mode_gen2(
                            wallboxSn, mode, chargePower=chargePower,
                            ensure_minimum_charging_power=ensure_minimum_charging_power,
                            renewToken=True, maxTokenRetries=maxTokenRetries - 1,
                            max_energy=max_energy, min_energy=min_energy,
                            soc_target=soc_target, finish_time=finish_time,
                        )
                    if code == "R0305":
                        # Transient "remote_control_fail" -- retry after short delay
                        if attempt <= _SetModeR0305Retries:
                            _LOGGER.debug(
                                "SEMS gen2 set-mode R0305 (remote_control_fail), "
                                "retrying in %.1fs (attempt %d/%d)",
                                _SetModeR0305Delay, attempt, _SetModeR0305Retries,
                            )
                            retry_delay(_SetModeR0305Delay)
                            continue
                        _LOGGER.warning(
                            "SEMS gen2 set-mode R0305 persisted after %d attempts (sn=%s)",
                            _SetModeR0305Retries, wallboxSn,
                        )
                    else:
                        _LOGGER.warning(
                            "SEMS gen2 set-mode non-success code=%s body=%s",
                            code, resp.text[:300],
                        )
                    break

                if not set_success:
                    return False

                return True
            except requests.exceptions.Timeout as error:
                _LOGGER.warning(
                    "SEMS gen2 set-mode response timed out after %.1fs; "
                    "device outcome is unknown (sn=%s)",
                    time.monotonic() - request_started, wallboxSn,
                )
                raise TimeoutError("Cloud setting response timed out; outcome unknown") from error
        except OutOfRetries:
            raise
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to execute gen2 SetChargeMode command. %s", exc)
            return False

    @_serialized_web_request
    def fetch_mqtt_settings(self):
        """Fetch short-lived push credentials without exposing them in logs.

        Returns:
            Validated WSS broker and MQTT credentials for this account's region.

        Raises:
            ConnectionError: Push settings are unavailable or authorization expired.
            ValueError: The returned broker or credentials are invalid.
        """
        if not self._ensure_web_token():
            raise ConnectionError("Cloud event login unavailable")
        for attempt in range(2):
            try:
                headers = self._build_web_headers()
            except OutOfRetries as err:
                raise ConnectionError("Cloud event login unavailable") from err
            response = self._request_gate.request(requests.get,
                self._eu_url("sems-plant/api/second-data/config"),
                headers=headers, timeout=request_timeout(15),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid cloud event response")
            if str(payload.get("code")) == "C0602" and attempt == 0:
                self._invalidate_rejected_session()
                continue
            if str(payload.get("code")) != "00000":
                raise ConnectionError("Cloud event credentials unavailable")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("Missing cloud event credentials")
            data = dict(data)
            break
        else:
            raise ConnectionError("Cloud event authorization expired")
        if not data.get("brokerUrl"):
            # The official frontend uses regional deployment config as fallback.
            host = urlsplit(self._web_api_base).hostname or ""
            region = host.split("-", 1)[0]
            if region not in {"eu", "au", "hk", "us", "cn"}:
                raise ValueError("Unknown cloud event region")
            response = self._request_gate.request(requests.get, "https://semsplus.goodwe.com/config.js",
                                                  headers={"User-Agent": SEMS_USER_AGENT}, timeout=request_timeout(15))
            response.raise_for_status()
            match = re.search(r'"mqttUrlPolling"\s*:\s*\{([^}]+)\}', response.text)
            regional = re.search(r'"' + region + r'"\s*:\s*"(wss://[^"\s]+)"',
                                 match.group(1) if match else "")
            if not regional:
                raise ValueError("No regional cloud event broker")
            data["brokerUrl"] = regional.group(1)
        broker = urlsplit(data["brokerUrl"])
        if (broker.scheme != "wss" or not broker.hostname
                or not broker.hostname.endswith(".goodwe-power.com")
                or broker.username or broker.password or broker.query or broker.fragment):
            raise ValueError("Invalid cloud event broker")
        for key in ("clientId", "userName", "password"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError("Missing cloud event credential field")
        return {key: data[key] for key in ("brokerUrl", "clientId", "userName", "password")}

    def _observation_token(self):
        """Reuse the serialized web session and its bounded login recovery."""
        if not self._ensure_web_token():
            raise ConnectionError("Shared SEMS session is unavailable")
        return dict(self._web_token)

    @_serialized_web_request
    def fetch_status_observation(self, wallbox_sn):
        """Read independent timestamped telemetry; never infer freshness from HTTP time."""
        return self._observation_reader.read(wallbox_sn)

    @_serialized_web_request
    def get_data_gen2(self, wallbox_sn: str) -> dict | None:
        """Fetch device status from EU gateway ev-charger/detail (Gen2 / HCA series).

        Returns None on any failure -- the coordinator will mark the update as
        failed and retry on the next poll interval.
        """
        if not self._ensure_web_token():
            _LOGGER.warning("SEMS gen2 getData: no web token")
            return None

        plant_id = self._ensure_plant_id()
        headers = self._build_web_headers()
        payload: dict = {"sn": wallbox_sn}
        if plant_id:
            payload["plantId"] = plant_id
        if self._product_model:
            payload["productModel"] = self._product_model

        try:
            _eu_detail_url = self._eu_url(_PATH_DETAIL)
            _LOGGER.debug(
                "SEMS gen2 getData: POST %s payload=%s", _eu_detail_url, payload
            )
            resp = self._request_gate.request(requests.post,
                _eu_detail_url, headers=headers, json=payload, timeout=request_timeout(_RequestTimeout)
            )
            rj = _response_json(resp)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "SEMS gen2 getData: POST %s -> HTTP %s\n%s",
                    _eu_detail_url, resp.status_code,
                    json.dumps(rj, indent=2, ensure_ascii=False),
                )
            code = str(rj.get("code") or "")
            raw = rj.get("data")

            if code == "C0602":
                _LOGGER.debug("SEMS gen2 getData: C0602, renewing web token")
                self._invalidate_rejected_session()
                if not self._ensure_web_token(renew=True):
                    _LOGGER.error("SEMS gen2 getData: could not renew web token")
                    return None
                headers = self._build_web_headers()
                resp = self._request_gate.request(requests.post,
                    _eu_detail_url, headers=headers, json=payload, timeout=request_timeout(_RequestTimeout)
                )
                rj = _response_json(resp)
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "SEMS gen2 getData (retry): POST %s -> HTTP %s\n%s",
                        _eu_detail_url, resp.status_code,
                        json.dumps(rj, indent=2, ensure_ascii=False),
                    )
                code = str(rj.get("code") or "")
                raw = rj.get("data")

            if code not in ("00000", "0") or not raw:
                _LOGGER.warning(
                    "SEMS gen2 getData: unexpected code=%s, no data returned", code
                )
                return None

            # Map EU gateway fields -> internal dict format.
            # Field names are inferred; all raw keys are logged above so we can
            # expand this mapping as the response format becomes clear.
            def _get(*keys, default=None):
                for k in keys:
                    v = raw.get(k)
                    if v is not None:
                        return v
                return default

            result: dict = {
                "sn": wallbox_sn,
                "name": _get("name", "deviceName", default="EV Charger"),
                "status": _get("status", "statusCode", "chargeStatus", default="unknown"),
                "workstate": _get("workstate", "workState", "carState", default="unknown"),
                "model": _get("model", "deviceModel", "productModel", default=self._product_model or ""),
                "fireware": _get("fireware", "firmware", "softwareVersion", default=""),
                "last_fireware": _get("last_fireware", "lastFirmware", default=""),
                "lastUpdate": _get("lastUpdate", "updateTime", "reportTime", default=""),
                "chargeEnergy": _get("chargeEnergy", "chargedEnergy", "totalEnergy", default="0"),
                "power": _get("power", "chargePower", "activePower", default="0"),
                "current": _get("current", "chargeCurrent", default="0"),
                "time": _get("time", "chargeTime", default="0"),
                "startStatus": _get("startStatus", "isCharging", default=False),
                "chargeMode": _get("chargeMode", "mode", "workMode", default=0),
                "_reported_charge_mode": _get("chargeMode", "mode", "workMode", default=None),
                "scheduleMode": _get("scheduleMode", default=0),
                "schedule_hour": _get("schedule_hour", "scheduleHour", default=0),
                "schedule_minute": _get("schedule_minute", "scheduleMinute", default=0),
                "schedule_total_minute": _get("schedule_total_minute", "scheduleTotalMinute", default=0),
                "set_charge_power": _get("chargePowerSetted", "chargeMaxPower", default=None),
                "max_charge_power": _get("max_charge_power", "maxChargePower", default=None),
                "min_charge_power": _get("min_charge_power", "minChargePower", default=None),
                "charge_from_grid": _get("charge_from_grid", "chargeFromGrid", default=1),
                "isOpen": _get("isOpen", "isConnected", default=False),
                "currentLimit": _get("currentLimit", "currentLimitValue", default=None),
                # Metadata is read at discovery and before explicit current writes,
                # never fetched again for every telemetry poll.
                "controlItemRanges": self._control_item_ranges.get(wallbox_sn, False),
                # Per-mode charging targets (0 = unlimited / no target)
                "max_energy": _get("maxEnergy", default=None),
                "min_energy": _get("minEnergy", default=None),
                "charge_target_soc": _get("soc", default=None),
                "finish_time": _get("finishTime", default=None),
                # Configuration absence is unknown, never an observed zero/off.
                "ensure_minimum_charging_power": _plug_and_charge_state(
                    raw.get("ensureMinimumChargingPower")
                ),
                "plug_and_charge": _plug_and_charge_state(raw.get("chargedNow")),
                "dynamicLoad": _plug_and_charge_state(raw.get("dynamicLoad")),
                "phaseSwitch": _plug_and_charge_state(raw.get("phaseSwitch")),
                # Hardware-rated max charge power (kW) -- informational, read-only
                "rated_max_charge_power": _get("ratedMaxiChargePower", "ratedMaxChargePower", default=None),
                # Physical hardware maximum (unchangeable device spec, used as slider ceiling)
                "hw_max_charge_power": _get("ratedMaxChargePower", default=None),
            }
            # Preserve presence: an unknown connection code must not silently
            # fall back to the stale SEMS+ workState text.
            if "vehConnStu" in raw:
                result["vehConnStu"] = raw["vehConnStu"]
            return result

        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("SEMS gen2 getData failed: %s", exc)
            return None

    @_serialized_web_request
    def set_config_gen2(self, wallbox_sn: str, **props) -> bool:
        """POST arbitrary properties to EU gateway set-config (Gen2).

        Use for single-property toggles such as:
          set_config_gen2(sn, chargedNow=1)       # enable Plug & Charge
          set_config_gen2(sn, chargedNow=0)         # disable Plug & Charge
          set_config_gen2(sn, dynamicLoad=1)        # enable Dynamic Load Control
          set_config_gen2(sn, phaseSwitch=1)        # switch to single-phase
          set_config_gen2(sn, currentLimit=16)      # import current limit (A)

        The current SEMS+ frontend sends chargedNow as 0/1. Do not copy legacy
        0xAA read representations into this endpoint's Plug and Charge writes.
        """
        plant_id = self._ensure_plant_id()
        if not plant_id:
            _LOGGER.error("SEMS gen2 set_config: no plant_id (sn=%s)", wallbox_sn)
            return False
        if not self._ensure_web_token():
            _LOGGER.error("SEMS gen2 set_config: cannot obtain web token")
            return False
        headers = self._build_web_headers()
        payload: dict = {"sn": wallbox_sn, "plantId": plant_id}
        if self._product_model:
            payload["productModel"] = self._product_model
        payload.update(props)
        url = self._eu_url(_PATH_SET_CONFIG)
        _LOGGER.debug("SEMS gen2 set_config: POST %s payload=%s", url, payload)
        try:
            resp = self._request_gate.request(requests.post, url, headers=headers, json=payload, timeout=request_timeout(_RequestTimeout))
            _LOGGER.debug(
                "SEMS gen2 set_config: HTTP %s body=%s", resp.status_code, resp.text[:300]
            )
            rj = _response_json(resp)
            code = str(rj.get("code") or "")
            if code == "C0602":
                _LOGGER.debug("SEMS gen2 set_config: C0602, renewing token and retrying")
                self._invalidate_rejected_session()
                if not self._ensure_web_token(renew=True):
                    return False
                headers = self._build_web_headers()
                resp = self._request_gate.request(requests.post, url, headers=headers, json=payload, timeout=request_timeout(_RequestTimeout))
                rj = _response_json(resp)
                code = str(rj.get("code") or "")
            ok = _command_succeeded(rj)
            if ok:
                _LOGGER.info("SEMS gen2 set_config succeeded (sn=%s props=%s)", wallbox_sn, props)
            else:
                _LOGGER.warning(
                    "SEMS gen2 set_config non-success code=%s body=%s",
                    code, resp.text[:300],
                )
            return ok
        except requests.exceptions.Timeout:
            _LOGGER.warning("SEMS gen2 set_config timed out (sn=%s)", wallbox_sn)
            return False
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS gen2 set_config failed: %s", exc)
            return False

    @_serialized_web_request
    def change_status_gen2(self, wallbox_sn: str, action: str) -> bool:
        """Start or stop charging via EU gateway (Gen2 / HCA series).

        action: "start" -> startCharge endpoint; "stop" -> stopCharge endpoint.
        Payload identical to set-mode: sn + plantId + productModel.
        """
        path = _PATH_START_CHARGE if action == "start" else _PATH_STOP_CHARGE
        plant_id = self._ensure_plant_id()
        if not plant_id:
            _LOGGER.error("SEMS gen2 change_status: no plant_id, cannot %s charging", action)
            return False
        if not self._ensure_web_token():
            _LOGGER.error("SEMS gen2 change_status: cannot obtain web token")
            return False
        headers = self._build_web_headers()
        payload: dict = {"sn": wallbox_sn, "plantId": plant_id}
        if self._product_model:
            payload["productModel"] = self._product_model
        url = self._eu_url(path)
        _LOGGER.debug("SEMS gen2 %sCharge: POST %s payload=%s", action, url, payload)
        try:
            resp = self._request_gate.request(requests.post, url, headers=headers, json=payload, timeout=request_timeout(_SetModeTimeout))
            _LOGGER.debug(
                "SEMS gen2 %sCharge: HTTP %s body=%s", action, resp.status_code, resp.text
            )
            rj = _response_json(resp)
            code = str(rj.get("code") or "")
            if code == "C0602":
                _LOGGER.debug("SEMS gen2 %sCharge: C0602, renewing token and retrying", action)
                self._invalidate_rejected_session()
                if not self._ensure_web_token(renew=True):
                    return False
                headers = self._build_web_headers()
                resp = self._request_gate.request(requests.post, url, headers=headers, json=payload, timeout=request_timeout(_SetModeTimeout))
                rj = _response_json(resp)
                code = str(rj.get("code") or "")
            ok = _command_succeeded(rj)
            if ok:
                _LOGGER.info("SEMS gen2 %sCharge succeeded (sn=%s)", action, wallbox_sn)
            else:
                _LOGGER.warning(
                    "SEMS gen2 %sCharge non-success code=%s body=%s",
                    action, code, resp.text[:300],
                )
            return ok
        except requests.exceptions.Timeout:
            _LOGGER.warning("SEMS gen2 %sCharge timed out (sn=%s)", action, wallbox_sn)
            return False
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS gen2 %sCharge failed: %s", action, exc)
            return False

    @_serialized_web_request
    def fetch_last_charge(self, wallbox_sn: str) -> dict | None:
        """Fetch last charge session from EU gateway (GET getLastCharge).

        Returns a dict with:
          - ``last_charge_work_status`` (int): 6 = actively charging, other = not charging
          - ``last_charge_power`` (float): actual EV power draw in kW (pevChar)
        Returns None on any error (non-blocking -- detail data is still valid).
        """
        plant_id = self._ensure_plant_id()
        if not plant_id:
            _LOGGER.debug("fetch_last_charge: no plant_id, skipping")
            return None
        if not self._ensure_web_token():
            _LOGGER.debug("fetch_last_charge: no web token, skipping")
            return None
        headers = self._build_web_headers()
        url = self._eu_url(_PATH_GET_LAST_CHARGE)
        params = {"chargeSn": wallbox_sn, "pwId": plant_id}
        try:
            resp = self._request_gate.request(requests.get, url, headers=headers, params=params, timeout=request_timeout(_RequestTimeout))
            rj = _response_json(resp)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "fetch_last_charge: GET %s -> HTTP %s\n%s",
                    resp.url, resp.status_code,
                    json.dumps(rj, indent=2, ensure_ascii=False),
                )
            code = str(rj.get("code") or "")
            if code == "C0602":
                self._invalidate_rejected_session()
                if not self._ensure_web_token(renew=True):
                    return None
                headers = self._build_web_headers()
                resp = self._request_gate.request(requests.get, url, headers=headers, params=params, timeout=request_timeout(_RequestTimeout))
                rj = _response_json(resp)
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "fetch_last_charge (retry): GET %s -> HTTP %s\n%s",
                        resp.url, resp.status_code,
                        json.dumps(rj, indent=2, ensure_ascii=False),
                    )
                code = str(rj.get("code") or "")
            if code != "00000":
                _LOGGER.debug("fetch_last_charge: non-success code=%s", code)
                return None
            log = (rj.get("data") or {}).get("chargeLog") or {}
            return {
                "last_charge_work_status": log.get("workStu"),
                "last_charge_power": log.get("pevChar"),
                "last_charge_duration_minutes": log.get("chargeTimeLength"),
                "last_charge_energy": log.get("currentChargeQuantity"),
            }
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("fetch_last_charge failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # EU gateway discovery (used during config flow)
    # ------------------------------------------------------------------

    @_serialized_web_request
    def fetch_device_info(self, wallbox_sn: str) -> dict:
        """Fetch device metadata (productModel, ratedPower, etc.) from the EU gateway.

        GET /sems-remote/api/ev-charger/control-item-content-list/{sn}
        Returns a dict with at least 'productModel' (empty string on failure).
        """
        if not self._ensure_web_token():
            return {}
        headers = self._build_web_headers()
        try:
            resp = self._request_gate.request(requests.get,
                f"{self._eu_url(_PATH_CONTROL_ITEMS)}/{wallbox_sn}",
                headers=headers,
                timeout=request_timeout(_RequestTimeout),
            )
            rj = _response_json(resp)
            _LOGGER.debug("SEMS fetch_device_info raw: %s", rj)
            if str(rj.get("code") or "") not in ("00000", "0"):
                return {}
            info = rj.get("data")
            if not isinstance(info, dict) or not info:
                return {}
            if info.get("sn") is not None and info["sn"] != wallbox_sn:
                return {}
            self._control_item_ranges[wallbox_sn] = info.get("controlItemRanges")
            return info
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("SEMS fetch_device_info failed: %s", exc)
            return {}

    @_serialized_web_request
    def fetch_stations(self) -> list[dict]:
        """Return list of plants/stations from the EU gateway.

        Each dict contains at least 'id' and 'name' (best-effort -- field names
        are inferred; raw response is logged at DEBUG for diagnostics).
        Returns an empty list on any error.
        """
        if not self._ensure_web_token():
            _LOGGER.warning("SEMS fetch_stations: no web token")
            return []
        headers = self._build_web_headers()
        try:
            resp = self._request_gate.request(requests.post,
                self._eu_url(_PATH_STATIONS_PAGE),
                headers=headers,
                json={"current": 1, "size": 50},
                timeout=request_timeout(_RequestTimeout),
            )
            rj = _response_json(resp)
            _LOGGER.debug("SEMS fetch_stations raw: %s", rj)
            data = rj.get("data") or {}
            if isinstance(data, list):
                records = data
            else:
                # Response uses dataList (centralized endpoint) or records/list
                records = (
                    data.get("dataList")
                    or data.get("records")
                    or data.get("list")
                    or data.get("data")
                    or []
                )
            # Normalise: ensure each record has 'id' and 'name'
            result = []
            for r in (records if isinstance(records, list) else []):
                sid = (
                    r.get("stationId")
                    or r.get("id")
                    or r.get("plantId")
                    or r.get("powerStationId")
                )
                name = r.get("stationName") or r.get("name") or str(sid)
                if sid:
                    result.append({"id": sid, "name": name, **r})
            return result
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS fetch_stations failed: %s", exc)
            return []

    @_serialized_web_request
    def fetch_ev_chargers(self, station_id: str | None = None) -> list[dict]:
        """Return list of EV chargers from the EU gateway.

        If *station_id* is provided the request is scoped to that plant.
        Each dict contains at least 'sn' (and optionally 'name', 'model',
        'stationId').  Raw response is logged at DEBUG for diagnostics.
        Returns an empty list on any error.

        Response structure (centralized/page endpoint):
          data.dataList[]
            .stationId, .stationName
            .children[]
              .sn, .name, .deviceType, .stationId
        """
        if not self._ensure_web_token():
            _LOGGER.warning("SEMS fetch_ev_chargers: no web token")
            return []
        headers = self._build_web_headers()
        payload: dict = {"deviceTypeList": ["EV_CHARGER"], "current": 1, "size": 50}
        if station_id:
            payload["stationId"] = station_id
        try:
            resp = self._request_gate.request(requests.post,
                self._eu_url(_PATH_CENTRALIZED_PAGE),
                headers=headers,
                json=payload,
                timeout=request_timeout(_RequestTimeout),
            )
            rj = _response_json(resp)
            _LOGGER.debug("SEMS fetch_ev_chargers raw: %s", rj)
            data = rj.get("data") or {}

            # Primary structure: data.dataList[].children[]
            data_list = data.get("dataList") if isinstance(data, dict) else None
            if data_list:
                chargers = []
                for station in data_list:
                    for child in (station.get("children") or []):
                        if child.get("deviceType") == "EV_CHARGER" or child.get("sn"):
                            # Enrich child with stationId if missing
                            if not child.get("stationId"):
                                child["stationId"] = station.get("stationId")
                            chargers.append(child)
                if chargers:
                    return chargers

            # Fallback: flat records/list/data
            if isinstance(data, list):
                return data
            records = (
                data.get("records")
                or data.get("list")
                or data.get("data")
                or []
            )
            return records if isinstance(records, list) else []
        except (CloudAuthenticationError, CloudRateLimitedError, BudgetCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS fetch_ev_chargers failed: %s", exc)
            return []


class OutOfRetries(exceptions.HomeAssistantError):
    """Error to indicate too many error attempts."""
