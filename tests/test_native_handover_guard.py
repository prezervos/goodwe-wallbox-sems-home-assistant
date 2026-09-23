"""Verify cloud handover does not cut off a pending protective Stop."""

import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

PACKAGE = "sems_guard_handover_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "sems_wallbox")
]
sys.modules[PACKAGE] = package
storage_stub = types.ModuleType("homeassistant.helpers.storage")
storage_stub.Store = object
with patch.dict(sys.modules, {"homeassistant.helpers.storage": storage_stub}):
    coordinator = importlib.import_module(PACKAGE + ".native_coordinator")


@pytest.mark.asyncio
async def test_cloud_handover_waits_for_protective_stop():
    finished = asyncio.Event()
    protection = asyncio.create_task(finished.wait())
    owner = types.SimpleNamespace(
        local=True,
        endpoint=types.SimpleNamespace(journal={}, async_restore=AsyncMock()),
        transport=types.SimpleNamespace(
            session_guard=types.SimpleNamespace(task=protection),
            async_disconnect=AsyncMock(),
        ),
        transitioning=False,
        last_update_success=True,
        routing_epoch=0,
        async_update_listeners=Mock(),
        _mark_cloud_handover=AsyncMock(),
    )
    operation = asyncio.create_task(
        coordinator.NativeCoordinator._set_local(owner, False)
    )
    try:
        await asyncio.sleep(0)
        assert owner.transitioning
        owner.endpoint.async_restore.assert_not_awaited()
        owner.transport.async_disconnect.assert_not_awaited()
        finished.set()
        await operation
        owner.endpoint.async_restore.assert_awaited_once()
        owner.transport.async_disconnect.assert_awaited_once_with(expected=True)
        assert not owner.local
        assert not owner.transitioning
    finally:
        finished.set()
        await asyncio.gather(protection, operation, return_exceptions=True)


def test_protection_notification_updates_one_ui_notice_with_measurements():
    notifications = types.ModuleType("homeassistant.components.persistent_notification")
    notifications.async_create = Mock()
    guard = types.SimpleNamespace(
        error="Measured power exceeded the session limit; Stop pending",
        requested_at_violation=4.2,
        measured_at_violation=10.8,
    )
    owner = types.SimpleNamespace(
        _closed=False,
        serial="TEST-WALLBOX",
        transport=types.SimpleNamespace(session_guard=guard),
        async_update_listeners=Mock(),
        hass=object(),
        entry=types.SimpleNamespace(entry_id="entry-1"),
    )
    with patch.dict(sys.modules, {notifications.__name__: notifications}):
        coordinator.NativeCoordinator._async_protection_changed(owner)
        guard.error = "Measured power exceeded the session limit; Stop confirmed"
        coordinator.NativeCoordinator._async_protection_changed(owner)
    assert owner.async_update_listeners.call_count == 2
    calls = notifications.async_create.call_args_list
    assert len(calls) == 2
    assert "Stop pending" in calls[0].args[1]
    assert "Stop confirmed" in calls[1].args[1]
    assert "4.2 kW" in calls[1].args[1] and "10.8 kW" in calls[1].args[1]
    assert calls[0].kwargs["notification_id"] == calls[1].kwargs["notification_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_intent", ["stop", "power"])
async def test_routing_fences_start_when_new_intent_arrives_during_native_preparation(
    new_intent,
):
    entered = asyncio.Event()
    resume = asyncio.Event()
    delivered = []
    owner = types.SimpleNamespace(local=True)

    async def native_start(*, power, start_allowed):
        assert power == 4.2
        entered.set()
        await resume.wait()
        if not start_allowed():
            raise coordinator.ModeVerificationError(
                "Charging request superseded; Start was not sent"
            )
        delivered.append("start")
        return True

    observation = importlib.import_module(
        PACKAGE + ".charge_mode_policy"
    ).ModeObservation(0, 4.2)
    owner.native_adapter = types.SimpleNamespace(
        read=AsyncMock(return_value=observation),
        start=native_start,
        stop=AsyncMock(return_value=True),
    )
    policy = coordinator.ChargeModePolicy(
        coordinator.RoutingAdapter(owner),
        types.SimpleNamespace(async_save=AsyncMock()),
        enabled=True,
    )
    owner.charge_mode_policy = policy
    policy.desired_power = 4.2
    start = asyncio.create_task(policy.async_start())
    newer = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if new_intent == "stop":
            newer = asyncio.create_task(policy.async_stop())
        else:
            newer = asyncio.create_task(
                policy.async_setting_write(
                    AsyncMock(return_value=True), desired_power=5.5
                )
            )
        await asyncio.sleep(0)
        resume.set()
        with pytest.raises(
            coordinator.ModeVerificationError, match="Start was not sent"
        ):
            await start
        await newer
        assert delivered == []
        if new_intent == "stop":
            owner.native_adapter.stop.assert_awaited_once()
        else:
            assert policy.desired_power == 5.5
    finally:
        resume.set()
        await asyncio.gather(
            start, *([newer] if newer is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", [[11.0], [11.0, 5.5, 4.2]])
async def test_real_routing_reprepares_latest_power_without_replaying_start(targets):
    from tests.test_native_transport import Device, SERIAL, ready

    server = coordinator.NativeTransport(SERIAL, "127.0.0.1")
    await server.async_listen("127.0.0.1", 0)
    device = Device()
    await device.connect(server.port)
    peer = asyncio.create_task(device.run())
    entered, resume = asyncio.Event(), asyncio.Event()
    send = server._send

    async def intercept(action, **values):
        await send(action, **values)
        if action == "power" and not entered.is_set():
            entered.set()
            await resume.wait()

    server._send = intercept
    owner = types.SimpleNamespace(
        local=True, native_adapter=coordinator.NativeModeAdapter(server)
    )
    policy = coordinator.ChargeModePolicy(
        coordinator.RoutingAdapter(owner),
        types.SimpleNamespace(async_save=AsyncMock()),
        enabled=True,
        timeout=10,
    )
    owner.charge_mode_policy = policy
    policy.desired_power = 4.2
    tasks = []
    try:
        await ready(server)
        starting = asyncio.create_task(policy.async_start())
        tasks.append(starting)
        await asyncio.wait_for(entered.wait(), 3)
        for target in targets:
            setting = asyncio.create_task(
                policy.async_setting_write(
                    lambda target=target: server.async_command(
                        "power", tenths_kw=round(target * 10)
                    ),
                    desired_power=target,
                )
            )
            tasks.append(setting)
            await asyncio.sleep(0)
        resume.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 8
        )
        assert outcomes[0] is None
        assert not isinstance(outcomes[-1], BaseException)
        for outcome in outcomes[1:-1]:
            assert isinstance(outcome, coordinator.ModeVerificationError)
        async with asyncio.timeout(1):
            while 7 not in device.writes:
                await asyncio.sleep(0.01)
        assert device.writes == [1, 1, 1, 7]
        assert device.limit == round(targets[-1] * 10)
        assert server.session_guard.limit == targets[-1]
        assert server.session_guard.error is None
    finally:
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.async_close()
        await device.close()
        await peer
