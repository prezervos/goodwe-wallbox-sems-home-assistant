#!/usr/bin/env python3
"""Exercise real Home Assistant lifecycle with a simulated wallbox only.

Run inside the HA Core devcontainer with its Python interpreter.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er


class Gateway:
    """Simulate explicit device reports without opening network connections."""

    def __init__(self) -> None:
        self.mode = 0
        self.power = 4.2
        self.active = False
        self.report_power = True
        self.minimum_power = False
        self.tick = 0
        self.writes = []
        self.behavior = "apply"

    def close(self):
        """Fake client has no HTTP resources to release."""

    def configure_gen2(self, *args: object) -> None:
        """Accept simulated configuration."""

    def get_data_gen2(self, serial: str) -> dict[str, object]:
        """Return a fresh simulated report."""
        self.tick += self.behavior != "stale"
        return {
            "sn": serial,
            "name": "Simulated Wallbox",
            "chargeMode": self.mode,
            "_reported_charge_mode": self.mode,
            "status": "Charging" if self.active else "Waiting",
            "power": (self.power if self.active else 0.0) if self.report_power else None,
            "startStatus": self.active,
            "ensure_minimum_charging_power": self.minimum_power,
            "set_charge_power": self.power,
            "min_charge_power": 4.2,
            "max_charge_power": 11.0,
            "lastUpdate": (
                datetime(2026, 9, 17, tzinfo=timezone.utc)
                + timedelta(seconds=self.tick)
            ).isoformat(),
        }

    def fetch_last_charge(self, serial: str) -> dict[str, int]:
        """Report simulated charging state."""
        return {"last_charge_work_status": 6 if self.active else 0}

    def fetch_device_info(self, serial: str) -> dict[str, object]:
        """Discover a generation missing from a legacy entry when requested."""
        if "--minimum-power-only" in sys.argv:
            return {"pileGeneration": 1, "moreDeviceControls": ["Dynamic_Load_Control"]}
        return {}

    def set_charge_mode_gen2(self, serial: str, mode: int, power: float | None = None,
                             ensure_minimum_charging_power: bool | None = None) -> bool:
        """Apply a mode only to the in-memory device."""
        if ensure_minimum_charging_power is not None:
            self.writes.append(["minimum", ensure_minimum_charging_power, mode])
            if self.behavior == "reject":
                return False
            if self.behavior != "ack_only":
                self.minimum_power = ensure_minimum_charging_power
            return True
        self.writes.append(["mode", mode, power])
        if self.behavior == "reject":
            return False
        if self.behavior != "ack_only":
            self.mode = mode
            if mode == 0:
                self.power = power if power is not None else 11
        return True

    def change_status_gen2(self, serial: str, command: str) -> bool:
        """Apply Start/Stop only to the in-memory device."""
        self.writes.append([command])
        self.active = command == "start"
        return True


async def run(config_dir: str) -> None:
    """Verify config flow, platforms, Store, reload and pre-Start correction."""
    hass = HomeAssistant(config_dir)
    loader.async_setup(hass)
    hass.config.skip_pip = True
    import socket

    with socket.socket() as http_probe:
        http_probe.bind(("127.0.0.1", 0))
        http_port = http_probe.getsockname()[1]
    await bootstrap.async_from_config_dict(
        {"http": {"server_host": "127.0.0.1", "server_port": http_port}}, hass
    )
    import custom_components.sems_wallbox as integration
    from custom_components.sems_wallbox.charge_mode_policy import (
        CONF_REMEMBER_MODE,
    )

    gateway = Gateway()
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="sems_wallbox",
        title="Simulated Wallbox",
        unique_id="SIMULATED",
        source="user",
        discovery_keys={},
        subentries_data=[],
        data={
            "username": "simulation",
            "password": "simulation",
            "wallbox_serial_No": "SIMULATED",
            "product_model": "simulation",
            **({"more_device_controls": ["Dynamic_Load_Control"]}
               if "--minimum-power-only" in sys.argv else {}),
        },
        options={CONF_REMEMBER_MODE: True},
    )
    try:
        with (
            patch.object(integration, "SemsApi", return_value=gateway),
            patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Network forbidden"),
            ),
        ):
            form = await hass.config_entries.flow.async_init(
                "sems_wallbox", context={"source": "user"}
            )
            assert form["type"] == "form", form
            hass.config_entries.flow.async_abort(form["flow_id"])
            await hass.config_entries.async_add(entry)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED, entry.state
            registry = er.async_get(hass)
            entities = er.async_entries_for_config_entry(registry, entry.entry_id)
            switch = next(
                e.entity_id
                for e in entities
                if e.unique_id.endswith("-switch-start-charging")
            )
            select = next(
                e.entity_id
                for e in entities
                if e.unique_id.endswith("-select-charge-mode")
            )
            assert not gateway.writes, gateway.writes
            if "--enable-mode-only" in sys.argv:
                hass.config_entries.async_update_entry(
                    entry, options=dict(entry.options, remember_charge_mode=False)
                )
                await hass.async_block_till_done()
                await hass.services.async_call(
                    "select", "select_option",
                    {"entity_id": select, "option": "pv_and_battery"}, blocking=True
                )
                owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                assert owner.charge_mode_policy.desired_mode == 2
                gateway.mode = 0  # Independent external change, not a HA selection.
                await owner.async_refresh()
                gateway.writes.clear()
                form = await hass.config_entries.options.async_init(entry.entry_id)
                values = form["data_schema"]({
                    "native_enabled": False, "remember_charge_mode": True
                })
                result = await hass.config_entries.options.async_configure(
                    form["flow_id"], values
                )
                assert result["type"] == "create_entry", result
                await hass.async_block_till_done()
                owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                assert owner.charge_mode_policy.desired_mode == 0
                assert owner.charge_mode_policy.enabled
                assert not gateway.writes, gateway.writes
                assert await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()
                owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                assert owner.charge_mode_policy.desired_mode == 0
                assert not gateway.writes, gateway.writes
                assert await hass.config_entries.async_unload(entry.entry_id)
                print("PASS: real options enable adopts current Fast over stale PV+battery, persists across reload, no device writes")
                return

            if "--minimum-power-only" in sys.argv:
                minimum = next(e.entity_id for e in entities
                               if e.unique_id.endswith("-switch-ensure-minimum-power"))
                owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                for option, mode, enabled in [("pv_priority", 1, True),
                                               ("pv_and_battery", 2, False)]:
                    await hass.services.async_call("select", "select_option",
                        {"entity_id": select, "option": option}, blocking=True)
                    if mode == 1:
                        # Missing or active measurements must not reach the setter.
                        for active, report_power in [(True, True), (False, False)]:
                            gateway.active = active
                            gateway.report_power = report_power
                            await owner.async_refresh()
                            writes_before = list(gateway.writes)
                            try:
                                await hass.services.async_call("switch", "turn_on",
                                    {"entity_id": minimum}, blocking=True)
                            except HomeAssistantError as error:
                                assert error.translation_key == "minimum_power_stop_first"
                            else:
                                raise AssertionError("Unconfirmed idle allowed a minimum-power write")
                            assert gateway.writes == writes_before
                        gateway.active = False
                        gateway.report_power = True
                        await owner.async_refresh()
                    await hass.services.async_call("switch", "turn_on" if enabled else "turn_off",
                        {"entity_id": minimum}, blocking=True)
                    assert gateway.writes[-1] == ["minimum", enabled, mode]
                    await owner.async_refresh()
                    assert hass.states.get(minimum).state == ("on" if enabled else "off")
                gateway.behavior = "ack_only"
                await hass.services.async_call("switch", "turn_on",
                    {"entity_id": minimum}, blocking=True)
                await owner.async_refresh()
                assert hass.states.get(minimum).state == "off"
                gateway.behavior = "reject"
                try:
                    await hass.services.async_call("switch", "turn_on",
                        {"entity_id": minimum}, blocking=True)
                except HomeAssistantError:
                    pass
                else:
                    raise AssertionError("Rejected minimum-power write was hidden")
                assert await hass.config_entries.async_unload(entry.entry_id)
                print("PASS: cloud-only HA registration, active/unknown idle guards, both PV modes, boolean encoder, readback, rejection, unload")
                return
            if "--mqtt-only" in sys.argv:
                from ha_cloud_push_control_checks import check_controls
                await check_controls(hass, entry, gateway, switch)
                assert await hass.config_entries.async_unload(entry.entry_id)
                return
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select, "option": "pv_and_battery"},
                blocking=True,
            )
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select, "option": "fast"},
                blocking=True,
            )
            gateway.mode = 1
            gateway.power = 11
            gateway.writes.clear()
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert not gateway.writes, "Reload must not write to the device"
            policy = hass.data["sems_wallbox"][entry.entry_id][
                "coordinator"
            ].charge_mode_policy
            assert policy.desired_mode == 0
            assert policy.desired_power == 4.2
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True
            )
            assert gateway.writes == [["mode", 0, 4.2], ["start"]], gateway.writes
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": switch}, blocking=True
            )
            for behavior in ("reject", "ack_only", "stale"):
                gateway.mode = 1
                gateway.writes.clear()
                gateway.behavior = behavior
                policy.timeout = 0.15
                policy.interval = 0.01
                try:
                    await hass.services.async_call(
                        "switch", "turn_on", {"entity_id": switch}, blocking=True
                    )
                except HomeAssistantError:
                    pass
                else:
                    raise AssertionError(f"Start unexpectedly succeeded: {behavior}")
                assert ["start"] not in gateway.writes, (behavior, gateway.writes)
            gateway.behavior = "apply"
            options = await hass.config_entries.options.async_init(entry.entry_id)
            assert options["type"] == "form", options
            saved = await hass.config_entries.options.async_configure(
                options["flow_id"],
                user_input={
                    CONF_REMEMBER_MODE: True,
                    "scan_interval": 30,
                    "scan_interval_charging": 10,
                },
            )
            assert saved["type"] == "create_entry", saved
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            policy = hass.data["sems_wallbox"][entry.entry_id][
                "coordinator"
            ].charge_mode_policy
            assert policy.desired_mode == 0, (
                "Changing the seed must preserve saved intent"
            )
            owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            with patch.object(gateway, "get_data_gen2", return_value={"sn": "OTHER"}), patch.object(
                gateway, "fetch_last_charge"
            ) as history:
                await owner.async_refresh()
                assert not owner.last_update_success
                assert "OTHER" not in owner.data
                history.assert_not_called()
            await owner.async_refresh()
            assert owner.last_update_success
            with patch.object(owner, "async_request_refresh", new_callable=AsyncMock) as refresh:
                owner.schedule_delayed_refresh(0.05)
                assert await hass.config_entries.async_unload(entry.entry_id)
                await asyncio.sleep(0.1)
                await hass.async_block_till_done()
                assert owner._pending_refresh_cancel is None
                refresh.assert_not_awaited()
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "entities": len(entities),
                        "checks": [
                            "config_flow",
                            "platform_setup",
                            "manual_select",
                            "store_reload",
                            "external_pv_reset",
                            "saved_power_survives_11kw_reset_and_reload",
                            "verified_start",
                            "stop",
                            "rejected_mode_blocks_start",
                            "ack_only_blocks_start",
                            "stale_report_blocks_start",
                            "options_save_reload",
                            "foreign_identity_rejected",
                            "delayed_refresh_cancelled_on_unload",
                            "unload",
                        ],
                        "simulated_writes": gateway.writes,
                    }
                )
            )
    finally:
        await hass.async_stop(force=True)


if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-runtime-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        asyncio.run(run(folder))
