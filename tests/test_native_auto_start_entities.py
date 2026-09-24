"""Auto start entity identity, transport gates and cache validity."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.test_native_cloud_settings import owner, settings
from tests.test_native_transport import PACKAGE
from homeassistant.exceptions import HomeAssistantError

configuration = importlib.import_module(PACKAGE + ".native_configuration")
polling_module = importlib.import_module(PACKAGE + ".native_configuration_polling")


def build():
    instance = owner()
    instance.serial = "5011KHCA00000000"
    instance.entry.data["pile_generation"] = "1"
    instance.local = True
    instance.transport = SimpleNamespace(
        epoch=1,
        available=True,
        optional_read_busy=False,
        _energy_uncertain=False,
        latest=SimpleNamespace(stopped=True, state=0, minimum_power=False),
        session_guard=SimpleNamespace(phase="idle"),
        async_read_configuration=AsyncMock(
            return_value=configuration.NativeConfiguration(False, False)
        ),
        async_set_auto_start=AsyncMock(
            return_value=configuration.NativeConfiguration(True, False)
        ),
    )
    instance.configuration_polling = polling_module.NativeConfigurationPolling(instance)
    instance.configuration_polling.enabled = True
    entity = next(
        x
        for x in settings.setup_cloud_settings("switch", instance)
        if x.setting.field == "plug_and_charge"
    )
    return instance, entity


@pytest.mark.asyncio
async def test_actual_native_state_and_cloud_absence_keep_same_entity(monkeypatch):
    monkeypatch.setattr(
        settings.NativeEntity, "available", property(lambda self: True), raising=False
    )
    instance, entity = build()
    assert entity._attr_unique_id == "5011KHCA00000000-switch-plug-and-charge"
    assert not entity.available and entity.is_on is None
    await instance.configuration_polling.tick()
    assert entity.available and entity.is_on is False
    instance.local = False
    instance.cloud_settings.valid = True
    assert not entity.available and entity.is_on is None
    instance.entry.data["dashboard_functions"] = ["plugAndCharge"]
    instance.cloud_settings.values["plug_and_charge"] = True
    assert entity.available and entity.is_on is True
    instance.local = True
    instance.transport.epoch += 1
    assert not entity.available and entity.is_on is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        None,
        TimeoutError(),
        ValueError("readback mismatch"),
        ValueError("Wallbox schedule changed during Auto start update"),
    ],
)
async def test_write_uses_only_verified_state_or_unknown(error):
    instance, entity = build()
    await instance.configuration_polling.tick()
    instance.configuration_polling.error = "read_failed"
    if error:
        instance.transport.async_set_auto_start.side_effect = error
    if error:
        with pytest.raises(HomeAssistantError) as failure:
            await entity.async_turn_on()
        if str(error) == "Wallbox schedule changed during Auto start update":
            assert failure.value.translation_key == "auto_start_schedule_changed"
        assert entity.is_on is None
    else:
        await entity.async_turn_on()
        assert entity.is_on is True
        assert instance.configuration_polling.error is None
    instance.transport.async_set_auto_start.assert_awaited_once_with(True)
    instance.cloud.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["transition", "uncertain", "cloud"])
async def test_polling_does_not_expose_previous_idle_state(change):
    instance, entity = build()
    await instance.configuration_polling.tick()
    if change == "transition":
        instance.transitioning = True
    elif change == "uncertain":
        instance.transport._energy_uncertain = True
    else:
        instance.local = False
    assert not entity.available and entity.is_on is None


def test_native_only_setup_and_other_generation_capability_gates():
    instance, entity = build()
    instance.cloud = None
    assert any(
        isinstance(x, settings.AutoStartSwitch)
        for x in settings.setup_cloud_settings("switch", instance)
    )
    instance.entry.data["pile_generation"] = "2"
    assert not settings.setup_cloud_settings("switch", instance)


@pytest.mark.asyncio
async def test_reading_configuration_does_not_invalidate_start_or_hide_active_state():
    instance, entity = build()
    instance.transport.latest.stopped = False
    instance.transport.latest.state = 2
    instance.transport.session_guard.phase = "charging"
    instance.charge_mode_policy.async_setting_write = AsyncMock()
    await instance.configuration_polling.tick()
    assert entity.available and entity.is_on is False
    instance.charge_mode_policy.async_setting_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_finishes_before_write_and_cannot_replace_verified_new_value():
    instance, entity = build()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_read():
        entered.set()
        await release.wait()
        return configuration.NativeConfiguration(False, False)

    instance.transport.async_read_configuration.side_effect = slow_read
    reading = asyncio.create_task(instance.configuration_polling.tick())
    await asyncio.wait_for(entered.wait(), 1)
    writing = asyncio.create_task(entity.async_turn_on())
    await asyncio.sleep(0)
    instance.transport.async_set_auto_start.assert_not_awaited()
    release.set()
    await asyncio.wait_for(asyncio.gather(reading, writing), 1)
    assert entity.is_on is True
    instance.transport.async_set_auto_start.assert_awaited_once_with(True)


@pytest.mark.asyncio
async def test_handover_while_waiting_for_configuration_lock_prevents_write():
    instance, entity = build()
    async with instance.configuration_polling.lock:
        task = asyncio.create_task(entity.async_turn_on())
        await asyncio.sleep(0)
        instance.routing_epoch += 1
    with pytest.raises(HomeAssistantError):
        await task
    instance.transport.async_set_auto_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_original_cloud_control_has_translated_error():
    instance, entity = build()
    instance.local = False
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_turn_on()
    assert error.value.translation_key == "auto_start_tcp_only"
    instance.cloud.set_config_gen2.assert_not_called()
