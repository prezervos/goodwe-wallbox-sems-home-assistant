#!/usr/bin/env python3
"""Verify identity clearing and inherited polling without external network access."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed


async def main(folder):
    """Exercise setup consumers and polling against simulated cloud data."""
    hass = HomeAssistant(folder)
    loader.async_setup(hass)
    import custom_components.sems_wallbox as integration
    from custom_components.sems_wallbox import config_flow
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator

    results = {}
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="sems_wallbox",
        title="Audit",
        unique_id="5011KHCA-AUDIT",
        source="user",
        discovery_keys={},
        subentries_data=[],
        options={"plant_id": "", "product_model": ""},
        data={
            "username": "fixture",
            "password": "fixture",
            "wallbox_serial_No": "5011KHCA-AUDIT",
            "plant_id": "old-plant",
            "product_model": "old-model",
            "native_host": "192.0.2.10",
            "native_advertised_host": "192.0.2.20",
            "native_port": 18899,
            "scan_interval": 123,
            "scan_interval_charging": 37,
        },
    )

    class Captured(Exception):
        """Stop setup after observing resolved identity, before device access."""

    class Client:
        """Provide telemetry while capturing identity resolution."""
        supports_timestamped_observation = True
        status = "Waiting"

        def configure_gen2(self, plant, model):
            """Record the setup result before external work can start."""
            results[current] = {"plant": plant, "model": model}
            raise Captured()

        def fetch_status_observation(self, serial):
            """Return independent simulated telemetry for interval selection."""
            return {
                "sn": serial,
                "status": self.status,
                "chargeMode": 1,
                "power": 0,
                "set_charge_power": 4.2,
            }

        get_data_gen2 = fetch_status_observation

    client = Client()
    with patch.object(integration, "_create_cloud_api", return_value=client):
        for current, setup in [
            ("blank_identity_cloud", integration._async_setup_cloud),
            ("blank_identity_native", integration._async_setup_native),
        ]:
            try:
                await setup(hass, entry)
            except Captured:
                pass

    class Options(config_flow.OptionsFlowHandler):
        """Expose the real form schema without starting an interactive flow."""
        @property
        def config_entry(self):
            """Return the test configuration entry."""
            return entry

        def async_show_form(self, **kwargs):
            """Capture the schema for default-value validation."""
            return kwargs

    flow = Options()
    flow.hass = hass
    form = await flow.async_step_init()
    defaults = form["data_schema"]({})
    results["blank_identity_form"] = {
        k: defaults[k] for k in ("plant_id", "product_model")
    }
    owner = NativeCoordinator(hass, entry, client)
    with patch.object(owner.cloud_settings, "observe_mode"):
        await owner._async_read_data()
    results["native_idle_interval_from_entry_data"] = {
        "configured": 123,
        "actual": owner.update_interval.total_seconds(),
    }
    client.status = "charging"
    with patch.object(owner.cloud_settings, "observe_mode"):
        await owner._async_read_data()
    results["native_charging_interval_from_entry_data"] = (
        owner.update_interval.total_seconds()
    )
    owner.entry = SimpleNamespace(
        options={"scan_interval_charging": 19}, data=entry.data
    )
    with patch.object(owner.cloud_settings, "observe_mode"):
        await owner._async_read_data()
    results["native_charging_option_overrides_data"] = (
        owner.update_interval.total_seconds()
    )
    owner.transport.session_guard.limit = 4.2
    owner.local = True
    try:
        await owner._async_read_data()
    except UpdateFailed as error:
        assert "telemetry is unavailable" in str(error)
    results["tcp_active_interval"] = owner.update_interval.total_seconds()
    await owner.cloud_settings.close()
    assert results["blank_identity_cloud"] == {"plant": None, "model": None}
    assert results["blank_identity_native"] == {"plant": None, "model": None}
    assert results["blank_identity_form"] == {"plant_id": "", "product_model": ""}
    assert results["native_idle_interval_from_entry_data"]["actual"] == 123
    assert results["native_charging_interval_from_entry_data"] == 37
    assert results["native_charging_option_overrides_data"] == 19
    assert results["tcp_active_interval"] == 2
    print(json.dumps({"result": "PASS", "checks": results}, indent=2))


with tempfile.TemporaryDirectory(prefix="goodwe-config-audit-") as folder:
    (Path(folder) / "custom_components").symlink_to(Path(sys.argv[1]).resolve())
    sys.path.insert(0, folder)
    with patch(
        "requests.sessions.Session.request",
        side_effect=AssertionError("External HTTP forbidden"),
    ):
        asyncio.run(main(folder))
