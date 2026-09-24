"""Real HA publication race and installed pymodbus lost-ACK replay; loopback only."""

import asyncio
import json
import socket
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

import custom_components.sems_wallbox as integration
from custom_components.sems_wallbox.native_coordinator import NativeCoordinator
from custom_components.sems_wallbox.native_entities import ValueSensor
from custom_components.sems_wallbox.native_protocol import NativeStatus
from custom_components.sems_wallbox.wallbox_modbus import WallboxModbusClient

SERIAL = "5011KHCA-AUDIT"


async def publication_race():
    with tempfile.TemporaryDirectory(prefix="astra-ha-audit-") as folder:
        hass = HomeAssistant(folder)
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="sems_wallbox",
            title="Audit",
            unique_id=SERIAL,
            source="user",
            discovery_keys={},
            subentries_data=[],
            options={},
            data={
                "wallbox_serial_No": SERIAL,
                "native_host": "192.0.2.10",
                "native_advertised_host": "192.0.2.20",
                "native_port": 18899,
            },
        )
        owner = NativeCoordinator(hass, entry)
        entered = asyncio.Event()
        report = asyncio.Event()
        restoring = asyncio.Event()
        finish = asyncio.Event()

        async def command(*args, **kwargs):
            entered.set()
            await report.wait()

        async def restore():
            restoring.set()
            await finish.wait()

        owner.transport = SimpleNamespace(
            epoch=1,
            available=True,
            observed_at=0,
            latest=NativeStatus(
                SERIAL, 0, 0, 4.2, 0, (0, 0, 0), (230, 230, 230), 0, 1, 0
            ),
            async_command=command,
            async_disconnect=AsyncMock(),
            session_guard=SimpleNamespace(limit=None, error=None, task=None),
        )
        owner.endpoint = SimpleNamespace(
            journal={"original": "fake"}, async_restore=restore
        )
        owner._mark_cloud_handover = AsyncMock()
        owner.local = True
        owner.cloud_settings.request_refresh = Mock()
        sensor = ValueSensor(owner, SERIAL + "_power", "power", "power", "kW", "power")
        polling = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(entered.wait(), timeout=10)
        moving = asyncio.create_task(owner._set_local(False))
        await asyncio.wait_for(restoring.wait(), timeout=10)
        report.set()
        await polling
        result = {
            "transitioning": owner.transitioning,
            "routing_epoch": owner.routing_epoch,
            "coordinator_success": owner.last_update_success,
            "power_sensor_available": sensor.available,
            "published_transport": (owner.data or {}).get(SERIAL, {}).get("transport"),
        }
        assert (
            result["transitioning"]
            and not result["coordinator_success"]
            and not result["power_sensor_available"]
        )
        assert (
            result["published_transport"] == "cloud"
        )  # Initial data retained but unavailable.
        finish.set()
        await moving
        await owner.cloud_settings.close()
        await super(NativeCoordinator, owner).async_shutdown()
        await hass.async_stop(force=True)
        return result




async def failed_setup(cancelled=False):
    with tempfile.TemporaryDirectory(prefix="astra-setup-") as folder:
        hass = HomeAssistant(folder)
        hass.data["sems_wallbox"] = {}
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="sems_wallbox",
            title="Audit",
            unique_id="5011KHCA-AUDIT",
            source="user",
            discovery_keys={},
            subentries_data=[],
            options={},
            data={
                "wallbox_serial_No": "5011KHCA-AUDIT",
                "native_enabled": True,
                "native_host": "192.0.2.10",
                "native_advertised_host": "192.0.2.20",
                "native_port": port,
            },
        )
        owner = NativeCoordinator(hass, entry)
        owner.connection_intent.manual_tcp = True
        owner.connection_intent.async_load = AsyncMock()
        owner.endpoint = SimpleNamespace(journal=None, async_load=AsyncMock())

        async def activate():
            owner.endpoint.journal = {"original": "fake"}
            raise (
                asyncio.CancelledError()
                if cancelled
                else ConnectionError("Simulated activation/readback failure")
            )

        owner.endpoint.async_activate = activate
        owner.endpoint.async_restore = AsyncMock(
            side_effect=ConnectionError("Simulated management unreachable")
        )
        owner._mark_cloud_handover = AsyncMock()
        with (
            patch(
                "custom_components.sems_wallbox.native_coordinator.NativeCoordinator",
                return_value=owner,
            ),
            patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("External HTTP forbidden"),
            ),
        ):
            try:
                await integration._async_setup_native(hass, entry)
            except (ConnectionError, asyncio.CancelledError) as exc:
                error = type(exc).__name__
            else:
                raise AssertionError("Expected setup failure missing")
        blocked = False
        try:
            server = await asyncio.start_server(lambda r, w: None, "0.0.0.0", port)
        except OSError:
            blocked = True
        else:
            server.close()
            await server.wait_closed()
        result = {
            "setup_error": error,
            "published_runtime": entry.entry_id in hass.data["sems_wallbox"],
            "listener_still_serving": bool(
                owner.transport._server and owner.transport._server.is_serving()
            ),
            "second_setup_bind_blocked": blocked,
            "restore_attempts": owner.endpoint.async_restore.await_count,
        }
        assert (
            not result["published_runtime"]
            and not result["listener_still_serving"]
            and not blocked
        ), result
        assert owner.endpoint.journal is not None
        print(
            "PASS: failed setup releases listener and retains journal; cancelled=",
            cancelled,
        )
        await owner.transport.async_close()
        await super(NativeCoordinator, owner).async_shutdown()
        await hass.async_stop(force=True)


