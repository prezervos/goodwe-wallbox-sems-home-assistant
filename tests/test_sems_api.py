"""Unit tests for sems_api.SemsApi."""

import json
from unittest.mock import MagicMock, patch

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

_HERE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "custom_components", "sems_wallbox")
api_package = types.ModuleType("sems_api_tests_package")
api_package.__path__ = [_HERE]
sys.modules[api_package.__name__] = api_package
sems_api_module = importlib.import_module(api_package.__name__ + ".sems_api")

SemsApi = sems_api_module.SemsApi
OutOfRetries = sems_api_module.OutOfRetries


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_api():
    hass = MagicMock()
    return SemsApi(hass, "user@example.com", "password123")


def _login_response(token_data: dict | None, code=0, has_error=False, *, endpoint="original", region="eu"):
    resp = MagicMock(status_code=200, headers={})
    resp.raise_for_status = MagicMock()
    gateway = f"https://{region}-gateway.semsportal.com/sems/"
    if endpoint == "original" and isinstance(token_data, dict):
        token_data = {**token_data, "api": gateway}
    resp.json.return_value = {
        "code": code,
        "hasError": has_error,
        "data": token_data,
        "msg": "",
    }
    if endpoint == "common":
        resp.json.return_value["api"] = gateway
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

    def test_error_code_returns_false(self, caplog):
        api = _make_api()
        response = _login_response({"token": "must-not-be-logged"}, code=100)
        response.json.return_value.update(
            description="Request rejected", translationCode="request_rejected"
        )
        with patch("requests.post", return_value=response):
            assert api.test_authentication() is False
        assert "description=Request rejected" in caplog.text
        assert "translation=request_rejected" in caplog.text
        assert "must-not-be-logged" not in caplog.text


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

    @pytest.mark.parametrize("action", ["start", "stop"])
    @pytest.mark.parametrize("code", ["E0001", "A0201"])
    def test_non_success_code_returns_false(self, action, code):
        api = self._setup_api()
        resp = self._gen2_response(code=code)
        with patch("requests.post", return_value=resp) as post:
            result = api.change_status_gen2("SN001", action)
        assert result is False
        post.assert_called_once()

    @pytest.mark.parametrize("final_code, expected", [("00000", True), ("C0602", False)])
    def test_explicit_expired_session_is_renewed_once(self, final_code, expected):
        api = self._setup_api()
        with patch("requests.post", side_effect=[
            self._gen2_response(code="C0602"),
            self._gen2_response(code=final_code),
        ]) as post, patch.object(api, "_fetch_web_token", return_value={
            "uid": "u", "token": "renewed", "timestamp": 2,
        }) as renew:
            assert api.change_status_gen2("SN001", "stop") is expected
        assert post.call_count == 2
        renew.assert_called_once()

    @pytest.mark.parametrize("action", ["start", "stop"])
    @pytest.mark.parametrize("failure", ["connection", "timeout", 429, 500, 502, 503, 504])
    def test_transport_failure_does_not_replay_command(self, action, failure):
        import requests

        api = self._setup_api()
        if failure == "connection":
            request = patch("requests.post", side_effect=requests.ConnectionError("connection lost"))
        elif failure == "timeout":
            request = patch("requests.post", side_effect=requests.Timeout("response lost"))
        else:
            response = requests.Response()
            response.status_code = failure
            response.headers["Retry-After"] = "600"
            response._content = b'{"message": "Please wait"}'
            request = patch("requests.post", return_value=response)
        with request as post:
            if failure in (429, 503):
                with pytest.raises(sems_api_module.CloudRateLimitedError):
                    api.change_status_gen2("SN001", action)
            else:
                assert api.change_status_gen2("SN001", action) is False
        post.assert_called_once()
        assert api.login_attempts == 0
        api.close()



@pytest.mark.parametrize("operation", ["mode", "config", "start", "stop"])
@pytest.mark.parametrize("payload,expected", [
    ({"code": "A0201", "data": True}, False),
    ({"code": 0, "data": None}, True),
    ({"data": True}, True),
])
def test_command_status_code_takes_precedence_over_boolean(operation, payload, expected):
    api = _make_api()
    api._plant_id = "plant-001"
    api._web_token = {"uid": "u", "token": "tok", "timestamp": 1}
    reply = MagicMock()
    reply.json.return_value = payload
    reply.status_code = 200
    reply.text = json.dumps(payload)
    with patch("requests.post", return_value=reply) as post:
        if operation == "mode":
            result = api.set_charge_mode_gen2("SN001", 0, 4.2)
        elif operation == "config":
            result = api.set_config_gen2("SN001", chargedNow=1)
        else:
            result = api.change_status_gen2("SN001", operation)
    assert result is expected
    post.assert_called_once()


