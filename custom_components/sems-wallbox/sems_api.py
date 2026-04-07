import base64
import hashlib
import json
import logging
import time

import requests
from homeassistant import exceptions

_LOGGER = logging.getLogger(__name__)

API_VERSION = "0.5.0"

_LoginURL = "https://www.semsportal.com/api/v3/Common/CrossLogin"

# v3/v4 endpoints for reading wallbox status
_WallboxURL_V3 = "https://www.semsportal.com/api/v3/EvCharger/GetCurrentChargeinfo"
_WallboxURL_V4 = "https://www.semsportal.com/api/v4/EvCharger/GetEvChargerMoreView"

# Toggle: set to True to prefer v4 endpoint (with automatic fallback to v3)
_USE_V4_STATUS = False

_SetChargeModeURL = "https://www.semsportal.com/api/v3/EvCharger/SetChargeMode"
_PowerControlURL = "https://www.semsportal.com/api/v3/EvCharger/Charging"

# Gen2 / SEMS-Plus EU gateway endpoints
# set-mode: sets both charge mode and power limit in one call (confirmed working: auth=token header,
#   field=chargePowerSetted, success code=00000; R000013=power<4.2kW; R0305=remote_control_fail)
_EuGatewaySetConfigURL = (
    "https://eu-gateway.semsportal.com/web/sems/sems-remote/api/ev-charger/set-config"
)
_EuGatewaySetModeURL = (
    "https://eu-gateway.semsportal.com/web/sems/sems-remote/api/ev-charger/set-mode"
)
# Used to auto-detect the plantId (power-station ID) associated with the wallbox
_GetPowerStationListURLPart = "/v1/PowerStation/GetPowerStationIdByOwner"

