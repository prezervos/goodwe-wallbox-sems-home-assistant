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
        assert api._web_token is not None

    def test_failure_returns_false(self):
        api = _make_api()
        with patch("requests.post", side_effect=Exception("timeout")):
            assert api.test_authentication() is False

    def test_error_code_returns_false(self):
        api = _make_api()
        with patch("requests.post", return_value=_login_response(None, code=100)):
            assert api.test_authentication() is False


# ===========================================================================
# test _fetch_web_token
# ===========================================================================

class TestFetchWebToken:
    def test_returns_token_dict(self):
        api = _make_api()
        token = {"uid": "u1", "token": "t1", "timestamp": 999}
        with patch("requests.post", return_value=_login_response(token)):
            result = api._fetch_web_token()
        assert result is not None
        assert result["token"] == "t1"

    def test_returns_none_on_network_error(self):
        api = _make_api()
        with patch("requests.post", side_effect=OSError("network down")):
            assert api._fetch_web_token() is None

    def test_returns_none_when_has_error(self):
        api = _make_api()
        with patch("requests.post", return_value=_login_response(None, has_error=True)):
            assert api._fetch_web_token() is None


# ===========================================================================
# test _ensure_web_token
# ===========================================================================

class TestEnsureWebToken:
    def test_fetches_token_when_none(self):
        api = _make_api()
        token = {"uid": "u", "token": "t", "timestamp": 1}
        with patch("requests.post", return_value=_login_response(token)):
            assert api._ensure_web_token() is True
        assert api._web_token is not None

    def test_skips_fetch_when_token_already_set(self):
        api = _make_api()
        api._web_token = {"uid": "existing"}
        with patch("requests.post") as mock_post:
            assert api._ensure_web_token() is True
            mock_post.assert_not_called()

    def test_renew_forces_refetch(self):
        api = _make_api()
        api._web_token = {"uid": "old"}
        new_token = {"uid": "new", "token": "fresh", "timestamp": 2}
        with patch("requests.post", return_value=_login_response(new_token)):
            assert api._ensure_web_token(renew=True) is True
        assert api._web_token["uid"] == "new"

    def test_returns_false_when_login_fails(self):
        api = _make_api()
        with patch("requests.post", return_value=_login_response(None, code=100)):
            assert api._ensure_web_token() is False
        assert api._web_token is None


# ===========================================================================
# test change_status_gen2
# ===========================================================================

class TestChangeStatusGen2:
    def _setup_api(self, plant_id="plant-001", product_model="GW11K-HCA"):
        api = _make_api()
        api._plant_id = plant_id
        api._web_token = {"uid": "u", "token": "tok", "timestamp": 1}
        api._product_model = product_model
        return api

    def _gen2_response(self, code="00000", data=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"code": code, "data": data, "msg": ""}
        resp.text = '{"code": "' + code + '"}'
        return resp

    def test_start_charge_success(self):
        api = self._setup_api()
        resp = self._gen2_response(code="00000")
        with patch("requests.post", return_value=resp) as mock_post:
            result = api.change_status_gen2("SN001", "start")
        assert result is True
        call_url = mock_post.call_args[0][0]
        assert "startCharge" in call_url
        payload = mock_post.call_args[1]["json"]
        assert payload["sn"] == "SN001"
        assert payload["plantId"] == "plant-001"

    def test_stop_charge_success(self):
        api = self._setup_api()
        resp = self._gen2_response(code="00000")
        with patch("requests.post", return_value=resp) as mock_post:
            result = api.change_status_gen2("SN001", "stop")
        assert result is True
        call_url = mock_post.call_args[0][0]
        assert "stopCharge" in call_url

    def test_no_plant_id_returns_false(self):
        api = _make_api()
        api._plant_id = None
        with patch.object(api, "_try_fetch_plant_id", return_value=None):
            result = api.change_status_gen2("SN001", "start")
        assert result is False

    def test_non_success_code_returns_false(self):
        api = self._setup_api()
        resp = self._gen2_response(code="E0001")
        with patch("requests.post", return_value=resp):
            result = api.change_status_gen2("SN001", "start")
        assert result is False

    def test_network_error_returns_false(self):
        api = self._setup_api()
        with patch("requests.post", side_effect=OSError("connection refused")):
            result = api.change_status_gen2("SN001", "start")
        assert result is False