def test_token_renewal_preserves_all_mode_parameters():
    api = _make_api()
    api._ensure_plant_id = MagicMock(return_value="PLANT")
    api._ensure_web_token = MagicMock(return_value=True)
    api._build_web_headers = MagicMock(return_value={})
    responses = []
    for code in ("C0602", "00000"):
        response = MagicMock(status_code=200, text="response")
        response.json.return_value = {"code": code, "data": True}
        responses.append(response)
    with patch.object(sems_api_module.requests, "post", side_effect=responses) as post:
        assert api.set_charge_mode_gen2(
            "TEST", 2, chargePower=4.2, ensure_minimum_charging_power=True,
            max_energy=20, min_energy=3, soc_target=80, finish_time="2",
        )
    assert post.call_count == 2
    assert post.call_args_list[0].kwargs["json"] == post.call_args_list[1].kwargs["json"]
    assert post.call_args_list[1].kwargs["json"]["maxEnergy"] == 20


@pytest.mark.parametrize("debug", [False, True])
def test_detail_parsed_once_and_full_debug_formatting_is_optional(debug):
    api = _make_api()
    api._ensure_plant_id = MagicMock(return_value="PLANT")
    api._ensure_web_token = MagicMock(return_value=True)
    api._build_web_headers = MagicMock(return_value={})
    response = MagicMock(status_code=200)
    response.json.return_value = {"code": "00000", "data": {"sn": "TEST", "chargeMode": 0}}
    with (patch.object(sems_api_module.requests, "post", return_value=response),
          patch.object(sems_api_module._LOGGER, "isEnabledFor", return_value=debug),
          patch.object(sems_api_module.json, "dumps", wraps=json.dumps) as dumps):
        assert api.get_data_gen2("TEST")["sn"] == "TEST"
    response.json.assert_called_once_with()
    assert dumps.call_count == int(debug)
    if debug:
        assert dumps.call_args.args[0] == response.json.return_value


def test_api_close_is_idempotent_and_blocks_late_requests():
    api = _make_api()
    api._observation_reader = MagicMock()
    api.close()
    api.close()
    api._observation_reader.close.assert_called_once_with()
    with pytest.raises(ConnectionError, match="closed"):
        api.get_data_gen2("TEST")


@pytest.mark.parametrize("status", [401, 403])
def test_runtime_login_rejection_keeps_authentication_type(status):
    api = _make_api()
    response = _login_response(None)
    response.status_code = status
    with patch("requests.post", return_value=response):
        with pytest.raises(sems_api_module.CloudAuthenticationError):
            api.get_data_gen2("TEST")
    api.close()


def test_transient_login_failure_does_not_request_reauthentication():
    import requests
    api = _make_api()
    with patch("requests.post", side_effect=requests.Timeout("temporary outage")):
        assert api.get_data_gen2("TEST") is None
    api.close()


@pytest.mark.parametrize("renew", [False, True])
def test_failed_login_backoff_is_shared_and_renew_cannot_bypass(renew):
    api = _make_api()
    with patch.object(sems_api_module.time, "monotonic", return_value=100), patch(
        "requests.post", return_value=_login_response(None, code="100004")
    ) as post:
        assert not api._ensure_web_token()
        assert not api._ensure_web_token(renew=renew)
        assert api.get_data_gen2("TEST") is None
        with pytest.raises(ConnectionError):
            api.fetch_mqtt_settings()
        assert post.call_count == 2
        assert api._web_login_retry_at == 130
        assert api.login_attempts == 1
        assert api.successful_logins == 0
        assert api.last_login_at is None
    with patch.object(sems_api_module.time, "monotonic", return_value=130), patch(
        "requests.post", return_value=_login_response(None, code="100004")
    ) as post:
        assert not api._ensure_web_token()
        assert api._web_login_retry_at == 190
        assert api.login_attempts == 2
        assert post.call_count == 2
    with patch.object(sems_api_module.time, "monotonic", return_value=190), patch(
        "requests.post", return_value=_login_response({"token": "fresh"})
    ):
        assert api._ensure_web_token()
        assert api._web_login_retry_at == 0
        assert api._web_login_delay == 30
        assert api.login_attempts == 3
        assert api.successful_logins == 1
        assert api.last_login_at == 190
        assert api._ensure_web_token()
        assert api.login_attempts == 3  # Cached access is not another login.


