"""Unit tests for sems_api.SemsApi."""

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# We mock the homeassistant module before importing SemsApi
import sys
from types import ModuleType

# ---------------------------------------------------------------------------
# Minimal HA stub so sems_api.py can be imported without a real HA install
# ---------------------------------------------------------------------------
ha_stub = ModuleType("homeassistant")
ha_exceptions = ModuleType("homeassistant.exceptions")


class _HomeAssistantError(Exception):
    pass


ha_exceptions.HomeAssistantError = _HomeAssistantError
ha_stub.exceptions = ha_exceptions
sys.modules.setdefault("homeassistant", ha_stub)
sys.modules.setdefault("homeassistant.exceptions", ha_exceptions)

import importlib
import types

# Point the package to our local files
pkg = types.ModuleType("custom_components")
pkg_wallbox = types.ModuleType("custom_components.sems_wallbox")
sys.modules.setdefault("custom_components", pkg)
sys.modules.setdefault("custom_components.sems_wallbox", pkg_wallbox)

# Now import the module under test
import importlib.util, os

_HERE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "custom_components", "sems-wallbox")
spec = importlib.util.spec_from_file_location("sems_api", os.path.join(_HERE, "sems_api.py"))
sems_api_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sems_api_module)

SemsApi = sems_api_module.SemsApi
OutOfRetries = sems_api_module.OutOfRetries


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_api():
    hass = MagicMock()
    return SemsApi(hass, "user@example.com", "password123")


def _login_response(token_data: dict | None, code=0, has_error=False):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "code": code,
        "hasError": has_error,
        "data": token_data,
        "api": "https://www.semsportal.com/api/",
        "msg": "",
    }
    return resp


def _data_response(data, msg=""):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": data, "msg": msg}
    resp.status_code = 200
    return resp


# ===========================================================================
# test_authentication
# ===========================================================================

class TestAuthentication:
    def test_success(self):
        api = _make_api()
        token = {"uid": "abc", "token": "tok123", "timestamp": 123}
        with patch("requests.post", return_value=_login_response(token)):
            assert api.test_authentication() is True
        assert api._token is not None

    def test_failure_returns_false(self):
        api = _make_api()
        with patch("requests.post", side_effect=Exception("timeout")):
            assert api.test_authentication() is False

    def test_error_code_returns_false(self):
        api = _make_api()
        with patch("requests.post", return_value=_login_response(None, code=100)):
            assert api.test_authentication() is False


# ===========================================================================
# test _fetch_login_token
# ===========================================================================

class TestFetchLoginToken:
    def test_returns_token_dict(self):
        api = _make_api()
        token = {"uid": "u1", "token": "t1", "timestamp": 999}
        with patch("requests.post", return_value=_login_response(token)):
            result = api._fetch_login_token()
        assert result is not None
        assert result["token"] == "t1"
        assert result["api"] == "https://www.semsportal.com/api/"

    def test_returns_none_on_network_error(self):
        api = _make_api()
        with patch("requests.post", side_effect=OSError("network down")):
            assert api._fetch_login_token() is None

    def test_returns_none_when_has_error(self):
        api = _make_api()
        with patch("requests.post", return_value=_login_response(None, has_error=True)):
            assert api._fetch_login_token() is None


# ===========================================================================
# test _ensure_token
# ===========================================================================

class TestEnsureToken:
    def test_fetches_token_when_none(self):
        api = _make_api()
        token = {"uid": "u", "token": "t", "timestamp": 1}
        with patch("requests.post", return_value=_login_response(token)):
            assert api._ensure_token() is True
        assert api._token is not None

    def test_skips_fetch_when_token_already_set(self):
        api = _make_api()
        api._token = {"uid": "existing"}
        with patch("requests.post") as mock_post:
            assert api._ensure_token() is True
            mock_post.assert_not_called()

    def test_renew_forces_refetch(self):
        api = _make_api()
        api._token = {"uid": "old"}
        new_token = {"uid": "new", "token": "fresh", "timestamp": 2}
        with patch("requests.post", return_value=_login_response(new_token)):
            assert api._ensure_token(renew=True) is True
        assert api._token["uid"] == "new"

    def test_returns_false_when_login_fails(self):
        api = _make_api()
        with patch("requests.post", return_value=_login_response(None)):
            assert api._ensure_token() is False
        assert api._token is None


