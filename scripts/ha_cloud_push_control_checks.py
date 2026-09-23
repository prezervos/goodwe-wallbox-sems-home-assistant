"""Real HA service calls remain usable during an optional MQTT outage."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.sems_wallbox.cloud_push import CloudPush


async def check_controls(hass, entry, gateway, switch):
    """Inject broker loss while executing real entity Start/Stop services."""
    owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
    # Setup must reuse the control client's session, not create another login.
    from custom_components.sems_wallbox import _start_cloud_push

    gateway.fetch_mqtt_settings = MagicMock(return_value={})
    shared_settings = gateway.fetch_mqtt_settings
    with patch("custom_components.sems_wallbox.cloud_push.CloudPush") as factory:
        factory.return_value.close = AsyncMock()
        _start_cloud_push(hass, entry, owner, gateway)
        assert factory.call_args.args[2] is shared_settings
        factory.return_value.start.assert_called_once()
    del gateway.fetch_mqtt_settings
    push = CloudPush(owner, "SIMULATED", lambda: {})
    push.CHECK_INTERVAL = 0.01
    push.RETRY_MIN = 0.01
    push.RETRY_MAX = 0.02
    push._listen = AsyncMock(side_effect=ConnectionError("simulated broker loss"))
    owner.cloud_push = push
    original = gateway.change_status_gen2
    disconnected = asyncio.Event()

    def disconnect():
        push.connected = False
        push._check_polling()
        disconnected.set()

    def write(serial, command):
        hass.loop.call_soon_threadsafe(disconnect)
        return original(serial, command)

    gateway.change_status_gen2 = write
    before = len(gateway.writes)
    push.start()
    try:
        for action, expected in (("turn_on", True), ("turn_off", False)):
            disconnected.clear()
            push.connected = True
            await hass.services.async_call(
                "switch", action, {"entity_id": switch}, blocking=True
            )
            await asyncio.wait_for(disconnected.wait(), 2)
            await owner.async_refresh()
            assert gateway.active is expected
            assert owner.last_update_success
        assert [x[0] for x in gateway.writes[before:]].count("start") == 1
        assert [x[0] for x in gateway.writes[before:]].count("stop") == 1
        assert not gateway.active
        assert push._listen.await_count >= 1
        print(
            "PASS: real HA Start/Stop each executed once during simulated MQTT outage"
        )
    finally:
        gateway.change_status_gen2 = original
        await push.close()
        del owner.cloud_push
