#!/usr/bin/env python3
"""Verify cloud client ownership through real HA setup, reload and shutdown.

Run in a separate process from unit tests, which install HA stubs.
Only in-memory cloud data is used; TCP ownership is never activated.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from ha_runtime_smoke import Gateway


class TrackedGateway(Gateway):
    """Track resource release independently of the integration's factory."""

    def __init__(self, fail_setup: bool) -> None:
        super().__init__()
        self.fail_setup = fail_setup
        self.close_threads: list[int] = []
        self.closed = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def configure_gen2(self, *args: object) -> None:
        """Fail after client creation to exercise real HA setup cleanup."""
        if self.fail_setup:
            raise ConfigEntryNotReady("Simulated initialization failure")

    def close(self) -> None:
        """Record the actual executor thread running resource cleanup."""
        self.close_threads.append(threading.get_ident())
        self.loop.call_soon_threadsafe(self.closed.set)


async def run(config_dir: str, native: bool) -> None:
    """Exercise failed setup, retry, reload, unload and HA shutdown."""
    loop_thread = threading.get_ident()
    hass = HomeAssistant(config_dir)
    loader.async_setup(hass)
    hass.config.skip_pip = True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    await bootstrap.async_from_config_dict(
        {"http": {"server_host": "127.0.0.1", "server_port": port}}, hass
    )
    import custom_components.sems_wallbox as integration

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        native_port = probe.getsockname()[1]
    clients: list[TrackedGateway] = []

    def factory(*args: object) -> TrackedGateway:
        client = TrackedGateway(fail_setup=not clients)
        clients.append(client)
        return client

    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Lifecycle simulation",
        unique_id="5011KHCA-SIMULATED", source="user", discovery_keys={}, subentries_data=[],
        options={}, data={
            "username": "simulation", "password": "simulation",
            "wallbox_serial_No": "5011KHCA-SIMULATED", "product_model": "simulation",
            "pile_generation": "1", "dashboard_functions": ["simulation"],
            "more_device_controls": ["simulation"], "native_enabled": native,
            "native_auto_fallback": False, "native_host": "127.0.0.1",
            "native_advertised_host": "192.0.2.20", "native_port": native_port,
        },
    )

    async def assert_closed(client: TrackedGateway) -> None:
        await asyncio.wait_for(client.closed.wait(), timeout=2)
        assert client.close_threads, "HA did not close its cloud client"
        assert all(t != loop_thread for t in client.close_threads), "close ran on the HA loop"

    try:
        with patch.object(integration, "SemsApi", side_effect=factory), patch(
            "requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")
        ):
            await hass.config_entries.async_add(entry)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.SETUP_RETRY, entry.state
            assert len(clients) == 1
            await assert_closed(clients[0])

            assert await hass.config_entries.async_reload(entry.entry_id), (entry.state, entry.reason)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED, entry.state
            assert len(clients) == 2 and not clients[-1].close_threads
            previous = clients[-1]
            assert await hass.config_entries.async_reload(entry.entry_id), (entry.state, entry.reason)
            await assert_closed(previous)
            assert entry.state is ConfigEntryState.LOADED
            assert len(clients) == 3 and not clients[-1].close_threads

            previous = clients[-1]
            assert await hass.config_entries.async_unload(entry.entry_id)
            await assert_closed(previous)
            assert entry.state is ConfigEntryState.NOT_LOADED
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert len(clients) == 4 and not clients[-1].close_threads
            await hass.async_stop(force=True)
            await assert_closed(clients[-1])
            assert all(not c.writes for c in clients), "Lifecycle must not control charging"
            print(f"PASS: {'native' if native else 'cloud'} setup failure, retry, reload, unload, shutdown; close off loop")
    finally:
        if not hass.is_stopping:
            await hass.async_stop(force=True)