async def modbus_wire_checks():
    """Observe actual pymodbus frames, identity fencing and lost acknowledgements."""
    writes = []
    writers = []
    identity = SERIAL

    async def peer(reader, writer):
        writers.append(writer)
        try:
            while True:
                head = await reader.readexactly(7)
                tid, pid, length, unit = struct.unpack(">HHHB", head)
                body = await reader.readexactly(length - 1)
                function, address, value = struct.unpack(">BHH", body)
                if function == 3:
                    assert address == 10040 and value == 8
                    payload = bytes([3, 16]) + identity.encode().ljust(16, b"\0")
                    writer.write(
                        struct.pack(">HHHB", tid, pid, len(payload) + 1, unit) + payload
                    )
                else:
                    assert function == 6
                    writes.append((address, value))
                    if value == 2:
                        continue  # Consume Start, deliberately lose its ACK.
                    writer.write(head + body)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # Per-operation clients close their connections normally.
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    client = WallboxModbusClient(
        "127.0.0.1", server.sockets[0].getsockname()[1], expected_serial=SERIAL
    )
    try:
        assert not await asyncio.to_thread(client.write_start_stop, True)
        assert writes == [(10060, 1), (10060, 2)], writes
        for other in ("OTHER-DEVICE", ""):
            identity = other
            assert not await asyncio.to_thread(client.write_start_stop, False)
            assert not await asyncio.to_thread(client.write_charge_mode, 1)
        assert writes == [(10060, 1), (10060, 2)], writes
        identity = SERIAL
        assert await asyncio.to_thread(client.write_start_stop, False)
        assert await asyncio.to_thread(client.write_breaker_current, 63)
        client.close()
        assert not await asyncio.to_thread(client.write_start_stop, True)
        assert writes == [(10060, 1), (10060, 2), (10060, 1), (10026, 63)], writes
        trace = client.diagnostics()
        assert trace["write_requests"] == len(writes)
        assert [(event["address"], event["value"]) for event in trace["recent_events"]
                if event["event"] == "write_request"] == writes
        assert trace["connection_attempts"] > 0 and trace["read_requests"] > 0
        assert SERIAL not in json.dumps(trace) and "127.0.0.1" not in json.dumps(trace)
        print(
            "PASS: one uncertain Start; wrong/missing identity and closed-client writes blocked"
        )
    finally:
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