def test_explicit_auth_rejection_is_not_retried_until_credentials_replaced():
    api = _make_api()
    response = _login_response(None)
    response.status_code = 401
    with patch("requests.post", return_value=response) as post:
        for _ in range(2):
            with pytest.raises(sems_api_module.CloudAuthenticationError):
                api._ensure_web_token()
        post.assert_called_once()
    api.replace_credentials("changed", "changed")
    with patch("requests.post", return_value=_login_response({"token": "fresh"})):
        assert api._ensure_web_token()


@pytest.mark.parametrize("header,delay", [("600",600),("invalid",30),("-1",30),("nan",30),
    ("Thu, 01 Jan 1970 00:30:00 GMT",800)])
def test_login_rate_limit_honors_valid_retry_after(header, delay):
    import requests
    api = _make_api()
    response = _login_response(None)
    response.status_code = 429
    response.headers = {"Retry-After": header}
    response.raise_for_status.side_effect = requests.HTTPError("rate limited")
    with patch.object(sems_api_module.time, "monotonic", return_value=100), patch.object(
        sems_api_module.time, "time", return_value=1000
    ), patch("requests.post", return_value=response):
        assert not api._ensure_web_token()
    assert api._web_login_retry_at == 100 + delay


@pytest.mark.parametrize("region", ["eu", "au", "us", "hk"])
def test_default_original_login_uses_web_client_and_returned_regional_gateway(region):
    api = _make_api()
    response = _login_response({"uid":"user","token":"secret-token","client":"semsPlusWeb"})
    response.json.return_value["data"]["api"] = f"https://{region}-gateway.semsportal.com/sems/"
    with patch("requests.post", return_value=response) as post:
        assert api._ensure_web_token()
    args = post.call_args
    assert args.args[0] == sems_api_module._LOGIN_URLS["original"]
    post.assert_called_once()
    assert json.loads(args.kwargs["headers"]["token"])["client"] == "semsPlusWeb"
    assert args.kwargs["headers"]["User-Agent"] == sems_api_module.SEMS_USER_AGENT
    import base64
    import hashlib
    assert args.kwargs["json"] == {
        "account": "user@example.com",
        "pwd": base64.b64encode(hashlib.md5(b"password123").hexdigest().encode()).decode(),
        "agreement": 1, "isLocal": False, "isChinese": False,
    }
    assert "x-signature" in args.kwargs["headers"]
    assert api._eu_url("sems-remote/api/ev-charger/detail") == (
        f"https://{region}-gateway.semsportal.com/web/sems/sems-remote/api/ev-charger/detail"
    )


@pytest.mark.parametrize("gateway", [
    "https://eu-gateway.semsportal.com.attacker.example/sems/",
    "http://eu-gateway.semsportal.com/sems/",
    "https://eu-gateway.semsportal.com/unexpected/",
    "https://user:password@eu-gateway.semsportal.com/sems/",
    "https://eu-gateway.semsportal.com/sems/?token=unexpected",
])
def test_original_login_rejects_untrusted_or_incompatible_gateway(gateway):
    api = _make_api()
    response = _login_response({"token":"secret-token","client":"semsPlusWeb"})
    response.json.return_value["data"]["api"] = gateway
    with patch("requests.post", return_value=response):
        assert not api._ensure_web_token()
    assert api._web_token is None


def _endpoint_response(endpoint, region="eu"):
    return _login_response({"uid": "user", "token": "fallback-token",
                            "client": "semsPlusWeb"}, endpoint=endpoint, region=region)


def _fallback_response(region="eu"):
    return _endpoint_response("common", region)


