#!/usr/bin/env python3
"""Run real HA entity services against loopback TCP and a simulated cloud only."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import logging

from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_native_transport import Device, SERIAL  # noqa: E402 - repository path above


async def run(config_dir):
    hass = HomeAssistant(config_dir)
    loader.async_setup(hass)
    hass.config.skip_pip = True

    with socket.socket() as http_probe:
        http_probe.bind(("127.0.0.1", 0))
        http_port = http_probe.getsockname()[1]
    await bootstrap.async_from_config_dict(
        {"http": {"server_host": "127.0.0.1", "server_port": http_port}}, hass
    )
    import custom_components.sems_wallbox as integration
    from custom_components.sems_wallbox import native_coordinator, config_flow

    device = Device()
    calls = []
    handover_entered = asyncio.Event()
    handover_release = asyncio.Event()
    handover_release.set()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class Endpoint:
        def __init__(self, peer, serial, host, listen_port, store):
            self.port = listen_port
            self.journal = None
            self.task = None

        async def async_load(self):
            pass

        async def async_activate(self):
            handover_entered.set()
            await handover_release.wait()
            calls.append("tcp")
            self.journal = {"original": f"TCP,Client,{http_port},127.0.0.1"}
            await device.connect(self.port)
            self.task = asyncio.create_task(device.run())

        async def async_restore(self):
            if self.journal:
                handover_entered.set()
                await handover_release.wait()
                calls.append("cloud")
                await device.close()
                await self.task
                self.journal = None

    class Cloud:
        stale = False
        error = None

        def replace_credentials(self, username, password):
            """Replace credentials without changing simulated device state."""
            self.username = username
            self.password = password

        def test_authentication(self):
            return True

        def fetch_status_observation(self, serial):
            return self.get_data_gen2(serial)

        def close(self):
            """Fake client has no HTTP resources to release."""

        def configure_gen2(self, *args):
            pass

        def get_data_gen2(self, serial):
            if self.error is not None:
                raise self.error
            return {
                "sn": serial,
                "chargeMode": device.mode,
                "_reported_charge_mode": device.mode,
                "set_charge_power": device.limit / 10,
                "min_charge_power": 4.2,
                "max_charge_power": 11,
                "power": device.power / 10,
                "time": "0",
                "workstate": "EVDetail_Status_Waiting_Stat01",
                "status": "charging" if device.state == 2 else "Waiting",
                "lastUpdate": "2000-01-01T00:00:00+00:00"
                if self.stale
                else datetime.now(timezone.utc).isoformat(),
                "startStatus": device.state == 2,
            }

        def fetch_last_charge(self, serial):
            return {"last_charge_work_status": 6 if device.state == 2 else 0}

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="sems_wallbox",
        title="Native loopback",
        unique_id=SERIAL,
        source="user",
        discovery_keys={},
        subentries_data=[],
        options={"remember_charge_mode": True},
        data={
            "wallbox_serial_No": SERIAL,
            "username": "fake",
            "password": "fake",
            "native_enabled": True,
            "native_auto_fallback": True,
            "native_host": "127.0.0.1",
            "native_advertised_host": "192.0.2.20",
            "native_port": port,
        },
    )
    cloud = Cloud()
    try:
        with (
            patch.object(integration, "SemsApi", return_value=cloud),
            patch.object(native_coordinator, "EndpointManager", Endpoint),
            # Advance policy deadlines explicitly; avoid background timing races.
            patch.object(native_coordinator.AutomaticFallback, "start", lambda self: None),
        ):
            settings = await config_flow.validate_native(
                {
                    "wallbox_serial_No": SERIAL,
                    "native_host": "192.0.2.10",
                    "native_advertised_host": "192.0.2.20",
                    "native_port": port,
                            "native_discover": False,
                }
            )
            assert settings["wallbox_serial_No"] == SERIAL
            await hass.config_entries.async_add(entry)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED, entry.state
            owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            assert owner.initial_power == 4.2
            assert owner.charge_mode_policy.desired_power == 4.2
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )

            def entity(suffix):
                return next(
                    e.entity_id for e in entities if e.unique_id == SERIAL + suffix
                )

            if "--lifecycle" in sys.argv:
                from ha_native_lifecycle_checks import check_lifecycle
                await check_lifecycle(
                    hass, entry, device, handover_entered, handover_release
                )
                return

            from ha_native_contract_checks import check_contracts
            await check_contracts(hass, entry, entities, cloud, SERIAL)

            # Exercise the same translation catalog and registry keys used by the UI.
            from homeassistant.helpers.translation import async_get_translations

            for language in ("en", "cs"):
                translated = await async_get_translations(
                    hass, language, "entity", {"sems_wallbox"}
                )
                for registered in entities:
                    assert registered.translation_key, registered.entity_id
                    key = (
                        "component.sems_wallbox.entity."
                        f"{registered.domain}.{registered.translation_key}"
                    )
                    assert key + ".name" in translated, (language, key)
                mode_key = "component.sems_wallbox.entity.select.charge_mode.state."
                expected = (
                    ["Fast", "PV priority", "PV & battery"]
                    if language == "en"
                    else ["Rychle", "PV Priorita", "PV a baterie"]
                )
                assert [translated[mode_key + option] for option in
                        ("fast", "pv_priority", "pv_and_battery")] == expected
                for option in ("standby", "charging", "starting", "waiting"):
                    assert translated[
                        "component.sems_wallbox.entity.sensor.status.state." + option
                    ] != option
                assert translated[
                    "component.sems_wallbox.entity.sensor.active_transport.state.tcp"
                ] != "tcp"
            print("PASS: all native entity translation keys and EN/CS UI labels")

            phase_entries = [e for e in entities if "_native_current_" in e.unique_id
                             or "_native_voltage_" in e.unique_id]
            assert len(phase_entries) == 6
            assert all(e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                       and e.entity_category.value == "diagnostic" for e in phase_entries)
            assert float(hass.states.get(entity("_charge_duration")).state) == 0
            assert hass.states.get(entity("_charge_duration")).attributes["unit_of_measurement"] == "min"
            assert hass.states.get(entity("_native_fault")).state == "unavailable"
            from custom_components.sems_wallbox.native_entities import ValueSensor
            from types import SimpleNamespace
            for raw, expected in [(None, None), ("0", 0), ("12", None), ("bad", None)]:
                subject = SimpleNamespace(field="session_seconds", index=None,
                    coordinator=SimpleNamespace(local=False), values={"time": raw},
                    _attr_native_unit_of_measurement="s")
                assert ValueSensor.native_value.fget(subject) == expected
            print("PASS: phase diagnostics disabled by default; unsupported cloud fields unavailable; no fabricated duration")

            for language in ("cs", "en"):
                errors = await async_get_translations(hass, language, "exceptions", {"sems_wallbox"})
                for key in ("invalid_power", "operation_timeout", "connection_failed", "operation_failed"):
                    assert "component.sems_wallbox.exceptions." + key + ".message" in errors
                selectors = await async_get_translations(hass, language, "selector", {"sems_wallbox"})
                assert "component.sems_wallbox.selector.initial_charge_mode.options.0" not in selectors
            form = await config_flow.ConfigFlow().async_step_user()
            validated = form["data_schema"]({"connection_type": "native_tcp"})
            assert "initial_charge_mode" not in validated

            assert hass.states.get(entity("_workstate")).state == "connected"
            activity_entry = er.async_get(hass).async_get(entity("_charging_active"))
            assert activity_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert hass.states.get(entity("_charging_active")) is None
            from custom_components.sems_wallbox.diagnostics import async_get_config_entry_diagnostics
            snapshot = await async_get_config_entry_diagnostics(hass, entry)
            serialized = json.dumps(snapshot)
            assert SERIAL not in serialized and "fake" not in serialized
            assert snapshot["transport"] == "cloud"
            connection = entity("-native-connection")
            charging = entity("-switch-start-charging")
            mode = entity("-select-charge-mode")
            power = entity("_number_set_charge_power")

            async def service(domain, action, target, **values):
                await hass.services.async_call(
                    domain, action, {"entity_id": target, **values}, blocking=True
                )

            transport_status = entity("_active_transport")

            if "--mode-preference-only" in sys.argv:
                # Exercise real HA services, disk-backed intent and TCP frames.
                # External changes bypass HA services just as SolarGo does.
                cases = []
                modes = ("fast", "pv_priority", "pv_and_battery")
                for remember in (True, False):
                    hass.config_entries.async_update_entry(
                        entry, options=dict(entry.options, remember_charge_mode=remember)
                    )
                    await hass.async_block_till_done()
                    await service("switch", "turn_on", connection)
                    for chosen, option in enumerate(modes):
                        await service("select", "select_option", mode, option=option)
                        owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                        assert owner.charge_mode_policy.desired_mode == chosen
                        for reload_first in (False, True):
                            external = (chosen + 1) % 3
                            device.mode = external
                            await device.status()
                            async with asyncio.timeout(3):
                                while owner.transport.latest.mode != external:
                                    await asyncio.sleep(0.01)
                            await owner.async_refresh()
                            assert owner.charge_mode_policy.desired_mode == chosen
                            assert hass.states.get(mode).state == modes[external]
                            assert hass.states.get(mode).attributes["preferred_mode"] == option
                            if reload_first:
                                assert await hass.config_entries.async_reload(entry.entry_id)
                                await hass.async_block_till_done()
                                owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                                assert owner.local
                                assert owner.charge_mode_policy.desired_mode == chosen
                                assert owner.charge_mode_policy.remember_mode == remember
                            await service("switch", "turn_on", charging)
                            async with asyncio.timeout(3):
                                while device.state != 2:
                                    await asyncio.sleep(0.01)
                            assert device.mode == (chosen if remember else external)
                            assert owner.charge_mode_policy.desired_mode == chosen
                            await service("switch", "turn_off", charging)
                            assert device.state == 0
                            cases.append([remember, option, reload_first])
                assert await hass.config_entries.async_unload(entry.entry_id)
                print(json.dumps({"result": "PASS", "external_mode_cases": cases}))
                return

            if "--energy-only" in sys.argv:
                from ha_native_energy_checks import check_energy
                await check_energy(hass, entry, device, SERIAL, service, connection)
                assert await hass.config_entries.async_unload(entry.entry_id)
                print("PASS: focused real HA energy checks")
                return

            async def checked_handover(enabled):
                handover_entered.clear()
                handover_release.clear()
                task = asyncio.create_task(service(
                    "switch", "turn_on" if enabled else "turn_off", connection
                ))
                try:
                    await asyncio.wait_for(handover_entered.wait(), 5)
                    await asyncio.sleep(0)
                    progress = hass.states.get(transport_status)
                    expected = "switching_to_tcp" if enabled else "switching_to_cloud"
                    assert progress.state == expected, progress
                    assert progress.attributes["icon"] == "mdi:swap-horizontal"
                    assert hass.states.get(connection).state != "unavailable"
                    assert hass.states.get(power).state != "unavailable"
                    assert hass.states.get(entity("_power")).state == "unavailable"
                finally:
                    handover_release.set()
                    await task
                assert hass.states.get(transport_status).state == (
                    "tcp" if enabled else "cloud"
                )

            assert device.writes == []
            for cycle in range(2):
                await checked_handover(True)
                from homeassistant.exceptions import HomeAssistantError
                before_invalid = list(device.writes)
                try:
                    await service("number", "set_value", power, value=4.25)
                except HomeAssistantError as error:
                    assert error.translation_domain == "sems_wallbox"
                    assert error.translation_key == "invalid_power"
                    assert error.translation_placeholders == {"minimum": "4.2", "maximum": "11"}
                else:
                    raise AssertionError("Fractional step was accepted")
                assert device.writes == before_invalid

                # Simulate a session inherited from cloud without a local Start.
                device.state, device.power, device.limit = 2, 38, 42
                await device.status()
                await service("number", "set_value", power, value=4.3)
                coordinator = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                assert coordinator.transport.session_guard.limit == 4.3
                assert coordinator.transport.session_guard.phase == "charging"
                await service("switch", "turn_off", charging)
                await service("number", "set_value", power, value=4.2)
                await service(
                    "select",
                    "select_option",
                    mode,
                    option=("pv_priority", "pv_and_battery")[cycle],
                )
                from types import SimpleNamespace
                from custom_components.sems_wallbox.native_entities import (
                    ChargePowerNumber,
                )

                for serial, bounds in (
                    ("57000HCA-TEST", (1.4, 7.0)),
                    ("5011KHCA-TEST", (4.2, 11.0)),
                    ("5022KHCA-TEST", (4.2, 22.0)),
                ):
                    subject = SimpleNamespace(
                        coordinator=SimpleNamespace(local=False, serial=serial),
                        values={},
                    )
                    assert ChargePowerNumber.native_min_value.fget(subject) == bounds[0]
                    assert ChargePowerNumber.native_max_value.fget(subject) == bounds[1]
                assert device.limit == 42
                device.behavior = "ack_only"
                await service("switch", "turn_on", charging)
                await hass.async_block_till_done()
                assert hass.states.get(charging).state == "on"
                assert coordinator.transport.session_guard.phase == "waiting"
                assert coordinator.transport.session_guard.limit == 4.2
                assert device.power == 0
                await service("switch", "turn_off", charging)
                device.behavior = "apply"
                await service("switch", "turn_on", charging)
                async with asyncio.timeout(2):
                    while device.state != 2:
                        await asyncio.sleep(0.01)
                assert device.mode == cycle + 1 and device.limit == 42
                await service("number", "set_value", power, value=4.3)
                assert device.mode == cycle + 1 and device.limit == 43
                assert coordinator.charge_mode_policy.desired_mode == cycle + 1
                assert coordinator.transport.session_guard.limit == 4.3
                await service("switch", "turn_off", charging)
                await service("number", "set_value", power, value=4.2)

                await service("select", "select_option", mode, option="fast")
                device.mode, device.limit = (
                    1,
                    110,
                )  # External reset must not change HA intent.
                await device.status()
                await service("switch", "turn_on", charging)
                async with asyncio.timeout(2):
                    while device.state != 2:
                        await asyncio.sleep(0.01)
                assert device.mode == 0 and device.limit == 42
                await service("number", "set_value", power, value=11.0)
                device.power = 108
                await device.status()
                coordinator = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                async with asyncio.timeout(2):
                    while coordinator.transport.latest.power_kw != 10.8:
                        await asyncio.sleep(0.01)
                assert coordinator.transport.session_guard.limit == 11.0
                assert coordinator.transport.session_guard.error is None
                assert hass.states.get(power).attributes["max"] == 11.0
                # No unsolicited report: a coordinator refresh must actively query
                # actual load instead of recycling the previous 10.8 kW snapshot.
                device.power = 38
                await asyncio.sleep(1.1)
                await coordinator.async_refresh()
                assert coordinator.transport.latest.power_kw == 3.8
                assert coordinator.update_interval.total_seconds() == 2
                await service("switch", "turn_off", charging)
                await service("number", "set_value", power, value=4.3)
                assert device.limit == 43
                await service("number", "set_value", power, value=4.2)
                await checked_handover(False)
                assert hass.states.get(connection).state == "off"
            await service("switch", "turn_on", connection)
            from homeassistant.components import persistent_notification

            notices = []

            def on_notification(update_type, notifications):
                notices.extend(item["message"] for item in notifications.values())

            unsubscribe = persistent_notification.async_register_callback(
                hass, on_notification
            )
            try:
                await service("switch", "turn_on", charging)
                async with asyncio.timeout(2):
                    while device.state != 2:
                        await asyncio.sleep(0.01)
                device.power = 108  # Independent simulated telemetry violates 4.2 kW.
                await device.status()
                coordinator = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
                async with asyncio.timeout(3):
                    while coordinator.transport.session_guard.task is None:
                        await asyncio.sleep(0.01)
                    await coordinator.transport.session_guard.task
                    while not any("Stop confirmed" in message for message in notices):
                        await asyncio.sleep(0.01)
                assert device.state == 0 and device.power == 0
                assert any("Stop pending" in message for message in notices)
                assert all(
                    "4.2 kW" in message and "10.8 kW" in message for message in notices
                )
            finally:
                unsubscribe()
            writes = len(device.writes)
            cloud.stale = True
            assert await hass.config_entries.async_reload(entry.entry_id)
            manual_reloaded = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            assert manual_reloaded.local and manual_reloaded.last_update_success
            assert manual_reloaded.connection_intent.manual_tcp
            assert manual_reloaded.automatic_fallback.paused
            assert len(device.writes) == writes, "Remembering TCP must not replay charging"
            await manual_reloaded.automatic_fallback.tick()
            assert manual_reloaded.local
            print("PASS: manual TCP survives reload without cloud freshness or charging replay")
            await manual_reloaded.async_set_local(False)
            assert not manual_reloaded.connection_intent.manual_tcp
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            reloaded = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            assert not reloaded.last_update_success
            assert reloaded.cloud_restored_at is not None, (
                "Reload must preserve the cloud freshness boundary"
            )
            assert hass.states.get(transport_status).state == "waiting_for_cloud"
            cloud.stale = False
            await reloaded.async_refresh()
            assert reloaded.last_update_success and reloaded.cloud_restored_at is None
            assert len(device.writes) == writes, "Reload must not Start or change power"
            assert (
                hass.data["sems_wallbox"][entry.entry_id][
                    "coordinator"
                ].charge_mode_policy.desired_power
                == 4.2
            )
            assert calls == ["tcp", "cloud"] * 4, calls
            # Exercise the actual coordinator and endpoint lifecycle with accelerated
            # elapsed-policy windows only; all network peers remain loopback fakes.
            import time
            auto = reloaded.automatic_fallback
            auto.enabled = True
            auto.paused = False
            cloud.stale = True
            await reloaded.async_refresh()
            await auto.tick()
            assert not reloaded.local, "One stale response must not trigger takeover"
            await reloaded.async_refresh()
            await reloaded.async_refresh()
            auto.failed_since = time.monotonic() - 91
            before_auto_writes = list(device.writes)
            await auto.tick()
            assert reloaded.local and reloaded.last_update_success
            assert device.writes == before_auto_writes, "Takeover must not replay controls"
            assert reloaded.connection_intent.automatic_tcp
            assert await hass.config_entries.async_reload(entry.entry_id)
            reloaded = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            auto = reloaded.automatic_fallback
            assert not reloaded.local and auto.trial_deadline is not None
            assert auto.reason == "cloud_unavailable" or auto.reason == "startup_cloud_trial"
            assert auto.failures < 3, "No normal outage debounce should be required"
            auto.trial_deadline = time.monotonic() - 1
            await auto.tick()
            assert reloaded.local and reloaded.last_update_success
            assert device.writes == before_auto_writes
            auto.return_delay = auto.TRIAL_DELAY
            print("PASS: automatic TCP reload retries cloud with bounded verification, then fast TCP")
            auto.next_attempt = 0
            cloud.error = ConnectionError("Simulated cloud API outage")
            await auto.tick()
            assert reloaded.local and reloaded.last_update_success
            assert auto.reason == "cloud_preflight_unavailable"
            assert auto.trial_deadline is None and auto.next_attempt > time.monotonic()
            assert device.writes == before_auto_writes
            assert hass.states.get(transport_status).state == "tcp"
            cloud.error = None
            auto.next_attempt = 0
            await auto.tick()
            assert not reloaded.local and auto.trial_deadline is not None
            assert not reloaded.last_update_success
            auto.trial_deadline = time.monotonic() - 1
            await auto.tick()
            assert reloaded.local and auto.return_delay == 3600
            cloud.stale = False
            auto.next_attempt = 0
            await auto.tick()
            assert not reloaded.local and reloaded.last_update_success
            assert auto.trial_deadline is None and auto.return_delay == 1800
            assert device.writes == before_auto_writes
            assert reloaded.charge_mode_policy.desired_power == 4.2
            print("PASS: automatic stale-cloud takeover, failed return recovery, fresh return, no control replay")
            auto.enabled = False
            original_ids = {e.unique_id: e.entity_id for e in entities}
            async def validate_settings(values):
                return {k: v for k, v in values.items() if k != "native_discover"}

            with patch.object(config_flow, "SemsApi", return_value=cloud), patch.object(
                config_flow, "validate_native", side_effect=validate_settings
            ):
                # Current entry has no explicit proxy key; supply the normalized default.
                settings = dict(entry.data, native_ingress_peer="")
                for source, step in (("reconfigure", "reconfigure"), ("reauth", "reauth_confirm")):
                    flow = await hass.config_entries.flow.async_init(
                        "sems_wallbox", context={"source": source, "entry_id": entry.entry_id},
                        data=entry.data if source == "reauth" else None,
                    )
                    assert flow["step_id"] == step, flow
                    submitted = {"username": "updated-user", "password": "updated-password"}
                    if source == "reconfigure":
                        submitted.update({k: settings[k] for k in (
                            "native_host", "native_advertised_host", "native_port", "native_ingress_peer"
                        )})
                    result = await hass.config_entries.flow.async_configure(flow["flow_id"], submitted)
                    assert result["type"] == "abort", result
                    assert result["reason"] == source + "_successful", result
                    await hass.async_block_till_done()
                    assert entry.state is ConfigEntryState.LOADED, entry.state
                    assert entry.data["wallbox_serial_No"] == SERIAL
                    current_ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(
                        er.async_get(hass), entry.entry_id)}
                    assert current_ids == original_ids
                    assert hass.data["sems_wallbox"][entry.entry_id]["coordinator"].charge_mode_policy.desired_power == 4.2
                print("PASS: real HA reconfigure and reauth retain entities and saved power")
            # Exercise last-choice buffering through real HA services while the
            # endpoint handover is held open. Repeated clicks must not replay Start.
            owner = hass.data["sems_wallbox"][entry.entry_id]["coordinator"]
            handover_entered.clear()
            handover_release.clear()
            moving = asyncio.create_task(service("switch", "turn_on", connection))
            try:
                await asyncio.wait_for(handover_entered.wait(), 5)
                before_pending = len(device.writes)
                for _ in range(10):
                    await service("switch", "turn_on", charging)
                    await service("switch", "turn_off", charging)
                await service("number", "set_value", power, value=4.3)
                await service("number", "set_value", power, value=4.2)
                await service("select", "select_option", mode, option="pv_priority")
                await service("select", "select_option", mode, option="fast")
                assert len(owner.pending_intent.pending) == 3
                assert len(device.writes) == before_pending
            finally:
                handover_release.set()
                await moving
            await asyncio.wait_for(owner.pending_intent.task, 10)
            assert 7 not in device.writes[before_pending:]
            assert device.limit == 42 and device.mode == 0 and device.state == 0
            assert not owner.pending_intent.pending and owner.pending_intent.error is None
            await service("switch", "turn_off", connection)
            print("PASS: real HA handover accepts latest Start/Stop, power and mode without command replay")
            from ha_native_energy_checks import check_energy
            await check_energy(hass, entry, device, SERIAL, service, connection)
            assert await hass.config_entries.async_unload(entry.entry_id)
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "entities": len(entities),
                        "transitions": calls,
                        "checks": [
                            "manual_native_config",
                            "visible_handover_progress_both_directions",
                            "cloud_power_bounds_fall_back_to_enrolled_model",
                            "pv_start_stop_and_explicit_power_without_mode_change",
                            "entity_services",
                            "two_tcp_cloud_cycles",
                            "mode_reset_power_restore",
                            "repeated_start_stop",
                            "power_controls",
                            "immediate_11kw_request",
                            "active_measurement_polling_without_push",
                            "supervision_of_cloud_originated_session",
                            "protective_stop_real_ha_notification",
                            "reload_cloud_restore_no_start",
                            "reload_rejects_stale_cloud",
                        ],
                    }
                )
            )
    finally:
        await hass.async_stop(force=True)


if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-native-runtime-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        asyncio.run(run(folder))
