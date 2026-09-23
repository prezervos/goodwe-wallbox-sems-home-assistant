#!/usr/bin/env python3
"""Exercise real HA reauth/update listeners without changing a TCP route."""

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

from ha_runtime_smoke import Gateway


class AccountGateway(Gateway):
    """Keep account changes visible while all device reports remain simulated."""

    def __init__(self):
        super().__init__()
        self.replacements = []
        self.close_count = 0

    def test_authentication(self):
        return True

    def fetch_status_observation(self, serial):
        return {"sn": serial}

    def replace_credentials(self, username, password):
        self.replacements.append((username, password, threading.get_ident()))

    def close(self):
        self.close_count += 1


async def run(folder):
    hass = HomeAssistant(folder)
    loader.async_setup(hass)
    hass.config.skip_pip = True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        http_port = probe.getsockname()[1]
    await bootstrap.async_from_config_dict(
        {"http": {"server_host": "127.0.0.1", "server_port": http_port}}, hass)
    import custom_components.sems_wallbox as integration
    from custom_components.sems_wallbox import config_flow

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        native_port = probe.getsockname()[1]
    active = AccountGateway()
    temporary = []
    def validation_client(*args):
        client = AccountGateway()
        temporary.append(client)
        return client

    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Reauth simulation",
        unique_id="5011KHCA-SIMULATED", source="user", discovery_keys={}, subentries_data=[],
        options={}, data={"username": "old", "password": "simulation",
            "wallbox_serial_No": "5011KHCA-SIMULATED", "product_model": "simulation",
            "pile_generation": "1", "dashboard_functions": ["simulation"],
            "more_device_controls": ["simulation"], "native_enabled": True,
            "native_auto_fallback": False, "native_host": "127.0.0.1",
            "native_advertised_host": "192.0.2.20", "native_port": native_port,
            })
    owner = None
    try:
        with patch.object(integration, "SemsApi", return_value=active), patch.object(
            config_flow, "SemsApi", side_effect=validation_client
        ), patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")):
            await hass.config_entries.async_add(entry)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            original_listener = owner.transport._server
            # Simulate an owned TCP route without connecting to a wallbox.
            owner.local = True
            with patch.object(owner, "_set_local", side_effect=AssertionError("Reauth changed route")), patch.object(
                hass.config_entries, "async_reload", side_effect=AssertionError("Reauth reloaded TCP")
            ):
                for manual in (True, False):
                    await owner.connection_intent.async_manual(manual)
                    owner.automatic_fallback.paused = manual
                    owner.automatic_fallback.blocked = True
                    owner.automatic_fallback.reason = "authentication_failed"
                    flow = await hass.config_entries.flow.async_init(
                        "sems_wallbox", context={"source": "reauth", "entry_id": entry.entry_id},
                        data=dict(entry.data))
                    assert flow["step_id"] == "reauth_confirm", flow
                    result = await hass.config_entries.flow.async_configure(
                        flow["flow_id"], {"username": "updated", "password": "updated"})
                    assert result["reason"] == "reauth_successful", result
                    await hass.async_block_till_done()
                    assert owner is hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                    assert owner.local and owner.transport._server is original_listener
                    assert owner.connection_intent.manual_tcp is manual
                    assert owner.automatic_fallback.paused is manual
                    assert not owner.automatic_fallback.blocked
                    assert entry.state is ConfigEntryState.LOADED
            assert len(active.replacements) == 2
            assert all(t != threading.get_ident() for _, _, t in active.replacements)
            assert len(temporary) == 2 and all(c.close_count == 1 for c in temporary)
            assert not active.writes
            print("PASS: real HA credential change and unchanged-credential retry preserve TCP, manual preference, entry and listener; temporary clients close off loop")
    finally:
        if owner is not None:
            owner.local = False
        await hass.async_stop(force=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-reauth-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        asyncio.run(run(folder))
