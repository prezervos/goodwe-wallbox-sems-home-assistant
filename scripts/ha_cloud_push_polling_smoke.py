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


async def check_http_cadence(folder):
    """Count real client HTTP calls at the network boundary; never contact a device."""
    import json
    import time
    from types import SimpleNamespace
    from unittest.mock import patch
    from urllib.parse import urlsplit

    import requests
    from homeassistant.config_entries import ConfigEntry

    from custom_components.sems_wallbox.cloud_push import CloudPush
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator
    from custom_components.sems_wallbox.sems_api import SemsApi

    hass = HomeAssistant(folder)
    hass.config.time_zone = "UTC"
    serial = "5011KHCA-HTTP-TEST"
    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="HTTP cadence",
        unique_id=serial, source="user", discovery_keys={}, subentries_data=[],
        options={"scan_interval": 60, "scan_interval_charging": 30},
        data={"wallbox_serial_No": serial, "native_host": "127.0.0.1",
              "native_advertised_host": "192.0.2.20", "native_port": 18899},
    )
    api = SemsApi(hass, "test", "test")
    # Authentication has separate retry tests. This measures steady-state reads.
    api._web_token = {"uid": "test", "token": "test"}
    api._observation_reader._token = {"token": "test"}
    api.configure_gen2("test-plant", "GW11K-HCA")
    owner = NativeCoordinator(hass, entry, api)
    push = CloudPush(owner, serial, api.fetch_mqtt_settings)
    owner.cloud_push = push
    push.connected = True
    state = SimpleNamespace(charging=False)
    calls = []
    telemetry_path = "/api/v3/EvCharger/GetCurrentChargeinfo"
    detail_path = "/web/sems/sems-remote/api/ev-charger/detail"

    def request(session, method, url, **kwargs):
        path = urlsplit(url).path
        assert method.upper() == "POST" and path in (telemetry_path, detail_path), (
            "Unexpected HTTP operation", method, path
        )
        calls.append((path, time.monotonic()))
        data = {
            "sn": serial, "lastUpdate": datetime.now(timezone.utc).isoformat(),
            "status": "charging" if state.charging else "standby",
            "power": 4.2 if state.charging else 0, "set_charge_power": 4.2,
            "chargeMode": 0, "chargePowerSetted": 4.2,
        }
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = json.dumps({
            "code": 0 if path == telemetry_path else "00000", "data": data,
        }).encode()
        return response

    def count(path):
        return sum(p == path for p, _ in calls)

    async def wait_reads(target, timeout):
        async with asyncio.timeout(timeout):
            while count(telemetry_path) < target:
                await asyncio.sleep(0.05)
            await hass.async_block_till_done()

    def event(tid):
        return push.event(push.topics[1], json.dumps({"sn": serial, "tid": tid}).encode(),
                          epoch=owner.routing_epoch)

    unsubscribe = owner.async_add_listener(lambda: None)
    try:
        with patch("requests.sessions.Session.request", request):
            await owner.async_refresh()
            await hass.async_block_till_done()
            assert count(telemetry_path) == 1
            assert owner.update_interval == timedelta(seconds=60)
            configuration_reads = count(detail_path)
            await asyncio.sleep(58)
            assert count(telemetry_path) == 1, "Idle poll ran before its configured interval"
            await wait_reads(2, 7)
            idle_gap = [t for p, t in calls if p == telemetry_path][-1] - [t for p, t in calls if p == telemetry_path][0]
            assert 59 <= idle_gap <= 65, idle_gap
            assert count(detail_path) == configuration_reads

            # Simulate a reported state change, never a Start service or setter.
            state.charging = True
            assert event("charging")
            await push._refresh_task
            assert owner.update_interval == timedelta(seconds=30)
            before = count(telemetry_path)
            charging_at = [t for p, t in calls if p == telemetry_path][-1]
            await asyncio.sleep(28)
            assert count(telemetry_path) == before
            await wait_reads(before + 1, 7)
            charging_gap = [t for p, t in calls if p == telemetry_path][-1] - charging_at
            assert 29 <= charging_gap <= 35, charging_gap

            before = count(telemetry_path)
            refreshes = push.refresh_count
            for index in range(20):
                assert event(f"burst-{index}")
            assert not event("burst-19"), "Repeated event identity was not deduplicated"
            await push._refresh_task
            assert count(telemetry_path) == before + 1, "MQTT burst amplified HTTP reads"
            assert push.refresh_count == refreshes + 1
            assert count(detail_path) == configuration_reads

            # Optional push loss must leave the normal HTTP scheduler running.
            push.connected = False
            before = count(telemetry_path)
            await wait_reads(before + 1, 35)
            assert owner.last_update_success and not owner.local
            assert owner.update_interval == timedelta(seconds=30)
            assert api.login_attempts == 0
            assert count(detail_path) == configuration_reads
            print(json.dumps({
                "result": "PASS", "case": "HTTP request cadence",
                "idle_gap_seconds": round(idle_gap, 2),
                "charging_gap_seconds": round(charging_gap, 2),
                "telemetry_requests": count(telemetry_path),
                "configuration_requests": count(detail_path),
                "burst_events": 20, "burst_http_refreshes": 1,
                "physical_commands": 0,
            }))
    finally:
        unsubscribe()
        await owner.async_shutdown()
        await hass.async_stop(force=True)


