import json
import logging

import requests
from homeassistant import exceptions

_LOGGER = logging.getLogger(__name__)

API_VERSION = "0.4.2"

_LoginURL = "https://www.semsportal.com/api/v3/Common/CrossLogin"

# v3/v4 endpoints for reading wallbox status
_WallboxURL_V3 = "https://www.semsportal.com/api/v3/EvCharger/GetCurrentChargeinfo"
_WallboxURL_V4 = "https://www.semsportal.com/api/v4/EvCharger/GetEvChargerMoreView"

# Toggle: set to True to prefer v4 endpoint (with automatic fallback to v3)
_USE_V4_STATUS = False

_SetChargeModeURL = "https://www.semsportal.com/api/v3/EvCharger/SetChargeMode"
_PowerControlURL = "https://www.semsportal.com/api/v3/EvCharger/Charging"

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

    def set_charge_mode(
        self,
        wallboxSn,
        mode,
        chargePower=None,
        renewToken: bool = False,
        maxTokenRetries: int = 1,
    ):
        """Set charge mode and optionally power."""
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

            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Unable to execute SetChargeMode command. %s", exc)
            return False


class OutOfRetries(exceptions.HomeAssistantError):
    """Error to indicate too many error attempts."""