@pytest.mark.parametrize("region", ["eu", "au", "us", "hk"])
@pytest.mark.parametrize("first", ["original", "common"])
def test_either_login_order_preserves_endpoint_format_and_region(region, first, monkeypatch):
    import base64
    import hashlib
    second = "common" if first == "original" else "original"
    monkeypatch.setattr(sems_api_module, "_LOGIN_ORDER", (first, second))
    api = _make_api()
    with patch("requests.post", side_effect=[
        _login_response(None, code="100004", endpoint=first), _endpoint_response(second, region)
    ]) as post:
        assert api._ensure_web_token()
        assert api._ensure_web_token()
    assert [call.args[0] for call in post.call_args_list] == [
        sems_api_module._LOGIN_URLS[first], sems_api_module._LOGIN_URLS[second]]
    for endpoint, call in zip((first, second), post.call_args_list):
        assert call.kwargs["headers"]["User-Agent"] == sems_api_module.SEMS_USER_AGENT
        assert json.loads(call.kwargs["headers"]["token"])["client"] == "semsPlusWeb"
        if endpoint == "original":
            assert call.kwargs["json"]["pwd"] == base64.b64encode(
                hashlib.md5(b"password123").hexdigest().encode()).decode()
            signature, stamp = base64.b64decode(call.kwargs["headers"]["x-signature"]).decode().split("@")
            assert signature == hashlib.sha256(f"{stamp}@@".encode()).hexdigest()
        else:
            assert call.kwargs["json"] == {"account": "user@example.com", "pwd": "password123"}
            assert "x-signature" not in call.kwargs["headers"]
    assert api._web_api_base == f"https://{region}-gateway.semsportal.com/web/sems"


@pytest.mark.parametrize("failure", ["http401", "http403", "http429", "retry_after",
    "business_auth", "business_rate", "unknown", "invalid_region"])
@pytest.mark.parametrize("first", ["original", "common"])
def test_login_fallback_cannot_bypass_rejection_or_server_delay(failure, first, monkeypatch):
    monkeypatch.setattr(sems_api_module, "_LOGIN_ORDER", (first, "common" if first == "original" else "original"))
    api = _make_api()
    response = _login_response(None, code="unclassified", endpoint=first)
    if failure.startswith("http"):
        response.status_code = int(failure[4:])
    elif failure == "retry_after":
        response.status_code = 503
        response.headers = {"Retry-After": "600"}
    elif failure.startswith("business"):
        response.json.return_value.update(code="100004", translationCode=(
            "account_password_error" if failure == "business_auth" else "too_many_requests"))
    elif failure == "invalid_region":
        response = _login_response({"token": "secret"}, endpoint=first)
        target = response.json.return_value["data"] if first == "original" else response.json.return_value
        target["api"] = "https://attacker.example/sems"
    with patch("requests.post", return_value=response) as post:
        if failure in ("http401", "http403"):
            with pytest.raises(sems_api_module.CloudAuthenticationError):
                api._ensure_web_token()
        else:
            assert not api._ensure_web_token()
        post.assert_called_once()


@pytest.mark.parametrize("failure", ["http503", "connection", "timeout", "malformed"])
def test_eligible_login_failure_uses_only_one_fallback(failure):
    import requests
    api = _make_api()
    first = _login_response(None)
    if failure == "http503": first.status_code = 503
    elif failure == "connection": first = requests.ConnectionError("offline")
    elif failure == "timeout": first = requests.Timeout("socket timeout")
    else: first.json.side_effect = ValueError("not JSON")
    with patch("requests.post", side_effect=[first, _fallback_response()]) as post:
        assert api._ensure_web_token()
    assert post.call_count == 2


@pytest.mark.parametrize("elapsed,attempts", [(29, 2), (31, 1)])
@pytest.mark.parametrize("missing_token", [False, True])
def test_login_fallback_shares_remaining_deadline(elapsed, attempts, missing_token):
    api = _make_api()
    clock = [100.0]
    def post(url, **kwargs):
        if url == sems_api_module._LOGIN_URLS["original"]:
            clock[0] += elapsed
            response = _login_response({} if missing_token else None)
            response.status_code = 200 if missing_token else 503
            return response
        assert 0 < kwargs["timeout"] <= 1
        return _fallback_response()
    with patch.object(sems_api_module.time, "monotonic", side_effect=lambda: clock[0]), patch(
        "requests.post", side_effect=post
    ) as request:
        assert api._ensure_web_token() is (attempts == 2)
        assert request.call_count == attempts


