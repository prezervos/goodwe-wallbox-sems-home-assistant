"""Exercise independent cloud settings and late executor responses in real HA."""

import asyncio
import threading
from unittest.mock import patch


async def check_cloud_settings(hass, owner, cloud, entity, service, device):
    """Verify user-visible readback through HA services and background refreshes.

    Args:
        hass: Isolated Home Assistant instance.
        owner: Integration coordinator under test.
        cloud: In-memory cloud client; no remote I/O.
        entity: Resolve registered synthetic entity IDs by unique-ID suffix.
        service: Invoke a real HA service with blocking completion.
        device: Synthetic device shared with the loopback transport.
    """
    settings = owner.cloud_settings
    power_id = entity("_number_set_charge_power")
    limit_id = entity("_set_charge_power_limit")

    async def settle():
        async with asyncio.timeout(5):
            await hass.async_block_till_done()

    def reported():
        return hass.states.get(limit_id).state

    await settle()
    original = cloud.get_data_gen2
    entered = threading.Event()
    release = threading.Event()

    def delayed_read(serial):
        # Only the already-started read is delayed; service verification can read
        # newer settings concurrently through the normal executor path.
        snapshot = original(serial)
        if not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release the old settings response")
        return snapshot

    settings.invalidate()
    try:
        with patch.object(cloud, "get_data_gen2", delayed_read):
            settings.request_refresh()
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            await service("number", "set_value", power_id, value=6)
            assert owner.charge_mode_policy.desired_power == 6
            assert device.limit == 60
            assert cloud.fetch_status_observation(owner.serial)["set_charge_power"] == 4.2
            # The pre-write response is still pending and must not be trusted.
            assert not settings.valid
            assert reported() == "unknown"
            release.set()
            await settle()
        assert not settings.valid
        settings.request_refresh()
        await settle()
        assert float(reported()) == 6
        assert float(hass.states.get(power_id).state) == 6
        assert hass.states.get(power_id).attributes["reported_power_limit"] == 6

        # A failed read must not turn intended power into reported power.
        settings.invalidate()
        with patch.object(cloud, "get_data_gen2", side_effect=ConnectionError("offline fixture")):
            settings.request_refresh()
            await settle()
        assert reported() == "unknown"
        assert owner.charge_mode_policy.desired_power == 6
        settings.invalidate()
        settings.request_refresh()
        await settle()
        assert float(reported()) == 6
        await service("number", "set_value", power_id, value=4.2)
        await settle()
        assert float(reported()) == 4.2
        assert device.state == 0 and device.power == 0
        print("PASS: real HA configured limit differs from telemetry, late read fenced, failure unknown, recovery and restore")
    finally:
        release.set()
        await settle()