async def check_late_cloud_response(folder):
    """Reject a delayed executor response after newer TCP data was published."""
    import threading
    from types import SimpleNamespace
    from homeassistant.config_entries import ConfigEntry
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator

    hass = HomeAssistant(folder)
    serial = "5011KHCA-RACE-TEST"
    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Delayed cloud",
        unique_id=serial, source="user", discovery_keys={}, subentries_data=[],
        options={}, data={"wallbox_serial_No": serial, "native_host": "192.0.2.10",
                          "native_advertised_host": "192.0.2.20", "native_port": 18899},
    )
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def delayed_read(sn):
        entered.set()
        try:
            assert release.wait(5), "Test did not release the blocked cloud read"
            return {"sn": sn, "power": 9.9, "status": "charging", "chargeMode": 0}
        finally:
            exited.set()

    cloud = SimpleNamespace(supports_timestamped_observation=True,
                            fetch_status_observation=delayed_read)
    owner = NativeCoordinator(hass, entry, cloud)
    # Configuration epoch races have their own tests; isolate delayed telemetry.
    owner.cloud_settings.request_refresh = lambda: None
    task = asyncio.create_task(owner.async_refresh())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # Model a completed handover publishing a newer transport observation.
        owner.routing_epoch += 1
        owner.local = True
        native = {serial: {"sn": serial, "power": 1.2, "transport": "tcp"}}
        owner.async_set_updated_data(native)
        release.set()
        await asyncio.wait_for(task, 3)
        assert exited.is_set()
        assert owner.data == native, "Late HTTP response overwrote newer TCP data"
        assert not owner.last_update_success  # Stale refresh must not claim success.
        owner.async_set_updated_data(native)
        assert owner.last_update_success  # A current TCP observation remains usable.
        print("PASS: delayed cloud executor response cannot overwrite newer TCP state")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await owner.cloud_settings.close()
        await super(NativeCoordinator, owner).async_shutdown()
        await hass.async_stop(force=True)