def test_concurrent_callers_share_one_fallback_session():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    api = _make_api()
    entered, release = threading.Event(), threading.Event()
    def post(url, **kwargs):
        if url == sems_api_module._LOGIN_URLS["original"]:
            entered.set();assert release.wait(2)
            return _login_response(None, code="100004")
        return _fallback_response()
    with patch("requests.post", side_effect=post) as request, ThreadPoolExecutor(2) as pool:
        first = pool.submit(api._ensure_web_token)
        assert entered.wait(2)
        second = pool.submit(api._ensure_web_token)
        release.set()
        assert first.result(timeout=2) and second.result(timeout=2)
        assert request.call_count == 2


def test_session_rejection_renews_via_fallback_without_extra_command_replay():
    api = _make_api();api._plant_id = "plant"
    api._web_token = {"uid": "user", "token": "old"}
    expired = _data_response(None);expired.json.return_value["code"] = "C0602"
    success = _data_response(True);success.json.return_value["code"] = "00000"
    with patch("requests.post", side_effect=[expired,
        _login_response(None, code="100004"), _fallback_response(), success]) as post:
        assert api.change_status_gen2("SN001", "stop")
    assert [c.args[0].rsplit("/", 1)[-1] for c in post.call_args_list] == [
        "stopCharge", "cross-login", "CrossLogin", "stopCharge"]


@pytest.mark.parametrize("gateway", [None, "http://eu-gateway.semsportal.com/web/sems",
    "https://eu-gateway.semsportal.com.attacker.example/web/sems"])
def test_fallback_rejects_missing_or_untrusted_region(gateway):
    api = _make_api();response = _fallback_response()
    response.json.return_value["api"] = gateway
    with patch("requests.post", side_effect=[_login_response(None, code="100004"), response]) as post:
        assert not api._ensure_web_token()
    assert post.call_count == 2


def test_cancelled_operation_never_attempts_fallback():
    api = _make_api()
    with patch.object(sems_api_module, "request_timeout", side_effect=sems_api_module.BudgetCancelled()), patch("requests.post") as post:
        with pytest.raises(sems_api_module.BudgetCancelled):api._ensure_web_token()
        post.assert_not_called()


def test_web_headers_override_requests_default_user_agent_without_changing_client():
    import requests
    api = _make_api()
    api._web_token = {"uid": "u", "token": "fake", "client": "semsPlusWeb"}
    with requests.Session() as session:
        request = session.prepare_request(requests.Request(
            "POST", "https://example.invalid", headers=api._build_web_headers()
        ))
    assert request.headers["User-Agent"] == sems_api_module.SEMS_USER_AGENT
    assert request.headers["User-Agent"].startswith("Mozilla/5.0")
    assert request.headers["client"] == "semsPlusWeb"
    assert json.loads(request.headers["token"])["token"] == "fake"


@pytest.mark.parametrize("data,reason,client_kind", [
    (None, "invalid_data_type", "unavailable"),
    ([], "invalid_data_type", "unavailable"),
    ({"client": "semsPlusWeb"}, "missing_token", "expected"),
    ({"token": "", "client": "semsPlusWeb"}, "missing_token", "expected"),
    ({"token": "private-token", "client": "private-client"}, "unexpected_client", "other_string"),
    ({"token": "private-token", "client": None}, "unexpected_client", "other_type"),
    ({"token": "private-token"}, "session_shape_accepted", "missing_default"),
    ({"token": "private-token", "client": "semsPlusWeb"}, "session_shape_accepted", "expected"),
])
def test_login_diagnostic_explains_shape_without_exposing_credentials(data, reason, client_kind, caplog):
    """A valid HTTP response must explain silent rejection without logging secrets."""
    api = _make_api()
    response = _login_response(data)
    response.json.return_value["private_field"] = "private-response-value"
    with caplog.at_level("DEBUG", logger=sems_api_module.__name__), patch(
        "requests.post", return_value=response
    ) as post:
        assert api.test_authentication() is (reason == "session_shape_accepted")
    assert post.call_count == (2 if reason == "missing_token" else 1)
    assert "endpoint=original http_status=200" in caplog.text
    assert "business_code=0" in caplog.text
    assert f"reason={reason}" in caplog.text
    assert f"client_kind={client_kind}" in caplog.text
    for secret in ("private-token", "private-client", "private-response-value", "user@example.com", "password123"):
        assert secret not in caplog.text
    api.close()