async def modbus_fields_and_services():
    """Missing optional registers stay unknown and refused services raise in HA."""
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.sems_wallbox import number, select, switch
    from custom_components.sems_wallbox.modbus_coordinator import (
        ModbusUpdateCoordinator,
    )

    with tempfile.TemporaryDirectory(prefix="goodwe-modbus-contracts-") as folder:
        hass = HomeAssistant(folder)
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="sems_wallbox",
            title="Test",
            unique_id=SERIAL,
            source="user",
            discovery_keys={},
            subentries_data=[],
            options={},
            data={"wallbox_serial_No": SERIAL},
        )
        client = Mock()
        owner = ModbusUpdateCoordinator(hass, entry, client)
        owner.data = {SERIAL: {"sn": SERIAL, "chargeMode": 0, "set_charge_power": 4.2}}
        owner.charge_mode_policy = None
        for sn in ("OTHER-DEVICE", None):
            client.read_all.return_value = {"sn": sn}
            with pytest.raises(UpdateFailed, match="identity"):
                await owner._async_update_data()
        # Decode a genuinely absent optional block, not a hand-written entity state.
        decoder = WallboxModbusClient("127.0.0.1", expected_serial=SERIAL)
        blocks = {10000: [0] * 20, 10040: [0] * 20, 10060: [0] * 20}
        raw = SERIAL.encode().ljust(16, b"\0")
        blocks[10040][:8] = list(struct.unpack(">8H", raw))
        with patch.object(
            decoder, "_read", side_effect=lambda _, address, count: blocks.get(address)
        ):
            values = decoder._read_all_inner(None)
        owner.data = {SERIAL: values}
        owner.last_update_success = True
        for cls in (
            switch.ModbusMaintainMinPowerSwitch,
            switch.ModbusDynamicLoadMgmtSwitch,
            switch.ModbusPhaseSwitchSwitch,
        ):
            entity = cls(owner, SERIAL, client)
            assert entity.is_on is None, cls.__name__
        owner.data[SERIAL].update(
            chargeMode=0,
            set_charge_power=4.2,
            max_energy=0,
            min_energy=0,
            charge_target_soc=20,
            finish_time="0",
        )
        for method in (
            "write_start_stop",
            "write_charge_mode",
            "write_completion_time",
            "write_max_charge_power",
            "set_config_gen2",
            "set_charge_mode_gen2",
        ):
            getattr(client, method).return_value = False
        cases = [
            (switch.ModbusStartStopSwitch, "async_turn_off", None, "_pending_state"),
            (
                switch.SemsDynamicLoadSwitch,
                "async_turn_on",
                None,
                "_pending_state",
            ),
            (
                number.SemsCurrentLimitNumber,
                "async_set_native_value",
                8,
                "_pending_value",
            ),
            (
                number.SemsOutputPowerLimitNumber,
                "async_set_native_value",
                4.2,
                "_pending_value",
            ),
            (number.SemsMaxEnergyNumber, "async_set_native_value", 5, "_pending_value"),
            (
                number.ModbusMaxChargePowerNumber,
                "async_set_native_value",
                4.2,
                "_pending_value",
            ),
            (
                select.SemsChargeDurationSelect,
                "async_select_option",
                "2h",
                "_pending_value",
            ),
            (
                select.ModbusChargeModeSelect,
                "async_select_option",
                "fast",
                "_pending_mode",
            ),
            (
                select.ModbusChargeDurationSelect,
                "async_select_option",
                "2h",
                "_pending_value",
            ),
        ]
        current = SimpleNamespace(entity=None, action=None, argument=None)

        async def refused_write(call):
            method = getattr(current.entity, current.action)
            await (method() if current.argument is None else method(current.argument))

        hass.services.async_register("audit", "write", refused_write)
        for cls, action, argument, pending in cases:
            current.entity = cls(owner, SERIAL, client)
            current.entity.hass = hass
            current.entity.async_write_ha_state = Mock()
            current.action, current.argument = action, argument
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call("audit", "write", {}, blocking=True)
            assert getattr(current.entity, pending) is None, cls.__name__
        # An explicit Stop must still reach the client exactly once, even when
        # telemetry is contradictory and the switch already appears off.
        client.write_start_stop.reset_mock(return_value=True)
        client.write_start_stop.return_value = True
        owner.data[SERIAL].update(modbus_status_raw=3, modbus_car_connected=1, modbus_power=3.3)
        current.entity = switch.ModbusStartStopSwitch(owner, SERIAL, client)
        current.entity.hass = hass
        current.entity.async_write_ha_state = Mock()
        current.action, current.argument = "async_turn_off", None
        await hass.services.async_call("audit", "write", {}, blocking=True)
        client.write_start_stop.assert_called_once_with(False)
        owner._cancel_delayed_refresh()
        await owner.async_shutdown()
        await hass.async_stop(force=True)
        print(
            "PASS: Modbus identity, unknown optional data, refused and successful explicit HA Stop"
        )


