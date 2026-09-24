"""HTTP-boundary cooldown, cross-client sharing and dispatch safety checks."""

from concurrent.futures import ThreadPoolExecutor
import importlib
import threading
from unittest.mock import Mock, patch

import pytest
import requests

from tests.test_sems_api import _make_api, sems_api_module

module = importlib.import_module(sems_api_module.__package__ + ".cloud_rate_limit")


def response(status=200, header=None):
    result = requests.Response()
    result.status_code = status
    result._content = b'{"code":"00000","data":{"sn":"TEST"}}'
    if header is not None:
        result.headers["Retry-After"] = header
    return result


@pytest.mark.parametrize("status,header,delay", [
    (429, "600", 600), (503, "600", 600),
    (429, None, 30), (429, "invalid", 30), (429, "nan", 30),
    (429, "inf", 30), (429, "-1", 30), (429, "0", 1),
    (503, "Thu, 01 Jan 1970 00:30:00 GMT", 800),
])
def test_deadline_blocks_dispatch_and_expires(status, header, delay):
    gate = module.CloudRequestGate()
    send = Mock(side_effect=[response(status, header), response()])
    with patch.object(module.time, "time", return_value=1000), patch.object(
        module.time, "monotonic", return_value=100
    ):
        with pytest.raises(module.CloudRateLimitedError) as caught:
            gate.request(send)
        assert caught.value.retry_after == delay
    with patch.object(module.time, "monotonic", return_value=100 + delay - 0.1):
        with pytest.raises(module.CloudRateLimitedError):
            gate.request(send)
        send.assert_called_once()
    with patch.object(module.time, "monotonic", return_value=100 + delay):
        assert gate.request(send).status_code == 200
    assert send.call_count == 2


def test_headerless_throttling_backoff_is_bounded_and_success_resets():
    gate = module.CloudRequestGate()
    now = 100
    for expected in [30, 60, 120, 240, 300, 300]:
        with patch.object(module.time, "monotonic", return_value=now):
            with pytest.raises(module.CloudRateLimitedError) as caught:
                gate.request(lambda: response(429))
            assert caught.value.retry_after == expected
        now += expected
    with patch.object(module.time, "monotonic", return_value=now):
        gate.request(lambda: response())
        with pytest.raises(module.CloudRateLimitedError) as caught:
            gate.request(lambda: response(429))
        assert caught.value.retry_after == 30


@pytest.mark.parametrize("status", [401, 403, 500, 502, 504])
def test_unrelated_http_failures_are_not_reclassified(status):
    gate = module.CloudRequestGate()
    reply = response(status, "600")
    assert gate.request(lambda: reply) is reply
    assert gate.request(lambda: response()).status_code == 200


@pytest.mark.parametrize("source", ["web", "telemetry", "login"])
def test_cooldown_is_shared_by_polling_commands_discovery_and_login(source):
    api = _make_api()
    api._plant_id = "fixture"
    if source != "login":
        api._web_token = {"uid": "fixture", "token": "fixture"}
    api._observation_reader._token = {"token": "fixture"}
    reply = response(429, "600")
    with patch("requests.post", return_value=reply) as post, patch(
        "requests.get", return_value=reply
    ) as get, patch.object(api._observation_reader._session, "post", return_value=reply) as telemetry:
        if source == "login":
            assert not api._ensure_web_token()
        else:
            with pytest.raises(module.CloudRateLimitedError):
                if source == "web":
                    api.get_data_gen2("TEST")
                else:
                    api._observation_reader.read("TEST")
        # Cached tokens cannot bypass a deadline, nor does discovery renew them.
        api._web_token = {"uid": "fixture", "token": "fixture"}
        operations = [lambda: api.get_data_gen2("TEST"), api.fetch_mqtt_settings,
                      lambda: api.change_status_gen2("TEST", "start"),
                      lambda: api.change_status_gen2("TEST", "stop"),
                      lambda: api._observation_reader.read("TEST")]
        for operation in operations:
            with pytest.raises(module.CloudRateLimitedError):
                operation()
        if source == "login":
            assert not api._ensure_web_token(renew=True)
        else:
            with pytest.raises(module.CloudRateLimitedError):
                api._ensure_web_token(renew=True)
            with pytest.raises(module.CloudRateLimitedError):
                api.test_authentication()
        assert post.call_count + get.call_count + telemetry.call_count == 1
    api.close()


def test_concurrent_caller_cannot_dispatch_before_limit_is_recorded():
    gate = module.CloudRequestGate()
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    def first_send():
        entered.set()
        assert release.wait(2)
        return response(429, "600")
    second_send = Mock(return_value=response())
    def second_call():
        second_started.set()
        return gate.request(second_send)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(gate.request, first_send)
        try:
            assert entered.wait(2)
            second = pool.submit(second_call)
            assert second_started.wait(2)
        finally:
            release.set()
        for future in (first, second):
            with pytest.raises(module.CloudRateLimitedError):
                future.result(timeout=2)
    second_send.assert_not_called()


async def test_cancelled_budget_does_not_dispatch():
    budget_module = importlib.import_module(sems_api_module.__package__ + ".operation_budget")
    gate = module.CloudRequestGate()
    budget = budget_module.OperationBudget(10)
    budget.cancelled.set()
    token = budget_module.CURRENT_BUDGET.set(budget)
    send = Mock()
    try:
        with pytest.raises(budget_module.BudgetCancelled):
            gate.request(send, timeout=15)
    finally:
        budget_module.CURRENT_BUDGET.reset(token)
    send.assert_not_called()