def test_login_diagnostic_distinguishes_server_and_local_backoff(caplog):
    """One throttled request followed by cooldown needs no second network call."""
    api = _make_api()
    response = _login_response(None)
    response.status_code = 429
    response.headers = {"Retry-After": "60"}
    with caplog.at_level("DEBUG", logger=sems_api_module.__name__), patch(
        "requests.post", return_value=response
    ) as post:
        assert api.test_authentication() is False
        assert api.test_authentication() is False
    assert post.call_count == 1
    assert "endpoint=original reason=server_backoff" in caplog.text
    assert "reason=local_backoff" in caplog.text
    api.close()


@pytest.mark.parametrize("data", [{}, {"token": None}, {"token": ""},
                                  {"client": "semsPlusWeb"}])
@pytest.mark.parametrize("code", [0, "0", "00000"])
@pytest.mark.parametrize("first", ["original", "common"])
def test_success_without_token_uses_alternate_login(data, code, first, monkeypatch):
    """An explicitly successful empty primary session uses the alternate login."""
    second = "common" if first == "original" else "original"
    monkeypatch.setattr(sems_api_module, "_LOGIN_ORDER", (first, second))
    api = _make_api()
    with patch("requests.post", side_effect=[
        _login_response(data, code=code, endpoint=first), _endpoint_response(second, "au")
    ]) as post:
        assert api._ensure_web_token()
        assert api._ensure_web_token()
    assert [call.args[0] for call in post.call_args_list] == [
        sems_api_module._LOGIN_URLS[first], sems_api_module._LOGIN_URLS[second]]
    assert api._web_token["token"] == "fallback-token"
    assert api._web_api_base == "https://au-gateway.semsportal.com/web/sems"
    api.close()


@pytest.mark.parametrize("data,code,has_error", [
    ({}, None, False), ({}, 0, True),
    ({"client": "other"}, 0, False), ({"client": None}, 0, False),
])
def test_missing_token_does_not_bypass_ambiguous_or_rejected_login(data, code, has_error):
    """Only explicit success for the expected client permits token fallback."""
    api = _make_api()
    response = _login_response(data, code=code)
    response.json.return_value["hasError"] = has_error
    with patch("requests.post", return_value=response) as post:
        assert not api._ensure_web_token()
    post.assert_called_once()
    api.close()


def test_both_logins_without_token_stop_and_share_backoff():
    """Two empty sessions must not loop or immediately retry."""
    api = _make_api()
    with patch("requests.post", side_effect=[
        _login_response({}), _login_response({})
    ]) as post:
        assert not api._ensure_web_token()
        assert not api._ensure_web_token()
    assert post.call_count == 2
    assert api.login_attempts == 1
    api.close()


@pytest.mark.parametrize("failure", ["http401", "http429", "missing_token"])
def test_secondary_failure_never_starts_a_third_login(failure):
    api = _make_api()
    second = _login_response({}, endpoint="common")
    if failure.startswith("http"):
        second.status_code = int(failure[4:])
    with patch("requests.post", side_effect=[_login_response(None, code="100004"), second]) as post:
        for _ in range(2):
            if failure == "http401":
                with pytest.raises(sems_api_module.CloudAuthenticationError):
                    api._ensure_web_token()
            else:
                assert not api._ensure_web_token()
        assert post.call_count == 2
    assert api._web_token is None