# ===========================================================================
# test getData
# ===========================================================================

class TestGetData:
    def _setup_api_with_token(self):
        api = _make_api()
        api._token = {"uid": "u", "token": "t", "timestamp": 1, "api": "https://www.semsportal.com/api/"}
        return api

    def test_returns_data_dict(self):
        api = self._setup_api_with_token()
        payload = {"sn": "SN001", "status": "EVDetail_Status_Title_Charging", "power": 7.4}
        with patch("requests.post", return_value=_data_response(payload)):
            result = api.getData("SN001")
        assert result == payload

    def test_returns_none_on_network_error(self):
        api = self._setup_api_with_token()
        with patch("requests.post", side_effect=OSError("connection refused")):
            assert api.getData("SN001") is None

    def test_retries_on_expired_auth(self):
        api = self._setup_api_with_token()
        expired_resp = _data_response(None, msg="authorization has expired")
        good_payload = {"sn": "SN001", "power": 0.0}
        good_resp = _data_response(good_payload)
        new_token = {"uid": "u", "token": "new", "timestamp": 99, "api": "x"}

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return expired_resp  # status call -> expired
            if call_count == 2:
                return _login_response(new_token)  # token renewal
            return good_resp  # retry status call

        with patch("requests.post", side_effect=side_effect):
            result = api.getData("SN001", maxTokenRetries=1)
        assert result == good_payload

    def test_raises_out_of_retries_when_max_reached(self):
        api = self._setup_api_with_token()
        with pytest.raises(OutOfRetries):
            api.getData("SN001", maxTokenRetries=-1)


# ===========================================================================
# test change_status
# ===========================================================================

class TestChangeStatus:
    def _setup_api_with_token(self):
        api = _make_api()
        api._token = {"uid": "u", "token": "t", "timestamp": 1, "api": "x"}
        return api

    def test_sends_correct_payload(self):
        api = self._setup_api_with_token()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": "ok", "msg": ""}

        with patch("requests.post", return_value=resp) as mock_post:
            api.change_status("SN001", 1)

        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"sn": "SN001", "status": "1"}

    def test_logs_warning_on_non_200(self):
        api = self._setup_api_with_token()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 500
        resp.text = "Internal Server Error"
        resp.json.return_value = {"data": None, "msg": "error"}

        with patch("requests.post", return_value=resp):
            api.change_status("SN001", 2)  # Should not raise, just log warning


# ===========================================================================
# test set_charge_mode
# ===========================================================================

class TestSetChargeMode:
    def _setup_api_with_token(self):
        api = _make_api()
        api._token = {"uid": "u", "token": "t", "timestamp": 1, "api": "x"}
        return api

    def test_sends_mode_without_power(self):
        api = self._setup_api_with_token()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": "ok", "msg": ""}

        with patch("requests.post", return_value=resp) as mock_post:
            api.set_charge_mode("SN001", 1)

        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"sn": "SN001", "type": 1}

    def test_sends_mode_with_power(self):
        api = self._setup_api_with_token()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": "ok", "msg": ""}

        with patch("requests.post", return_value=resp) as mock_post:
            api.set_charge_mode("SN001", 0, chargePower=7.4)

        # First call is the gen1 SetChargeMode; second call (if any) is the gen2 gateway
        first_call = mock_post.call_args_list[0]
        assert first_call[1]["json"] == {"sn": "SN001", "type": 0, "charge_power": 7.4}