async def check_rate_limit_recovery(folder, *, automatic):
    """Recover real HA polling/MQTT through a gated HTTP outage and loopback TCP.

    Only HTTP responses, MQTT broker I/O and physical endpoint configuration are
    simulated. Retry-After uses real elapsed time; supervisor delays are shortened
    explicitly. No Start/Stop or power/mode command is permitted in either path.
    """
    import json
    import time
    from types import SimpleNamespace
    from unittest.mock import patch
    from urllib.parse import urlsplit

    import requests
    from homeassistant.config_entries import ConfigEntry
    from custom_components.sems_wallbox import cloud_push as push_module
    from custom_components.sems_wallbox.native_coordinator import NativeCoordinator
    from custom_components.sems_wallbox.sems_api import SemsApi
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.test_native_transport import Device, SERIAL

    hass = HomeAssistant(folder)
    hass.config.time_zone = "UTC"
    entry = ConfigEntry(
        version=1, minor_version=1, domain="sems_wallbox", title="Cooldown recovery",
        unique_id=SERIAL, source="user", discovery_keys={}, subentries_data=[],
        options={"scan_interval": 1, "native_auto_fallback": automatic},
        data={"wallbox_serial_No": SERIAL, "native_host": "127.0.0.1",
              "native_advertised_host": "192.0.2.20", "native_port": 18899},
    )
    api = SemsApi(hass, "fixture", "fixture")
    api._web_token = {"uid": "fixture", "token": "fixture"}
    api._observation_reader._token = {"token": "fixture"}
    api.configure_gen2("fixture-plant", "GW11K-HCA")
    owner = NativeCoordinator(hass, entry, api)
    push = push_module.CloudPush(owner, SERIAL, api.fetch_mqtt_settings)
    owner.cloud_push = push
    # Exercise real scheduling without waiting production 15s/30min intervals.
    push.CHECK_INTERVAL = 0.01
    push.RETRY_MIN = push.RETRY_MAX = 0.1
    push.DEBOUNCE = push.MIN_REFRESH_INTERVAL = 0.01
    policy = owner.automatic_fallback
    policy.FAILURE_DELAY = 0.01
    policy.return_delay = policy.TRIAL_DELAY = 0.01
    policy.RETRY_DELAY = 0.01
    state = SimpleNamespace(throttle=False, limited_at=None, stale=False,
                            old_stamp=datetime.now(timezone.utc).isoformat())
    calls, transitions, clients = [], [], []
    disconnect = asyncio.Event()
    hints = asyncio.Queue()
    device = Device()

    async def accept_probe(reader, writer):
        writer.close()
        await writer.wait_closed()

    cloud_server = await asyncio.start_server(accept_probe, "127.0.0.1", 0)
    cloud_port = cloud_server.sockets[0].getsockname()[1]
    await owner.transport.async_listen("127.0.0.1", 0)

    class Endpoint:
        journal = None
        task = None

        async def async_activate(self):
            transitions.append("tcp")
            self.journal = {"original": f"TCP,Client,{cloud_port},127.0.0.1"}
            await device.connect(owner.transport.port)
            self.task = asyncio.create_task(device.run())

        async def async_restore(self):
            if self.journal:
                transitions.append("cloud")
                await device.close()
                await self.task
                self.journal = None

    owner.endpoint = Endpoint()

    class Broker:
        def __init__(self, **kwargs):
            self.index = len(clients)
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def subscribe(self, topics):
            assert topics == [(topic, 0) for topic in push.topics]

        @property
        def messages(self):
            return self.receive()

        async def receive(self):
            if self.index == 0:
                await disconnect.wait()
                raise push_module.aiomqtt.MqttError("simulated broker loss")
            while True:
                tid = await hints.get()
                yield SimpleNamespace(topic=push.topics[1], retain=False,
                                      payload=json.dumps({"sn": SERIAL, "tid": tid}).encode())

    def request(session, method, url, **kwargs):
        path = urlsplit(url).path
        kind = ("telemetry" if path.endswith("GetCurrentChargeinfo") else
                "detail" if path.endswith("ev-charger/detail") else
                "mqtt" if path.endswith("second-data/config") else None)
        assert kind is not None, ("Unexpected HTTP request", method, path)
        assert method.upper() == ("GET" if kind == "mqtt" else "POST")
        calls.append((kind, time.monotonic()))
        response = requests.Response()
        response.url = url
        if state.throttle:
            state.throttle = False
            state.limited_at = time.monotonic()
            response.status_code = 429
            response.headers["Retry-After"] = "3"
            response._content = b'{}'
            return response
        response.status_code = 200
        if kind == "mqtt":
            data = {"brokerUrl": "wss://fixture.goodwe-power.com/mqtt",
                    "clientId": "fixture", "userName": "fixture", "password": "fixture"}
        else:
            data = {"sn": SERIAL, "lastUpdate": state.old_stamp if state.stale else
                    datetime.now(timezone.utc).isoformat(), "status": "standby",
                    "power": 0, "set_charge_power": 4.2, "chargeMode": 0,
                    "chargePowerSetted": 4.2}
        response._content = json.dumps({"code": 0 if kind == "telemetry" else "00000",
                                        "data": data}).encode()
        return response

    async def wait_until(predicate, timeout=7):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    def count(kind):
        return sum(key == kind for key, _ in calls)

    unsubscribe = owner.async_add_listener(lambda: None)
    try:
        with patch("requests.sessions.Session.request", request), patch.object(
            push_module.aiomqtt, "Client", Broker
        ):
            await owner.async_refresh()
            push.start()
            await wait_until(lambda: push.connected)
            await hass.async_block_till_done()
            assert owner.last_update_success and not owner.local
            assert push.subscription_count == 1
            state.throttle = True
            await owner.async_refresh()
            assert not owner.last_update_success
            assert state.limited_at is not None
            limited_count = len(calls)
            disconnect.set()
            await wait_until(lambda: not push.connected)
            # Complete three failing polls; all after the first stay off the wire.
            await asyncio.sleep(0.02)
            await owner.async_refresh()
            await owner.async_refresh()
            assert len(calls) == limited_count
            if automatic:
                await policy.tick()
                assert owner.local and owner.last_update_success
                assert transitions == ["tcp"]
                await asyncio.sleep(0.02)
                await policy.tick()  # Reachable socket does not bypass the HTTP gate.
                assert owner.local and transitions == ["tcp"]
                assert policy.reason == "cloud_preflight_unavailable"
            await asyncio.sleep(max(0, state.limited_at + 2.8 - time.monotonic()))
            assert len(calls) == limited_count, "HTTP escaped the cooldown"
            assert not push.connected and push.subscription_count == 1
            if automatic:
                await asyncio.sleep(max(0, state.limited_at + 3.1 - time.monotonic()))
                state.stale = True
                await policy.tick()
                assert transitions == ["tcp", "cloud"]
                assert not owner.local and not owner.last_update_success
                assert owner.cloud_restored_at is not None
                assert not push._eligible(), "MQTT enabled before fresh handover data"
                state.stale = False
                await policy.tick()
            # Without fallback, recovery must come from the actual HA scheduler.
            await wait_until(lambda: owner.last_update_success and push.connected)
            assert not owner.local and owner.cloud_restored_at is None
            assert policy.trial_deadline is None and policy.failures == 0
            assert push.subscription_count == 2
            assert owner.update_interval == timedelta(seconds=1)
            resumed = calls[limited_count:]
            assert resumed and min(t for _, t in resumed) >= state.limited_at + 3
            before = count("telemetry")
            await wait_until(lambda: count("telemetry") > before)
            before = push.refresh_count
            hints.put_nowait("post-cooldown")
            await wait_until(lambda: push.refresh_count > before)
            await push._refresh_task
            assert owner.last_update_success
            assert push.event_counts["charging"] == 1
            assert api.login_attempts == 0 and device.writes == []
            print(json.dumps({"result": "PASS", "case": "cooldown recovery",
                              "automatic_tcp": automatic, "transitions": transitions,
                              "first_resumed_http_seconds": round(resumed[0][1] - state.limited_at, 3),
                              "mqtt_subscriptions": push.subscription_count,
                              "post_recovery_hint_refresh": True, "device_writes": 0}))
    finally:
        unsubscribe()
        await owner.async_shutdown()
        api.close()
        cloud_server.close()
        await cloud_server.wait_closed()
        await hass.async_stop(force=True)
    assert all(client.closed for client in clients)

if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="goodwe-cloud-push-") as folder:
        (Path(folder) / "custom_components").symlink_to(source)
        sys.path.insert(0, folder)
        if "--cooldown-only" in sys.argv:
            asyncio.run(check_rate_limit_recovery(folder, automatic=False))
            asyncio.run(check_rate_limit_recovery(folder, automatic=True))
        elif "--races-only" not in sys.argv:
            asyncio.run(run(folder))
            asyncio.run(check_http_cadence(folder))
        if "--cooldown-only" not in sys.argv:
            asyncio.run(check_late_cloud_response(folder))