@pytest.mark.parametrize("region", ["eu", "au"])
def test_timestamped_reader_reuses_working_original_web_login(region):
    """Mode verification must not create the failing Android login from #21."""
    api = _make_api()
    token = {"token": "shared", "uid": "fixture", "client": "semsPlusWeb"}
    report = {"sn": "TEST", "lastUpdate": "2026-09-24T10:00:00Z", "power": "0"}
    telemetry_reply = _data_response(report)
    telemetry_reply.json.return_value["code"] = "0"
    with patch("requests.post", return_value=_login_response(token, region=region)) as login, patch.object(
        api._observation_reader._session, "post", return_value=telemetry_reply
    ) as telemetry:
        assert api.test_authentication()
        assert api.fetch_status_observation("TEST") == report
        assert api.fetch_status_observation("TEST") == report
        assert api._ensure_web_token()
    assert login.call_count == 1
    assert login.call_args.args[0] == sems_api_module._LOGIN_URLS["original"]
    assert telemetry.call_count == 2
    assert all(json.loads(call.kwargs["headers"]["token"])["token"] == "shared"
               for call in telemetry.call_args_list)
    assert all(json.loads(call.kwargs["headers"]["token"])["client"] == "semsPlusWeb"
               for call in telemetry.call_args_list)
    assert api._web_api_base == f"https://{region}-gateway.semsportal.com/web/sems"
    api.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_timestamped_expiry_renews_shared_session_once(persistent):
    api = _make_api()
    api._web_token = {"token": "old", "client": "semsPlusWeb"}
    expired = _data_response(None)
    expired.json.return_value["code"] = "100001"
    fresh = _data_response({"sn": "TEST", "lastUpdate": "2026-09-24T10:00:00Z"})
    with patch("requests.post", return_value=_login_response({"token": "new", "client":"semsPlusWeb"})) as login, patch.object(
        api._observation_reader._session, "post", side_effect=[expired, expired if persistent else fresh]
    ) as telemetry:
        if persistent:
            with pytest.raises(ConnectionError, match="session renewal"):
                api.fetch_status_observation("TEST")
        else:
            assert api.fetch_status_observation("TEST")["sn"] == "TEST"
    assert login.call_count == 1
    assert telemetry.call_count == 2
    assert api._web_token["token"] == "new"
    assert api.session_recovery_attempts == 1
    api.close()


def test_timestamped_reader_shares_failed_login_backoff_without_status_requests():
    api = _make_api()
    bad = _login_response(None, code=100)
    with patch("requests.post", return_value=bad) as login, patch.object(
        api._observation_reader._session, "post"
    ) as telemetry:
        for _ in range(2):
            with pytest.raises(ConnectionError, match="Shared SEMS session"):
                api.fetch_status_observation("TEST")
        assert not api._ensure_web_token()
    assert login.call_count == 1
    telemetry.assert_not_called()
    api.close()


def test_timestamped_reader_preserves_explicit_credential_rejection():
    api = _make_api()
    rejected = _login_response(None)
    rejected.status_code = 401
    with patch("requests.post", return_value=rejected) as login, patch.object(
        api._observation_reader._session, "post"
    ) as telemetry:
        for _ in range(2):
            with pytest.raises(sems_api_module.CloudAuthenticationError):
                api.fetch_status_observation("TEST")
    assert login.call_count == 1
    telemetry.assert_not_called()
    api.close()


@pytest.mark.parametrize("fields", [{}, {"vehConnStu": 0}, {"vehConnStu": 1},
                                   {"vehConnStu": None}, {"vehConnStu": 2}])
def test_detail_preserves_explicit_vehicle_connection(fields):
    """Do not discard a connection flag or invent one for older responses."""
    api = _make_api()
    api._ensure_plant_id = MagicMock(return_value="PLANT")
    api._ensure_web_token = MagicMock(return_value=True)
    api._build_web_headers = MagicMock(return_value={})
    response = MagicMock(status_code=200)
    response.json.return_value = {"code": "00000", "data": {
        "sn": "TEST", "workState": "available_gun_no_insered", **fields,
    }}
    with patch.object(sems_api_module.requests, "post", return_value=response):
        result = api.get_data_gen2("TEST")
    assert ("vehConnStu" in result) == ("vehConnStu" in fields)
    if fields:
        assert result["vehConnStu"] == fields["vehConnStu"]
    assert result["workstate"] == "available_gun_no_insered"


def test_set_mode_socket_timeout_is_uncertain_and_never_replayed(caplog):
    """A response timeout is not a rejection, even if the wallbox applied it."""
    api = _make_api()
    api._ensure_plant_id = MagicMock(return_value="PLANT")
    api._ensure_web_token = MagicMock(return_value=True)
    api._build_web_headers = MagicMock(return_value={})
    with patch.object(api._request_gate, "request", side_effect=sems_api_module.requests.Timeout("lost reply")) as request:
        with pytest.raises(TimeoutError, match="outcome unknown"):
            api.set_charge_mode_gen2("TEST", 0, 2.6)
    assert request.call_count == 1
    assert "device outcome is unknown" in caplog.text
    assert "after 90s" not in caplog.text
    api.close()
