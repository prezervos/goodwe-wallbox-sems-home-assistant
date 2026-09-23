"""Exercise adaptive polling with the real HA coordinator, without network I/O."""

import asyncio
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator


async def run(folder):
    from custom_components.sems_wallbox.cloud_push import CloudPush

    hass = HomeAssistant(folder)
    hass.config.time_zone = "UTC"

    class Coordinator(DataUpdateCoordinator):
        def __init__(self):
            super().__init__(
                hass,
                logging.getLogger(__name__),
                name="push test",
                config_entry=None,
                update_interval=timedelta(seconds=1),
            )
            self.local = False
            self.transitioning = False
            self.routing_epoch = 1
            self.reads = 0

        async def _async_update_data(self):
            self.reads += 1
            data = {
                "lastUpdate": datetime.now(timezone.utc).isoformat(),
                "power": 4.2,
                "set_charge_power": 4.2,
                "chargeMode": 0,
                "status": "charging",
            }
            self.update_interval = push.polling.observed(data, 1)
            return data

    owner = Coordinator()
    push = CloudPush(owner, "TEST", lambda: {})
    push.DEBOUNCE = 0.001
    push.connected = True
    unsubscribe = owner.async_add_listener(lambda: None)
    try:
        await owner.async_refresh()
        for _ in range(3):
            await asyncio.sleep(0.01)
            push.polling.telemetry_hint()
            await owner.async_refresh()
        assert push.polling.extended
        assert owner.update_interval == timedelta(seconds=2)
        before = owner.reads
        push.connected = False
        push._check_polling()
        assert owner.update_interval == timedelta(seconds=1)
        await push._refresh_task
        assert owner.reads > before
        assert not push.polling.extended
        print(
            "PASS: real HA refresh, qualified interval, disconnect refresh and normal scheduler"
        )
    finally:
        unsubscribe()
        await push.close()
        await owner.async_shutdown()
        await hass.async_stop(force=True)


if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-cloud-push-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        asyncio.run(run(folder))
