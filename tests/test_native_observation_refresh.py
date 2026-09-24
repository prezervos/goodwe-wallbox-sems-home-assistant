"""Refresh timeout handling must distinguish new TCP observations from stale data."""
import importlib
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_native_handover_guard import PACKAGE, coordinator

protocol = importlib.import_module(PACKAGE + ".native_protocol")


@pytest.mark.parametrize("advances,replaced,available,error,accepted", [
    (True, False, True, TimeoutError, True),
    (False, False, True, TimeoutError, False),
    (True, True, True, TimeoutError, False),
    (True, False, False, TimeoutError, False),
    (True, False, True, ConnectionError, False),
])
async def test_status_timeout_only_accepts_new_valid_same_session_report(
    advances, replaced, available, error, accepted,
):
    def report(state):
        return protocol.NativeStatus("TEST", state, 0, 4.2, 0,
                                     (0, 0, 0), (230, 230, 230), 0, 1, 0)

    link = SimpleNamespace(available=True, epoch=1, observed_at=time.monotonic()-2,
                           latest=report(1), session_guard=SimpleNamespace(limit=4.2, error=None))

    async def request(action, timeout):
        assert action == "status" and timeout == 10
        if advances:
            link.latest = report(0)
            link.observed_at = time.monotonic()
        link.epoch += int(replaced)
        link.available = available
        raise error("Status request did not complete")

    link.async_command = request
    owner = SimpleNamespace(cloud_settings=SimpleNamespace(request_refresh=Mock()),
                            transitioning=False, _closed=False, local=True,
                            serial="TEST", transport=link)
    if accepted:
        result = await coordinator.NativeCoordinator._async_read_data(owner)
        assert result["TEST"]["raw_state"] == 0
        assert result["TEST"]["power"] == 0
    else:
        with pytest.raises(coordinator.UpdateFailed):
            await coordinator.NativeCoordinator._async_read_data(owner)