_RequestTimeout = 30  # seconds

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
        self._token: dict | None = None
        # Gen2: cached plant info (auto-detected or user-supplied)
        self._plant_id: str | None = None
        self._product_model: str | None = None
        _LOGGER.info(
            "SEMS API wrapper v%s initialized (status via %s)",
            API_VERSION,
            "v4" if _USE_V4_STATUS else "v3",
        )

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    def _fetch_login_token(self) -> dict | None:
        """Call CrossLogin and return token dict or None."""
        try:
            _LOGGER.debug("SEMS v%s - Getting API token", API_VERSION)
            login_data = json.dumps(
                {"account": self._username, "pwd": self._password}
            )
            login_response = requests.post(
                _LoginURL,
                headers=_DefaultHeaders,
                data=login_data,
                timeout=_RequestTimeout,
            )
            _LOGGER.debug("Login Response: %s", login_response)
            login_response.raise_for_status()
            json_response = login_response.json()
            _LOGGER.debug("Login JSON response %s", json_response)

            if json_response.get("hasError") or json_response.get("code") not in (0, None):
                _LOGGER.error(
                    "SEMS login returned error: %s",
                    json_response.get("msg"),
                )
                return None

            token_dict = json_response["data"]
            token_dict["api"] = json_response.get("api")
            _LOGGER.debug("SEMS - API Token received: %s", token_dict)
            return token_dict
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to fetch login token from SEMS API. %s", exc)
            return None

    def _ensure_token(self, renew: bool = False) -> bool:
        """Ensure we have a valid token in self._token."""
        if self._token is None or renew:
            _LOGGER.debug(
                "SEMS v%s - fetching new token (token_is_none=%s, renew=%s)",
                API_VERSION,
                self._token is None,
                renew,
            )
            token = self._fetch_login_token()
            if token is None:
                self._token = None
                return False
            self._token = token
        return True

    def _build_headers(self) -> dict:
        """Build request headers with current token."""
        if not self._ensure_token():
            raise OutOfRetries("Could not obtain SEMS token")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "token": json.dumps(self._token),
        }

    def _build_web_headers(self) -> dict:
        """Build headers for SEMS Plus EU gateway (requires x-signature).

        Algorithm (from semsplus.goodwe.com JS bundle):
          x-signature = base64(SHA256(timestamp_ms + '@' + uid + '@' + token) + '@' + timestamp_ms)
        """
        if not self._ensure_token():
            raise OutOfRetries("Could not obtain SEMS token")
        ts = str(int(time.time() * 1000))
        uid = self._token.get("uid", "") if self._token else ""
        tok = self._token.get("token", "") if self._token else ""
        digest = hashlib.sha256(f"{ts}@{uid}@{tok}".encode()).hexdigest()
        x_signature = base64.b64encode(f"{digest}@{ts}".encode()).decode()
        # EU gateway requires client=semsPlusWeb; our semsportal.com login returns
        # semsPlusAndroid, so we override it here. client is NOT part of x-signature.
        web_token = {**self._token, "client": "semsPlusWeb"}
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "token": json.dumps(web_token),
            "client": "semsPlusWeb",
            "neutral": "0",
            "currentlang": "en",
            "x-signature": x_signature,
        }

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def test_authentication(self) -> bool:
        """Test if we can authenticate with the host."""
        try:
            ok = self._ensure_token(renew=True)
            _LOGGER.debug(
                "SEMS v%s - test_authentication result: %s", API_VERSION, ok
            )
            return ok
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("SEMS Authentication exception: %s", exc)
            return False

    def _resolve_status_url(self) -> str:
        """Return the correct status URL based on toggle."""
        return _WallboxURL_V4 if _USE_V4_STATUS else _WallboxURL_V3

    def getData(self, wallbox_sn, renewToken: bool = False, maxTokenRetries: int = 1):
        """Get the latest data from the SEMS API."""
        _LOGGER.debug(
            "SEMS v%s - getData called for wallbox %s (renewToken=%s, retries=%s)",
            API_VERSION,
            wallbox_sn,
            renewToken,
            maxTokenRetries,
        )
        try:
            if maxTokenRetries < 0:
                _LOGGER.info(
                    "SEMS - Maximum token fetch tries reached, aborting for now"
                )
                raise OutOfRetries

            if not self._ensure_token(renew=renewToken):
                _LOGGER.error("SEMS - Could not ensure token before getData")
                return None

            headers = self._build_headers()
            wallbox_url = self._resolve_status_url()
            payload = json.dumps({"sn": wallbox_sn})

            try:
                _LOGGER.debug(
                    "SEMS v%s - Making Wallbox Status API Call, URL=%s, SN=%s",
                    API_VERSION,
                    wallbox_url,
                    wallbox_sn,
                )
                response = requests.post(
                    wallbox_url, headers=headers, data=payload, timeout=_RequestTimeout
                )
                response.raise_for_status()
                json_response = response.json()
            except requests.exceptions.HTTPError as http_err:
                # If v4 returns 404, fall back to v3
                if (
                    _USE_V4_STATUS
                    and wallbox_url.endswith("GetEvChargerMoreView")
                    and http_err.response is not None
                    and http_err.response.status_code == 404
                ):
                    _LOGGER.warning(
                        "SEMS v%s - v4 endpoint 404, falling back to v3 for SN=%s",
                        API_VERSION,
                        wallbox_sn,
                    )
                    v3_response = requests.post(
                        _WallboxURL_V3,
                        headers=headers,
                        data=payload,
                        timeout=_RequestTimeout,
                    )
                    v3_response.raise_for_status()
                    json_response = v3_response.json()
                else:
                    raise

            data = json_response.get("data")
            msg = str(json_response.get("msg", ""))
            _LOGGER.debug(
                "SEMS v%s - getData raw response msg=%s, data_is_none=%s",
                API_VERSION,
                msg,
                data is None,
            )

            # Handle authorization expiry → retry once with fresh token
            if data is None and "authorization has expired" in msg.lower():
                _LOGGER.debug(
                    "SEMS - Authorization expired (%s), retrying with fresh token, remaining retries: %s",
                    msg,
                    maxTokenRetries,
                )
                self._token = None
                return self.getData(
                    wallbox_sn, renewToken=True, maxTokenRetries=maxTokenRetries - 1
                )

            if data is None:
                _LOGGER.error(
                    "Unable to fetch data from SEMS, message: %s", msg
                )
                return None

            return data

        except OutOfRetries:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to fetch data from SEMS. %s", exc)
            return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def change_status(
        self,
        inverterSn,
        status,
        renewToken: bool = False,
        maxTokenRetries: int = 1,
    ):
        """Start or stop charging."""
        _LOGGER.debug(
            "SEMS v%s - change_status(%s, %s, renewToken=%s, retries=%s)",
            API_VERSION,
            inverterSn,
            status,
            renewToken,
            maxTokenRetries,
        )
        try:
            if maxTokenRetries < 0:
                _LOGGER.info(
                    "SEMS - Maximum token fetch tries reached for change_status"
                )
                raise OutOfRetries

            if not self._ensure_token(renew=renewToken):
                _LOGGER.error("SEMS - Could not ensure token before change_status")
                return

            headers = self._build_headers()
            _LOGGER.debug(
                "Sending power control command (%s) for wallbox sn: %s status: %s",
                _PowerControlURL,
                inverterSn,
                status,
            )

            data = {"sn": inverterSn, "status": str(status)}
            response = requests.post(
                _PowerControlURL, headers=headers, json=data, timeout=_RequestTimeout
            )

            try:
                resp_json = response.json()
            except Exception:  # noqa: BLE001
                resp_json = None

            if response.status_code != 200 or (
                isinstance(resp_json, dict)
                and resp_json.get("data") is None
                and "authorization has expired"
                in str(resp_json.get("msg", "")).lower()
            ):
                if (
                    isinstance(resp_json, dict)
                    and "authorization has expired"
                    in str(resp_json.get("msg", "")).lower()
                    and maxTokenRetries > 0
                ):
                    _LOGGER.debug(
                        "SEMS - change_status authorization expired, retrying once with new token"
                    )
                    self._token = None
                    return self.change_status(
                        inverterSn,
                        status,
                        renewToken=True,
                        maxTokenRetries=maxTokenRetries - 1,
                    )

                _LOGGER.warning(
                    "Power control command not successful (HTTP %s), response: %s",
                    response.status_code,
                    response.text,
                )
                return

            return
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to execute Power control command. %s", exc)

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
        """Auto-detect plantId for single-plant accounts via SEMS PowerStation API.

        Returns the plantId string or None if it can't be determined.
        Only queries once; result must be cached by the caller.
        """
        if not self._ensure_token():
            return None
        api_base = (self._token or {}).get("api", "https://www.semsportal.com")
        url = f"{api_base}{_GetPowerStationListURLPart}"
        try:
            headers = self._build_headers()
            resp = requests.post(url, headers=headers, json={}, timeout=_RequestTimeout)
            resp.raise_for_status()
            resp_json = resp.json()
            stations = resp_json.get("data")
            _LOGGER.debug("SEMS GetPowerStationList response: %s", resp_json)
            if isinstance(stations, list) and len(stations) == 1:
                st = stations[0]
                plant_id = (
                    st.get("id")
                    or st.get("stationId")
                    or st.get("powerStationId")
                    or st.get("stationIdHashCode")
                )
                if plant_id:
                    return str(plant_id)
            elif isinstance(stations, list) and len(stations) > 1:
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

    def set_charge_power_gen2(
        self,
        wallbox_sn: str,
        power: float,
        mode: int = 0,
    ) -> bool:
        """Set charge mode + power limit via the SEMS Plus EU gateway (Gen2 / HCA series).

        Strategy:
          1. Try set-mode (mode + chargePowerSetted) — confirmed working for Gen1.
          2. If set-mode returns a non-success code, also try set-config
             (ratedMaxiChargePower) as fallback — expected to work for Gen2 (HCA series).
        Both attempts are logged in full so we can diagnose which endpoint works per device.

        Known codes: 00000=success, R000013=power<4.2kW, R0305=remote_control_fail.
        Returns True if either attempt succeeds.
        """
        _LOGGER.debug(
            "SEMS v%s - set_charge_power_gen2(sn=%s, mode=%s, power=%s)",
            API_VERSION,
            wallbox_sn,
            mode,
            power,
        )

        if not self._ensure_token():
            _LOGGER.warning("SEMS gen2: no token, skipping eu-gateway call")
            return False

        plant_id = self._ensure_plant_id()
        headers = self._build_web_headers()

        def _base_payload() -> dict:
            p: dict = {"sn": wallbox_sn}
            if plant_id:
                p["plantId"] = plant_id
            if self._product_model:
                p["productModel"] = self._product_model
            return p

        def _post(label: str, url: str, extra: dict) -> tuple[bool, str]:
            """POST to url with base + extra payload. Returns (success, code)."""
            payload = {**_base_payload(), **extra}
            try:
                _LOGGER.debug("SEMS gen2 %s: POST %s payload=%s", label, url, payload)
                resp = requests.post(url, headers=headers, json=payload, timeout=_RequestTimeout)
                _LOGGER.debug("SEMS gen2 %s: HTTP %s body=%s", label, resp.status_code, resp.text)
                try:
                    rj = resp.json()
                except Exception:  # noqa: BLE001
                    rj = {}
                code = str(rj.get("code") or rj.get("Code") or "")
                success = code in ("00000", "0") or rj.get("data") is True
                return success, code
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("SEMS gen2 %s call failed: %s", label, exc)
                return False, ""

        # --- Attempt 1: set-mode (mode + chargePowerSetted) ---
        ok, code = _post(
            "set-mode",
            _EuGatewaySetModeURL,
            {"mode": mode, "chargePowerSetted": float(power)},
        )
        if ok:
            _LOGGER.info(
                "SEMS gen2 set-mode succeeded (sn=%s, mode=%s, power=%s)", wallbox_sn, mode, power
            )
            return True
        if code == "R000013":
            _LOGGER.warning(
                "SEMS gen2 set-mode rejected: power %.1f kW is below the 4.2 kW minimum (sn=%s)",
                power,
                wallbox_sn,
            )
            # Power validation failed — no point trying set-config with the same power value
            return False
        if code == "R0305":
            _LOGGER.warning(
                "SEMS gen2 set-mode: remote_control_fail (sn=%s) — "
                "wallbox may be offline; will still try set-config fallback",
                wallbox_sn,
            )
        elif code:
            _LOGGER.warning(
                "SEMS gen2 set-mode non-success code=%s (sn=%s) — trying set-config fallback",
                code,
                wallbox_sn,
            )

        # --- Attempt 2: set-config (ratedMaxiChargePower) — Gen2 / HCA series fallback ---
        ok, code = _post(
            "set-config",
            _EuGatewaySetConfigURL,
            {"ratedMaxiChargePower": int(power)},
        )
        if ok:
            _LOGGER.info(
                "SEMS gen2 set-config succeeded (sn=%s, power=%s)", wallbox_sn, power
            )
            return True
        _LOGGER.warning(
            "SEMS gen2 both set-mode and set-config failed for sn=%s power=%s "
            "(last code=%s) — check plant_id/product_model in integration options",
            wallbox_sn,
            power,
            code,
        )
        return False

    def set_charge_mode(
        self,
        wallboxSn,
        mode,
        chargePower=None,
        renewToken: bool = False,
        maxTokenRetries: int = 1,
    ):
        """Set charge mode and optionally power.

        For Gen1 (semsportal.com SetChargeMode) this handles everything.
        For Gen2 (HCA series) the power limit requires an additional call to the
        EU gateway set-config endpoint — both are attempted so both device generations work.
        """
        _LOGGER.debug(
            "SEMS v%s - set_charge_mode(sn=%s, mode=%s, power=%s, renewToken=%s, retries=%s)",
            API_VERSION,
            wallboxSn,
            mode,
            chargePower,
            renewToken,
            maxTokenRetries,
        )
        try:
            if maxTokenRetries < 0:
                _LOGGER.info(
                    "SEMS - Maximum token fetch tries reached for set_charge_mode"
                )
                raise OutOfRetries

            if not self._ensure_token(renew=renewToken):
                _LOGGER.error("SEMS - Could not ensure token before set_charge_mode")
                return False

            headers = self._build_headers()
            _LOGGER.debug(
                "Sending SetChargeMode command (%s) for wallbox SN: %s mode: %s chargepower: %s",
                _SetChargeModeURL,
                wallboxSn,
                mode,
                chargePower,
            )

            if chargePower is not None:
                data = {"sn": wallboxSn, "type": mode, "charge_power": chargePower}
            else:
                data = {"sn": wallboxSn, "type": mode}

            response = requests.post(
                _SetChargeModeURL, headers=headers, json=data, timeout=_RequestTimeout
            )

            try:
                resp_json = response.json()
            except Exception:  # noqa: BLE001
                resp_json = None

            if response.status_code != 200 or (
                isinstance(resp_json, dict)
                and resp_json.get("data") is None
                and "authorization has expired"
                in str(resp_json.get("msg", "")).lower()
            ):
                if (
                    isinstance(resp_json, dict)
                    and "authorization has expired"
                    in str(resp_json.get("msg", "")).lower()
                    and maxTokenRetries > 0
                ):
                    _LOGGER.debug(
                        "SEMS - set_charge_mode authorization expired, retrying once with new token"
                    )
                    self._token = None
                    return self.set_charge_mode(
                        wallboxSn,
                        mode,
                        chargePower=chargePower,
                        renewToken=True,
                        maxTokenRetries=maxTokenRetries - 1,
                    )

                _LOGGER.warning(
                    "SetChargeMode command not successful (HTTP %s), response: %s",
                    response.status_code,
                    response.text,
                )
                return False

            # For Gen2 (HCA series) chargers the SetChargeMode endpoint ignores
            # charge_power, so also call the EU gateway set-mode endpoint.
            # Use plant_id from options if set; otherwise try auto-detection once
            # (result is cached).  If neither yields a plant_id, skip the gen2 call
            # so Gen1 users who have no SEMS Plus plant are unaffected.
            if chargePower is not None and self._ensure_plant_id():
                _LOGGER.debug(
                    "SEMS - plant_id=%s, also calling set_charge_power_gen2 "
                    "for sn=%s mode=%s power=%s",
                    self._plant_id,
                    wallboxSn,
                    mode,
                    chargePower,
                )
                self.set_charge_power_gen2(wallboxSn, chargePower, mode=mode)

            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to execute SetChargeMode command. %s", exc)
            return False


class OutOfRetries(exceptions.HomeAssistantError):
    """Error to indicate too many error attempts."""
