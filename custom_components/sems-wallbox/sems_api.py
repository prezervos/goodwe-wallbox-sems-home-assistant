import base64
import hashlib
import json
import logging
import time

import requests
from homeassistant import exceptions

_LOGGER = logging.getLogger(__name__)

API_VERSION = "1.4.0"

_WebLoginURL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"

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
_SetModeR0305Retries = 3   # retry count on R0305 (remote_control_fail — transient)
_SetModeR0305Delay = 2.0   # seconds between R0305 retries

_DefaultHeaders = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "token": '{"version":"","client":"semsPlusAndroid","language":"en"}',
}


class SemsApi:
    """Interface to the SEMS API."""

    def __init__(self, hass, username, password):
        """Init SEMS API wrapper."""
        self._hass = hass
        self._username = username
        self._password = password
        self._web_token: dict | None = None  # semsPlusWeb token for EU gateway
        self._web_api_base: str = _EuGatewayBase  # overridden from login response
        # Gen2: cached plant info (auto-detected or user-supplied)
        self._plant_id: str | None = None
        self._product_model: str | None = None
        _LOGGER.info("SEMS API wrapper v%s initialized", API_VERSION)

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    def _fetch_web_token(self) -> dict | None:
        """Login to semsplus.goodwe.com to obtain a semsPlusWeb token.

        Password is sent as base64(MD5(password)) — observed from browser
        traffic capture.  The request is signed with an empty uid/token x-signature.
        """
        try:
            empty_token = json.dumps(
                {"uid": "", "timestamp": 0, "token": "",
                 "client": "semsPlusWeb", "version": "", "language": "en"}
            )
            ts = str(int(time.time() * 1000))
            digest = hashlib.sha256(f"{ts}@@".encode()).hexdigest()
            x_sig = base64.b64encode(f"{digest}@{ts}".encode()).decode()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "token": empty_token,
                "client": "semsPlusWeb",
                "neutral": "0",
                "currentlang": "en",
                "x-signature": x_sig,
            }
            pwd_md5_b64 = base64.b64encode(
                hashlib.md5(self._password.encode()).hexdigest().encode()
            ).decode()
            body = {
                "account": self._username,
                "pwd": pwd_md5_b64,
                "agreement": 1,
                "isLocal": False,
                "isChinese": False,
            }
            _LOGGER.debug("SEMS web login: POST %s", _WebLoginURL)
            resp = requests.post(
                _WebLoginURL, headers=headers, json=body, timeout=_RequestTimeout
            )
            resp.raise_for_status()
            rj = resp.json()
            _LOGGER.debug("SEMS web login response: %s", rj)
            code = rj.get("code")
            if code not in (0, "0", "00000", None) or rj.get("hasError"):
                _LOGGER.warning("SEMS web login failed: code=%s msg=%s", code, rj.get("msg"))
                return None
            data = rj.get("data") or {}
            _LOGGER.debug("SEMS web token received: client=%s", data.get("client"))
            return data
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("SEMS web login exception: %s", exc)
            return None

    def _ensure_web_token(self, renew: bool = False) -> bool:
        """Ensure we have a valid semsPlusWeb token."""
        if self._web_token is None or renew:
            tok = self._fetch_web_token()
            if tok is None:
                return False
            self._web_token = tok
            # Update base URL from login response (handles regional gateways)
            api = (tok.get("api") or "").rstrip("/")
            if api:
                self._web_api_base = api
        return True

    def _eu_url(self, path: str) -> str:
        """Build a full EU gateway URL from a relative path."""
        return f"{self._web_api_base}/{path.lstrip('/')}"

    def _build_web_headers(self) -> dict:
        """Build headers for SEMS Plus EU gateway (requires x-signature).

        Uses a dedicated semsPlusWeb token obtained from semsplus.goodwe.com.
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

    def test_authentication(self) -> bool:
        """Test if we can authenticate with the EU gateway."""
        try:
            ok = self._ensure_web_token(renew=True)
            _LOGGER.debug("SEMS v%s - test_authentication result: %s", API_VERSION, ok)
            return ok
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
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("SEMS plantId auto-detect failed: %s", exc)
        return None

    def _ensure_plant_id(self) -> str | None:
        """Return cached plantId, fetching it first if not yet known."""
        if self._plant_id is None:
            self._plant_id = self._try_fetch_plant_id()
        return self._plant_id

    def set_charge_mode_gen2(
        self,
        wallboxSn,
        mode,
        chargePower=None,
        ensure_minimum_charging_power: bool | None = None,
        renewToken: bool = False,
        maxTokenRetries: int = 1,
    ):
        """Set charge mode/power exclusively via EU gateway (Gen2 / HCA series).

        Skips the legacy semsportal.com SetChargeMode call entirely — this avoids
        the wallbox being "busy" when the EU gateway set-mode arrives.
        Only EU gateway set-mode is attempted (no set-config fallback) so we can
        cleanly test whether the old API was causing the 30s timeout.
        """
        _LOGGER.debug(
            "SEMS v%s - set_charge_mode_gen2(sn=%s, mode=%s, power=%s, ensure_min=%s, renewToken=%s, retries=%s)",
            API_VERSION,
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
                    "SEMS gen2: no plant_id — cannot set charge mode without EU gateway plant_id"
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
                payload["chargePowerSetted"] = float(chargePower)
            if ensure_minimum_charging_power is not None:
                payload["ensureMinimumChargingPower"] = ensure_minimum_charging_power

            _eu_set_mode_url = self._eu_url(_PATH_SET_MODE)
            _LOGGER.debug(
                "SEMS gen2 set-mode (exclusive): POST %s payload=%s",
                _eu_set_mode_url, payload,
            )
            try:
                for attempt in range(1, _SetModeR0305Retries + 2):
                    resp = requests.post(
                        _eu_set_mode_url,
                        headers=headers,
                        json=payload,
                        timeout=_SetModeTimeout,
                    )
                    _LOGGER.debug(
                        "SEMS gen2 set-mode (attempt %d): HTTP %s body=%s",
                        attempt, resp.status_code, resp.text,
                    )
                    rj = resp.json()
                    code = str(rj.get("code") or "")
                    if code in ("00000", "0") or rj.get("data") is True:
                        _LOGGER.info(
                            "SEMS gen2 set-mode succeeded (sn=%s, mode=%s, power=%s, attempt=%d)",
                            wallboxSn, mode, chargePower, attempt,
                        )
                        return True
                    if code == "C0602" and maxTokenRetries > 0:
                        _LOGGER.debug(
                            "SEMS gen2 set-mode C0602 (session expired), renewing web token and retrying"
                        )
                        self._web_token = None
                        return self.set_charge_mode_gen2(
                            wallboxSn, mode, chargePower=chargePower,
                            ensure_minimum_charging_power=ensure_minimum_charging_power,
                            renewToken=True, maxTokenRetries=maxTokenRetries - 1,
                        )
                    if code == "R0305":
                        # Transient "remote_control_fail" — retry after short delay
                        if attempt <= _SetModeR0305Retries:
                            _LOGGER.debug(
                                "SEMS gen2 set-mode R0305 (remote_control_fail), "
                                "retrying in %.1fs (attempt %d/%d)",
                                _SetModeR0305Delay, attempt, _SetModeR0305Retries,
                            )
                            time.sleep(_SetModeR0305Delay)
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
                return False
            except requests.exceptions.Timeout:
                _LOGGER.warning(
                    "SEMS gen2 set-mode timed out after %ss (sn=%s)",
                    _SetModeTimeout, wallboxSn,
                )
                return False
        except OutOfRetries:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to execute gen2 SetChargeMode command. %s", exc)
            return False

    def get_data_gen2(self, wallbox_sn: str) -> dict | None:
        """Fetch device status from EU gateway ev-charger/detail (Gen2 / HCA series).

        Returns None on any failure — the coordinator will mark the update as
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
            resp = requests.post(
                _eu_detail_url, headers=headers, json=payload, timeout=_RequestTimeout
            )
            _LOGGER.info(
                "SEMS gen2 getData: HTTP %s body=%s", resp.status_code, resp.text
            )
            rj = resp.json()
            code = str(rj.get("code") or "")
            raw = rj.get("data")

            if code == "C0602":
                _LOGGER.debug("SEMS gen2 getData: C0602, renewing web token")
                self._web_token = None
                if not self._ensure_web_token(renew=True):
                    _LOGGER.error("SEMS gen2 getData: could not renew web token")
                    return None
                headers = self._build_web_headers()
                resp = requests.post(
                    _eu_detail_url, headers=headers, json=payload, timeout=_RequestTimeout
                )
                _LOGGER.info(
                    "SEMS gen2 getData retry: HTTP %s body=%s", resp.status_code, resp.text
                )
                rj = resp.json()
                code = str(rj.get("code") or "")
                raw = rj.get("data")

            if code not in ("00000", "0") or not raw:
                _LOGGER.warning(
                    "SEMS gen2 getData: unexpected code=%s, no data returned", code
                )
                return None

            # Map EU gateway fields → internal dict format.
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
                "scheduleMode": _get("scheduleMode", default=0),
                "schedule_hour": _get("schedule_hour", "scheduleHour", default=0),
                "schedule_minute": _get("schedule_minute", "scheduleMinute", default=0),
                "schedule_total_minute": _get("schedule_total_minute", "scheduleTotalMinute", default=0),
                "set_charge_power": _get(
                    "set_charge_power", "chargePowerSetted", "ratedMaxiChargePower",
                    "chargePowerLimit", default=None,
                ),
                "max_charge_power": _get("max_charge_power", "maxChargePower", default=None),
                "min_charge_power": _get("min_charge_power", "minChargePower", default=None),
                "charge_from_grid": _get("charge_from_grid", "chargeFromGrid", default=1),
                "isOpen": _get("isOpen", "isConnected", default=False),
                "currentLimit": _get("currentLimit", "currentLimitValue", default=0.0),
                # ensureMinimumChargingPower: firmware may return 170 (0xAA) as
                # uninitialized sentinel — treat that as False (disabled).
                "ensure_minimum_charging_power": (
                    raw.get("ensureMinimumChargingPower") in (True, 1)
                ),
            }
            _LOGGER.debug("SEMS gen2 getData mapped result: %s", result)
            return result

        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("SEMS gen2 getData failed: %s", exc)
            return None

    def change_status_gen2(self, wallbox_sn: str, action: str) -> bool:
        """Start or stop charging via EU gateway (Gen2 / HCA series).

        action: "start" → startCharge endpoint; "stop" → stopCharge endpoint.
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
            resp = requests.post(url, headers=headers, json=payload, timeout=_SetModeTimeout)
            _LOGGER.debug(
                "SEMS gen2 %sCharge: HTTP %s body=%s", action, resp.status_code, resp.text
            )
            rj = resp.json()
            code = str(rj.get("code") or "")
            if code == "C0602":
                _LOGGER.debug("SEMS gen2 %sCharge: C0602, renewing token and retrying", action)
                self._web_token = None
                if not self._ensure_web_token(renew=True):
                    return False
                headers = self._build_web_headers()
                resp = requests.post(url, headers=headers, json=payload, timeout=_SetModeTimeout)
                rj = resp.json()
                code = str(rj.get("code") or "")
            ok = code in ("00000", "0") or rj.get("data") is True
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
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS gen2 %sCharge failed: %s", action, exc)
            return False

    # ------------------------------------------------------------------
    # EU gateway discovery (used during config flow)
    # ------------------------------------------------------------------

    _StationsPageURL    = None  # unused — use self._eu_url(_PATH_STATIONS_PAGE)
    _CentralizedPageURL = None  # unused — use self._eu_url(_PATH_CENTRALIZED_PAGE)
    _ControlItemURL     = None  # unused — use self._eu_url(_PATH_CONTROL_ITEMS)

    def fetch_device_info(self, wallbox_sn: str) -> dict:
        """Fetch device metadata (productModel, ratedPower, etc.) from the EU gateway.

        GET /sems-remote/api/ev-charger/control-item-content-list/{sn}
        Returns a dict with at least 'productModel' (empty string on failure).
        """
        if not self._ensure_web_token():
            return {}
        headers = self._build_web_headers()
        try:
            resp = requests.get(
                f"{self._eu_url(_PATH_CONTROL_ITEMS)}/{wallbox_sn}",
                headers=headers,
                timeout=_RequestTimeout,
            )
            rj = resp.json()
            _LOGGER.debug("SEMS fetch_device_info raw: %s", rj)
            if str(rj.get("code") or "") not in ("00000", "0"):
                return {}
            return rj.get("data") or {}
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("SEMS fetch_device_info failed: %s", exc)
            return {}

    def fetch_stations(self) -> list[dict]:
        """Return list of plants/stations from the EU gateway.

        Each dict contains at least 'id' and 'name' (best-effort — field names
        are inferred; raw response is logged at DEBUG for diagnostics).
        Returns an empty list on any error.
        """
        if not self._ensure_web_token():
            _LOGGER.warning("SEMS fetch_stations: no web token")
            return []
        headers = self._build_web_headers()
        try:
            resp = requests.post(
                self._eu_url(_PATH_STATIONS_PAGE),
                headers=headers,
                json={"current": 1, "size": 50},
                timeout=_RequestTimeout,
            )
            rj = resp.json()
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
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS fetch_stations failed: %s", exc)
            return []

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
            resp = requests.post(
                self._CentralizedPageURL,
                headers=headers,
                json=payload,
                timeout=_RequestTimeout,
            )
            rj = resp.json()
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
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("SEMS fetch_ev_chargers failed: %s", exc)
            return []


class OutOfRetries(exceptions.HomeAssistantError):
    """Error to indicate too many error attempts."""
