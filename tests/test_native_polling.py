"""Configured cloud intervals must follow observed charging state and option precedence."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_native_handover_guard import coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("options,expected", [
    ({"scan_interval": 60, "scan_interval_charging": 17}, [60, 17, 60]),
    ({}, [45, 12, 45]),
])
async def test_state_changes_select_saved_intervals_without_extra_telemetry_reads(options, expected):
    cloud = SimpleNamespace(supports_timestamped_observation=True,
                            fetch_status_observation=Mock())
    reports = [{"sn": "test", "status": state, "power": power, "chargeMode": 0}
               for state, power in [("Waiting", 0), ("Charging", 4.2), ("Waiting", 0)]]
    cloud.fetch_status_observation.side_effect = reports
    async def execute(function, *args):
        return function(*args)
    owner = SimpleNamespace(
        cloud=cloud, local=False, transitioning=False, _closed=False, routing_epoch=0,
        serial="test", cloud_restored_at=None,
        automatic_fallback=SimpleNamespace(enabled=False),
        cloud_settings=SimpleNamespace(request_refresh=Mock(), observe_mode=Mock()),
        hass=SimpleNamespace(async_add_executor_job=execute),
        entry=SimpleNamespace(options=options, data={"scan_interval":45,"scan_interval_charging":12}),
    )
    for seconds in expected:
        await coordinator.NativeCoordinator._async_read_data(owner)
        assert owner.update_interval.total_seconds() == seconds
    assert cloud.fetch_status_observation.call_count == 3