async def check_partial_cloud_reads(folder):
    """Verify partial telemetry and source recovery with the real HA coordinator."""
    from unittest.mock import Mock
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.sems_wallbox.coordinator import SemsUpdateCoordinator
    from custom_components.sems_wallbox.cloud_data import configuration_available
    from custom_components.sems_wallbox.cloud_observation import CloudAuthenticationError
    from custom_components.sems_wallbox.sensor import SemsPowerSensor, SemsSensor
    from custom_components.sems_wallbox.switch import SemsPhaseSwitchSwitch, SemsSwitch

    hass = HomeAssistant(folder)
    hass.config.time_zone = "UTC"
    serial = "SIMULATED-PARTIAL"
    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Partial observation",
        unique_id=serial, source="user", discovery_keys={}, subentries_data=[],
        options={}, data={"wallbox_serial_No": serial},
    )
    api = Mock()
    api.get_data_gen2.return_value = None
    api.fetch_last_charge.return_value = {
        "last_charge_work_status": 6, "last_charge_power": 4.2,
        "last_charge_energy": 0.1,
    }
    owner = SemsUpdateCoordinator(hass, entry, api)
    try:
        await owner.async_refresh()
        assert owner.last_update_success
        assert api.fetch_last_charge.call_count == 1
        values = owner.data[serial]
        assert not configuration_available(owner, serial)
        assert all(key not in values for key in ("chargeMode", "set_charge_power", "vehConnStu", "lastUpdate"))
        assert SemsPowerSensor(owner, serial).native_value == 4.2
        assert SemsSensor(owner, serial).state == "charging"
        assert not SemsPhaseSwitchSwitch(owner, serial, api).available
        charging = SemsSwitch(owner, serial, api, True)
        charging.hass = hass
        charging.async_write_ha_state = Mock()
        assert charging.available
        api.change_status_gen2.return_value = True
        await charging.async_turn_off()
        api.change_status_gen2.assert_called_once_with(serial, "stop")
        api.fetch_last_charge.return_value = {"last_charge_work_status": 6, "last_charge_power": 0}
        await owner.async_refresh()
        assert owner.last_update_success and SemsPowerSensor(owner, serial).native_value == 0
        api.fetch_last_charge.return_value = {"last_charge_work_status": 8, "last_charge_power": None}
        try:
            await owner._async_update_data()
        except UpdateFailed:
            pass
        else:
            raise AssertionError("Completed history must not mask configuration outage")
        api.get_data_gen2.return_value = {"sn": serial, "chargeMode": 0, "set_charge_power": 4.2}
        await owner.async_refresh()
        assert owner.last_update_success and configuration_available(owner, serial)
        assert owner.data[serial]["chargeMode"] == 0
        api.get_data_gen2.return_value = None
        api.fetch_last_charge.return_value = {"last_charge_work_status": 6, "last_charge_power": 4.8}
        await owner.async_refresh()
        assert owner.last_update_success and not configuration_available(owner, serial)
        assert "chargeMode" not in owner.data[serial] and "set_charge_power" not in owner.data[serial]
        api.fetch_last_charge.return_value = None
        try:
            await owner._async_update_data()
        except UpdateFailed:
            pass
        else:
            raise AssertionError("Both source failures must remain unavailable")
        api.get_data_gen2.return_value = {"sn": serial, "chargeMode": 0, "set_charge_power": 4.2}
        api.fetch_last_charge.side_effect = OSError("session unavailable")
        await owner.async_refresh()
        assert owner.last_update_success and "last_charge_power" not in owner.data[serial]
        api.get_data_gen2.return_value = {"sn": "WRONG"}
        before = api.fetch_last_charge.call_count
        try:
            await owner._async_update_data()
        except UpdateFailed:
            pass
        else:
            raise AssertionError("Mismatched detail must not authorize partial data")
        assert api.fetch_last_charge.call_count == before
        api.get_data_gen2.side_effect = CloudAuthenticationError("rejected")
        from homeassistant.exceptions import ConfigEntryAuthFailed
        try:
            await owner._async_update_data()
        except ConfigEntryAuthFailed:
            pass
        else:
            raise AssertionError("Authentication rejection must not be hidden")
        assert api.fetch_last_charge.call_count == before
        print("PASS: partial cloud telemetry, real zero, history exclusion, source recovery, identity and auth fencing")
    finally:
        owner._cancel_delayed_refresh()
        await owner.async_shutdown()
        await hass.async_stop(force=True)


async def check_modern_timestamped_cloud(folder):
    """Exercise unchanged handover freshness fences using modern source reports."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock, AsyncMock
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator
    from custom_components.sems_wallbox.cloud_data import modern_status_observation

    hass = HomeAssistant(folder)
    hass.config.time_zone = "UTC"
    serial = "5011KHCA-MODERN"
    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Modern cloud",
        unique_id=serial, source="user", discovery_keys={}, subentries_data=[],
        options={"native_auto_fallback": True}, data={
            "wallbox_serial_No": serial, "native_host": "127.0.0.1",
            "native_port": 18899, "native_advertised_host": "192.0.2.20",
        },
    )
    cloud = Mock()
    cloud.supports_timestamped_observation = True
    owner = NativeCoordinator(hass, entry, cloud)
    owner._handover_store.async_save = AsyncMock()
    now = datetime.now(timezone.utc)
    def report(stamp):
        return modern_status_observation(serial, {"workStu": "8", "updateTime": stamp.isoformat()},
                                         {"last_charge_work_status": 8, "last_charge_power": None},
                                         {"sn": serial, "chargeMode": 0, "set_charge_power": 4.2})
    try:
        owner.cloud_restored_at = now.timestamp() - 10
        for stamp in [now - timedelta(seconds=60), now + timedelta(seconds=60)]:
            cloud.fetch_status_observation.return_value = report(stamp)
            try:
                await owner._async_read_data()
            except UpdateFailed:
                pass
            else:
                raise AssertionError("Old/future modern timestamps must not confirm handover")
        owner._handover_store.async_save.assert_not_called()
        cloud.fetch_status_observation.return_value = report(now)
        values = (await owner._async_read_data())[serial]
        assert owner.cloud_restored_at is None
        owner._handover_store.async_save.assert_awaited_once_with(None)
        assert values["power"] is None and values["lastUpdate"] == now.isoformat()
        cloud.fetch_last_charge.assert_not_called()
        print("PASS: modern cloud handover rejects cached/future time, accepts real report time, preserves unknown power")
    finally:
        await owner.async_abort_setup()
        await hass.async_stop(force=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-lifecycle-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        asyncio.run(run(folder, "--native" in sys.argv))
        asyncio.run(check_partial_cloud_reads(folder))
        asyncio.run(check_modern_timestamped_cloud(folder))
