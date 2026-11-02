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
async def test_stop_drains_the_transmitted_uncertain_setting_without_replay(monkeypatch, cancel_caller):
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
        expected = asyncio.CancelledError if cancel_caller else api_module.CloudSettingError
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


@pytest.mark.parametrize("uncertain", [True, False])
@pytest.mark.parametrize("wrapped", [True, False])
def test_cloud_command_error_text_and_log_preserve_outcome(caplog, uncertain, wrapped):
    errors = importlib.import_module(PACKAGE + ".ui_errors")
    commands = importlib.import_module(PACKAGE + ".cloud_command")
    cause = commands.CloudCommandError("stop", "C0001" if uncertain else "invalid", uncertain=uncertain)
    error = cause
    if wrapped:
        error = RuntimeError("Operation failed")
        error.__cause__ = cause
    result = errors.operation_error(error)
    assert result.translation_key == (
        "cloud_command_outcome_unknown" if uncertain else "cloud_command_rejected")
    expected = "outcome unknown" if uncertain else "rejected"
    assert expected in str(result)
    assert expected in caplog.text
    assert "Wallbox operation failed" not in caplog.text
    assert "acknowledgement failed" not in str(result)


@pytest.mark.parametrize("fallback", [None, "disabled", "enabled", "paused", "blocked"])
async def test_command_retry_scope_does_not_leak_into_background_polling(fallback):
    confirmation = importlib.import_module(PACKAGE + ".write_confirmation")
    route = None if fallback is None else SimpleNamespace(
        enabled=fallback != "disabled", paused=fallback == "paused", blocked=fallback == "blocked")
    owner = SimpleNamespace(automatic_fallback=route)
    entity = SimpleNamespace(coordinator=owner)
    hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
    expected = (2.0, 5.0) if fallback == "enabled" else (2.0, 5.0, 10.0)

    @confirmation.confirm_write("chargeMode")
    async def command(entity):
        direct = await budget_module.async_execute(hass, budget_module.cloud_detail_retry_delays)
        background = await asyncio.create_task(budget_module.async_execute(
            hass, budget_module.cloud_detail_retry_delays))
        assert direct == expected
        assert background == (2.0, 5.0)

    await command(entity)
    assert budget_module.cloud_detail_retry_delays() == (2.0, 5.0)


@pytest.mark.parametrize("enabled,failures,attempts,accepted", [
    (True, 3, 3, False), (False, 3, 4, True), (False, 4, 4, False),
])
async def test_entity_command_selects_bounded_detail_retry_policy(
    monkeypatch, enabled, failures, attempts, accepted
):
    confirmation = importlib.import_module(PACKAGE + ".write_confirmation")
    api = api_module.SemsApi(None, "simulation", "simulation")
    api._ensure_web_token = lambda **kwargs: True
    api._ensure_plant_id = lambda: "PLANT"
    api._build_web_headers = lambda: {}
    responses = []
    for index in range(attempts):
        payload = {"code": "B0001"} if index < failures else {
            "code": "00000", "data": {"sn": "TEST", "chargeMode": 0}}
        responses.append(Mock(status_code=200, json=Mock(return_value=payload)))
    post = Mock(side_effect=responses)
    waits = Mock()
    monkeypatch.setattr(api_module.requests, "post", post)
    monkeypatch.setattr(api, "_wait_detail_retry", waits)
    entity = SimpleNamespace(coordinator=SimpleNamespace(automatic_fallback=SimpleNamespace(
        enabled=enabled, paused=False, blocked=False)))
    hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)

    @confirmation.confirm_write("chargeMode")
    async def command(entity):
        return await budget_module.async_execute(hass, api.get_data_gen2, "TEST")

    try:
        result = await command(entity)
        assert (result is not None) is accepted
        assert post.call_count == attempts
        assert [call.args[0] for call in waits.call_args_list] == (
            [2.0, 5.0] if enabled else [2.0, 5.0, 10.0])
        assert all(call.args[0].endswith("/detail") for call in post.call_args_list)
    finally:
        api.close()


@pytest.mark.parametrize("interrupt", ["stop", "cancel", "close", "deadline"])
async def test_extra_ten_second_read_backoff_is_interruptible(monkeypatch, interrupt):
    confirmation = importlib.import_module(PACKAGE + ".write_confirmation")
    modes = importlib.import_module(PACKAGE + ".charge_mode_policy")
    api = api_module.SemsApi(None, "simulation", "simulation")
    api._ensure_web_token = lambda **kwargs: True
    api._ensure_plant_id = lambda: "PLANT"
    api._build_web_headers = lambda: {}
    post = Mock(return_value=Mock(status_code=200, json=Mock(return_value={"code": "B0001"})))
    monkeypatch.setattr(api_module.requests, "post", post)
    loop = asyncio.get_running_loop()
    waiting = asyncio.Event()
    original_wait = api._wait_detail_retry

    def wait(seconds):
        if seconds == 10:
            loop.call_soon_threadsafe(waiting.set)
            original_wait(seconds)

    monkeypatch.setattr(api, "_wait_detail_retry", wait)
    hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
    policy = Policy(SimpleNamespace(stop=AsyncMock(return_value=True)), Store(),
                    enabled=True, timeout=0.15 if interrupt == "deadline" else 3)
    entity = SimpleNamespace(coordinator=SimpleNamespace())

    @confirmation.confirm_write("chargeMode")
    async def command(entity):
        async def read():
            return await budget_module.async_execute(hass, api.get_data_gen2, "TEST")
        if interrupt in ("stop", "deadline"):
            return await policy.async_setting_write(read)
        return await read()

    task = asyncio.create_task(command(entity))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        if interrupt == "stop":
            await asyncio.wait_for(policy.async_stop(), 1)
        elif interrupt == "cancel":
            task.cancel()
        elif interrupt == "close":
            await asyncio.wait_for(asyncio.to_thread(api.close), 1)
        result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1))[0]
        expected = {"stop": modes.RequestSuperseded, "cancel": asyncio.CancelledError,
                    "close": budget_module.BudgetCancelled, "deadline": modes.ModeVerificationError}
        assert isinstance(result, expected[interrupt]), result
        assert post.call_count == 3  # Fourth read and any write were prevented.
        assert budget_module.cloud_detail_retry_delays() == (2.0, 5.0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        api.close()
