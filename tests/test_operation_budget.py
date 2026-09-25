"""Verify Stop and cancellation at the real synchronous HTTP/executor boundary."""

import asyncio
import importlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_charge_mode_policy import PACKAGE, Policy, Store

budget_module = importlib.import_module(PACKAGE + ".operation_budget")
api_module = importlib.import_module(PACKAGE + ".sems_api")


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_stop_suppresses_retries_but_drains_the_transmitted_write(monkeypatch, cancel_caller):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    timeouts = []

    def post(*args, **kwargs):
        calls.append("http")
        timeouts.append(kwargs["timeout"])
        entered.set()
        assert release.wait(2), "Test failed to release simulated HTTP"
        return SimpleNamespace(status_code=200, text="busy", raise_for_status=lambda: None,
                               json=lambda: {"code": "R0305"})

    monkeypatch.setattr(api_module.requests, "post", post)
    api = api_module.SemsApi(None, "simulation", "simulation")
    api._ensure_plant_id = lambda: "test-plant"
    api._ensure_web_token = lambda **kwargs: True
    api._build_web_headers = lambda: {}

    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)

    async def stop():
        calls.append("stop")
        return True

    policy = Policy(SimpleNamespace(stop=stop), Store(), enabled=True, timeout=1)
    hass = SimpleNamespace(async_add_executor_job=executor)
    pending = asyncio.create_task(policy.async_setting_write(
        lambda: budget_module.async_execute(hass, api.set_charge_mode_gen2, "TEST", 0, 4.2)))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        stopping = asyncio.create_task(policy.async_stop())
        if cancel_caller:
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        await asyncio.sleep(0.02)
        assert calls == ["http"]
        assert not stopping.done()
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(pending, stopping, return_exceptions=True), 1)
        expected = asyncio.CancelledError if cancel_caller else importlib.import_module(PACKAGE + ".charge_mode_policy").RequestSuperseded
        assert isinstance(outcomes[0], expected), outcomes[0]
        assert outcomes[1] is None
        assert calls == ["http", "stop"]
        assert 0 < timeouts[0] <= 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        api.close()


async def test_background_poll_does_not_inherit_finished_command_budget():
    budget = budget_module.OperationBudget(1)
    token = budget_module.CURRENT_BUDGET.set(budget)
    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)
    try:
        task = asyncio.create_task(budget_module.async_execute(
            SimpleNamespace(async_add_executor_job=executor), budget_module.request_timeout, 30))
        budget.cancelled.set()
        assert await task == 30
    finally:
        budget_module.CURRENT_BUDGET.reset(token)


@pytest.mark.parametrize("supersede", ["stop", "close", "mode"])
async def test_final_start_waiting_for_web_lock_is_cancelled(monkeypatch, supersede):
    """A newer intent prevents the first Start POST, not just preparation retries."""
    adapters = importlib.import_module(PACKAGE + ".charge_mode_adapter")
    modes = importlib.import_module(PACKAGE + ".charge_mode_policy")
    entered = asyncio.Event()
    calls = []
    loop = asyncio.get_running_loop()

    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)

    hass = SimpleNamespace(async_add_executor_job=executor)
    api = api_module.SemsApi(hass, "simulation", "simulation")
    api._ensure_plant_id = lambda: "test-plant"
    api._ensure_web_token = lambda **kwargs: True
    api._build_web_headers = lambda: {}
    original = api.change_status_gen2

    def command(serial, action):
        if action == "start":
            loop.call_soon_threadsafe(entered.set)
        return original(serial, action)

    api.change_status_gen2 = command
    response = Mock(status_code=200, text="accepted")
    response.json.return_value = {"code": "00000"}
    monkeypatch.setattr(api_module.requests, "post",
                        lambda url, **kwargs: calls.append(url.rsplit("/", 1)[-1]) or response)
    adapter = adapters.ModeTransportAdapter(hass, "TEST", api)
    adapter.read = AsyncMock(return_value=modes.ModeObservation(0, power=4.2))
    policy = Policy(adapter, Store(), enabled=True, timeout=2)
    api._web_request_lock.acquire()
    starting = asyncio.create_task(policy.async_start())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        next_request = asyncio.create_task(
            policy.async_stop() if supersede == "stop" else
            policy.async_close() if supersede == "close" else
            policy.async_remember_mode(1)
        )
        await asyncio.sleep(0)
    finally:
        api._web_request_lock.release()
    results = await asyncio.wait_for(asyncio.gather(starting, next_request, return_exceptions=True), 3)
    assert isinstance(results[0], modes.RequestSuperseded)
    assert results[1] is None
    assert calls == (["stopCharge"] if supersede == "stop" else [])
    api.close()


@pytest.mark.parametrize("failure", ["inner", "empty_inner", "outer"])
async def test_setting_timeout_preserves_origin_and_releases_budget(failure):
    modes = importlib.import_module(PACKAGE + ".charge_mode_policy")
    errors = importlib.import_module(PACKAGE + ".ui_errors")
    calls = []
    policy = Policy(SimpleNamespace(), Store(), enabled=True,
                    timeout=0.01 if failure == "outer" else 1)
    original = TimeoutError("Native confirmation timed out" if failure == "inner" else "")

    async def operation():
        calls.append("write")
        if failure == "outer":
            await asyncio.Event().wait()
        raise original

    with pytest.raises(modes.ModeVerificationError) as caught:
        await policy.async_setting_write(operation)
    expected = {"inner": "Native confirmation timed out",
                "empty_inner": "Wallbox response timed out",
                "outer": "Operation budget expired"}[failure]
    assert str(caught.value) == expected
    assert isinstance(caught.value.__cause__, TimeoutError)
    if failure != "outer":
        assert caught.value.__cause__ is original
    assert errors.operation_error(caught.value).translation_key == "operation_timeout"
    assert calls == ["write"]
    assert policy._budget is None
    assert budget_module.CURRENT_BUDGET.get() is None
    # The failed write neither holds serialization nor replays itself.
    await policy.async_setting_write(AsyncMock(return_value=True))
    assert calls == ["write"]


@pytest.mark.parametrize("enabled", [True, False])
async def test_rate_limited_setting_preserves_translated_error_without_replay(enabled):
    rates = importlib.import_module(PACKAGE + ".cloud_rate_limit")
    errors = importlib.import_module(PACKAGE + ".ui_errors")
    modes = importlib.import_module(PACKAGE + ".charge_mode_policy")
    optimism = importlib.import_module(PACKAGE + ".optimistic_write")
    policy = Policy(SimpleNamespace(), Store(), enabled=enabled, timeout=1)
    entity = SimpleNamespace(
        coordinator=SimpleNamespace(
            charge_mode_policy=policy, data={"SN": {}}, schedule_delayed_refresh=Mock()
        ),
        sn="SN", _handle_coordinator_update=Mock(),
        _pending_value=None, async_write_ha_state=Mock(),
    )
    operation = AsyncMock(side_effect=rates.CloudRateLimitedError(60))
    @modes.mode_setting_write
    @optimism.optimistic_write
    async def write(subject):
        subject._pending_value = 5
        return await operation()
    with pytest.raises(errors.HomeAssistantError) as caught:
        await write(entity)
    assert caught.value.translation_key == "cloud_rate_limited"
    assert caught.value.translation_placeholders == {"seconds": "60"}
    operation.assert_awaited_once()
    assert entity._pending_value is None
    entity._handle_coordinator_update.assert_called_once()
    entity.coordinator.schedule_delayed_refresh.assert_called_once_with(3.0)
    assert budget_module.CURRENT_BUDGET.get() is None
