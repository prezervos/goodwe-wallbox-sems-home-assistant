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


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-lifecycle-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        asyncio.run(run(folder, "--native" in sys.argv))
