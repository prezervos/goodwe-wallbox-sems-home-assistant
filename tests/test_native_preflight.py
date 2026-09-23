"""Preflight probes must never publish cached cloud data or control the device."""

import asyncio
import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, Mock, patch

import pytest

PACKAGE = "preflight_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/sems_wallbox")]
sys.modules[PACKAGE] = package
module = importlib.import_module(PACKAGE + ".native_preflight")


def subject(data=None):
    return types.SimpleNamespace(
        endpoint=types.SimpleNamespace(journal={"original": "TCP,Client,22345,regional.example,TLS"}),
        cloud=types.SimpleNamespace(fetch_status_observation=Mock(), get_data_gen2=Mock()),
        serial="example", data={"example": {"power": 4.2}},
        hass=types.SimpleNamespace(async_add_executor_job=AsyncMock(return_value=data)),
    )


@pytest.mark.asyncio
async def test_saved_destination_and_stale_offline_api_response_are_accepted():
    owner = subject({"sn": "example", "status": "offline", "lastUpdate": "2020-01-01"})
    writer = Mock(wait_closed=AsyncMock())
    with patch.object(module.asyncio, "open_connection", AsyncMock(return_value=(None, writer))) as connect:
        assert await module.async_cloud_preflight(owner)
    connect.assert_awaited_once_with("regional.example", 22345)
    writer.close.assert_called_once()
    writer.write.assert_not_called()
    assert owner.data == {"example": {"power": 4.2}}
    assert owner.hass.async_add_executor_job.await_count == 2
    owner.hass.async_add_executor_job.assert_awaited_with(owner.cloud.get_data_gen2, "example")


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, {}, {"sn": "another"}])
async def test_api_identity_must_match(data):
    owner = subject(data)
    with patch.object(module.asyncio, "open_connection", AsyncMock(return_value=(None, Mock(wait_closed=AsyncMock())))):
        assert not await module.async_cloud_preflight(owner)


@pytest.mark.asyncio
async def test_unreachable_endpoint_does_not_call_api():
    owner = subject()
    with patch.object(module.asyncio, "open_connection", AsyncMock(side_effect=ConnectionRefusedError())):
        with pytest.raises(ConnectionRefusedError):
            await module.async_cloud_preflight(owner)
    owner.hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_ownership_does_not_open_connection():
    owner = subject(); owner.endpoint.journal = None
    with patch.object(module.asyncio, "open_connection", AsyncMock()) as connect:
        assert not await module.async_cloud_preflight(owner)
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_probe_connection():
    owner = subject()
    owner.hass.async_add_executor_job.side_effect = asyncio.CancelledError
    writer = Mock(wait_closed=AsyncMock())
    with patch.object(module.asyncio, "open_connection", AsyncMock(return_value=(None, writer))):
        with pytest.raises(asyncio.CancelledError):
            await module.async_cloud_preflight(owner)
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_slow_connect_is_bounded():
    owner = subject()
    real_timeout = asyncio.timeout
    async def slow(*args):
        await asyncio.Event().wait()
    with patch.object(module.asyncio, "open_connection", slow), patch.object(module.asyncio, "timeout", side_effect=lambda seconds: real_timeout(.01)):
        with pytest.raises(TimeoutError):
            await module.async_cloud_preflight(owner)
    owner.hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_healthy_telemetry_without_control_session_does_not_restore_cloud():
    owner = subject()
    owner.hass.async_add_executor_job.side_effect = [{"sn": "example"}, None]
    with patch.object(module.asyncio, "open_connection", AsyncMock(return_value=(None, Mock(wait_closed=AsyncMock())))):
        assert not await module.async_cloud_preflight(owner)