async def modbus_polling_is_read_only():
    """Contradictory telemetry and read gaps never become unsolicited controls."""
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.sems_wallbox import modbus_coordinator as module

    with tempfile.TemporaryDirectory(prefix="goodwe-modbus-read-only-") as folder:
        hass = HomeAssistant(folder)
        entry = ConfigEntry(
            version=1, minor_version=1, domain="sems_wallbox", title="Read-only polling",
            unique_id=SERIAL, source="user", discovery_keys={}, subentries_data=[],
            options={}, data={"wallbox_serial_No": SERIAL},
        )
        # Keep HA's event-loop clock real; only advance the old coordinator timer.
        clock = SimpleNamespace(monotonic=lambda: 100.0)
        try:
            for power, car, gap in ((3.3, 1, False), (0.0, 1, False),
                                    (0.0, None, False), (0.0, 1, True)):
                report = {"sn": SERIAL, "modbus_status_raw": 3,
                          "modbus_power": power, "modbus_cp_state_name": "9V"}
                if car is not None:
                    report["modbus_car_connected"] = car
                client = Mock()
                client.read_all.return_value = report
                owner = module.ModbusUpdateCoordinator(hass, entry, client)
                try:
                    with patch.object(module, "time", clock, create=True):
                        clock.monotonic = lambda: 100.0
                        assert await owner._async_update_data() == {SERIAL: report}
                        if gap:
                            client.read_all.return_value = None
                            with pytest.raises(UpdateFailed):
                                await owner._async_update_data()
                            client.read_all.return_value = report
                        for now in (131.0, 200.0):
                            clock.monotonic = lambda: now
                            assert await owner._async_update_data() == {SERIAL: report}
                    # Include all client methods, not just Stop: polling is read-only.
                    assert all(call[0] == "read_all" for call in client.mock_calls), client.mock_calls
                finally:
                    owner._cancel_delayed_refresh()
                    await owner.async_shutdown()
        finally:
            await hass.async_stop(force=True)
    print("PASS: Modbus polling retains telemetry without writes across contradictions and read failures")


async def cancelled_cleanup():
    """Drain real socket cleanup despite cancellation at its internal await."""
    from tests.test_native_transport import Device, SERIAL as DEVICE_SERIAL, ready

    with tempfile.TemporaryDirectory(prefix="astra-cleanup-cancel-") as folder:
        hass = HomeAssistant(folder)
        entry = ConfigEntry(
            version=1, minor_version=1, domain="sems_wallbox", title="Audit",
            unique_id=DEVICE_SERIAL, source="user", discovery_keys={},
            subentries_data=[], options={}, data={
                "wallbox_serial_No": DEVICE_SERIAL,
                "native_host": "127.0.0.1",
                "native_advertised_host": "192.0.2.20", "native_port": 18899,
            },
        )
        owner = NativeCoordinator(hass, entry)
        await owner.transport.async_listen("127.0.0.1", 0)
        device = Device()
        await device.connect(owner.transport.port)
        peer = asyncio.create_task(device.run())
        try:
            await ready(owner.transport)
            owner.transport.session_guard.begin(4.2)
            deadline = owner.transport.session_guard._deadline_task
            await asyncio.sleep(0)
            cleanup = asyncio.create_task(owner.async_abort_setup())

            def cancel_cleanup(_):
                cleanup.cancel()
                asyncio.get_running_loop().call_soon(cleanup.cancel)

            deadline.add_done_callback(cancel_cleanup)
            await cleanup
            assert owner.transport._server is None
            assert owner.transport._writer is None or owner.transport._writer.is_closing()
            assert all(task.done() for task in owner.transport._tasks)
        finally:
            await owner.transport.async_close()
            await device.close()
            await peer
            await super(NativeCoordinator, owner).async_shutdown()
            await hass.async_stop(force=True)
        print("PASS: repeated cleanup cancellation leaves no peer or reader task")


async def main():
    with patch(
        "requests.sessions.Session.request",
        side_effect=AssertionError("External HTTP forbidden"),
    ):
        await publication_race()
        print("PASS: old TCP report cannot republish available data during handover")
        await failed_setup()
        await failed_setup(cancelled=True)
        await cancelled_cleanup()
        await modbus_wire_checks()
        await modbus_fields_and_services()
        await modbus_polling_is_read_only()


if __name__ == "__main__":
    asyncio.run(main())
