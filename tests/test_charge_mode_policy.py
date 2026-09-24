"""Test saved intent, fresh readback and cancellation without physical devices."""

import asyncio
import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

PACKAGE = "sems_mode_policy_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "sems_wallbox")
]
sys.modules[PACKAGE] = package
policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
adapter_module = importlib.import_module(PACKAGE + ".charge_mode_adapter")
Policy = policy_module.ChargeModePolicy
Observation = policy_module.ModeObservation
Error = policy_module.ModeVerificationError


class Store:
    def __init__(self, saved=None):
        self.saved = saved

    async def async_load(self):
        return self.saved

    async def async_save(self, data):
        self.saved = dict(data)


class Adapter:
    def __init__(self, *observations):
        self.observations = list(observations)
        self.calls = []
        self.write_result = True
        self.start_result = True

    async def read(self):
        self.calls.append("read")
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]

    async def write_mode(self, mode, before):
        self.calls.append(("mode", mode, before.power))
        return self.write_result

    async def start(self):
        self.calls.append("start")
        return self.start_result

    async def stop(self):
        self.calls.append("stop")
        return True


def make_policy(adapter, store=None, **kwargs):
    kwargs.setdefault("initial_mode", 0)
    return Policy(
        adapter, store or Store(), enabled=True, timeout=0.01, interval=0, **kwargs
    )


async def test_reload_restores_manual_pv_without_any_device_io():
    store = Store({"mode": 1})
    adapter = Adapter(Observation(0))
    policy = make_policy(adapter, store)
    await policy.async_load()
    assert policy.desired_mode == 1
    assert adapter.calls == []
    assert store.saved == {"mode": 1}


async def test_power_reset_pv_cannot_overwrite_saved_fast_and_start_follows_readback():
    adapter = Adapter(Observation(1, power=4.2), Observation(0, power=4.2))
    policy = make_policy(adapter, Store({"mode": 0}))
    await policy.async_load()
    await policy.async_start()
    assert adapter.calls == ["read", ("mode", 0, 4.2), "read", "start"]
    assert policy.desired_mode == 0


async def test_matching_local_mode_needs_no_write():
    adapter = Adapter(Observation(0))
    await make_policy(adapter).async_start()
    assert adapter.calls == ["read", "start"]


async def test_manual_selection_is_durable_and_survives_new_instance():
    store = Store()
    adapter = Adapter(Observation(0), Observation(2))
    await make_policy(adapter, store).async_select_mode(2)
    restored = make_policy(adapter, store)
    await restored.async_load()
    assert restored.desired_mode == 2
    assert store.saved == {"mode": 2}
    assert "start" not in adapter.calls


async def test_failed_confirmation_retains_intent_but_never_starts():
    adapter = Adapter(Observation(1))
    policy = make_policy(adapter)
    with pytest.raises(Error, match="fresh confirmation"):
        await policy.async_select_mode(0)
    assert policy.store.saved == {"mode": 0}
    assert "start" not in adapter.calls


async def test_api_ack_and_stale_cloud_mode_do_not_confirm():
    adapter = Adapter(
        Observation(1, report_marker="test:001", requires_advance=True),
        Observation(0, report_marker="test:001", requires_advance=True),
    )
    with pytest.raises(Error, match="fresh confirmation"):
        await make_policy(adapter).async_start()
    assert "start" not in adapter.calls


async def test_cloud_matching_mode_still_requires_new_report():
    adapter = Adapter(
        Observation(0, report_marker="test:001", requires_advance=True),
        Observation(0, report_marker="test:002", requires_advance=True),
    )
    await make_policy(adapter).async_start()
    assert adapter.calls == ["read", "read", "start"]


async def test_cloud_new_report_with_wrong_mode_does_not_confirm():
    adapter = Adapter(
        Observation(1, report_marker="test:001", requires_advance=True),
        Observation(1, report_marker="test:002", requires_advance=True),
    )
    with pytest.raises(Error):
        await make_policy(adapter).async_start()
    assert "start" not in adapter.calls


async def test_rejected_write_does_not_start():
    adapter = Adapter(Observation(1))
    adapter.write_result = False
    with pytest.raises(Error, match="rejected"):
        await make_policy(adapter).async_start()
    assert adapter.calls == ["read", ("mode", 0, None)]


async def test_uncertain_start_is_not_replayed():
    adapter = Adapter(Observation(0))
    adapter.start_result = False
    with pytest.raises(Error, match="not retried"):
        await make_policy(adapter).async_start()
    assert adapter.calls.count("start") == 1


@pytest.mark.parametrize("mode", [0, 1])
async def test_active_device_blocks_start_with_specific_ui_error(mode):
    adapter = Adapter(Observation(mode, active=True))
    with pytest.raises(Error, match="stop state not confirmed") as caught:
        await make_policy(adapter).async_start()
    errors = importlib.import_module(PACKAGE + ".ui_errors")
    assert errors.operation_error(caught.value).translation_key == "start_requires_idle"
    assert adapter.calls == ["read"]


async def test_explicit_select_can_change_mode_while_active():
    adapter = Adapter(Observation(1, active=True), Observation(0, active=True))
    await make_policy(adapter).async_select_mode(0)
    assert "start" not in adapter.calls


@pytest.mark.parametrize("action", ["stop", "select", "close", "setting"])
async def test_newer_action_fences_start_after_inflight_read(action):
    adapter = Adapter(Observation(0), Observation(1))
    policy = make_policy(adapter)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_read = adapter.read

    async def blocked_read():
        entered.set()
        await release.wait()
        return await original_read()

    adapter.read = blocked_read
    pending = asyncio.create_task(policy.async_start())
    await entered.wait()
    if action == "setting":
        policy.invalidate()
        newer = None
    else:
        callback = {
            "stop": policy.async_stop,
            "select": lambda: policy.async_select_mode(1),
            "close": policy.async_close,
        }[action]
        newer = asyncio.create_task(callback())
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(Error, match="superseded"):
        await pending
    if newer:
        await newer
    assert "start" not in adapter.calls
    if action == "stop":
        assert adapter.calls[-1] == "stop"


async def test_invalid_saved_mode_fails_without_device_access():
    adapter = Adapter(Observation(0))
    with pytest.raises(Error, match="Stored"):
        await make_policy(adapter, Store({"mode": 9})).async_load()
    assert adapter.calls == []


@pytest.mark.parametrize("mode", [-1, 3, True, "0"])
async def test_invalid_mode_rejected(mode):
    adapter = Adapter(Observation(0))
    with pytest.raises(ValueError):
        await make_policy(adapter).async_select_mode(mode)
    assert adapter.calls == []


def transport(data, *, modbus=False):
    async def execute(func, *args):
        return func(*args)

    hass = types.SimpleNamespace(async_add_executor_job=execute)
    client = MagicMock()
    client.get_data_gen2.return_value = data
    client.read_all.return_value = data
    client.set_charge_mode_gen2.return_value = True
    return adapter_module.ModeTransportAdapter(
        hass, "SN", client, modbus=modbus
    ), client


@pytest.mark.parametrize(
    "change",
    [
        {"sn": "OTHER"},
        {"chargeMode": None},
        {"_reported_charge_mode": None},
        {"status": "offline"},
        {"lastUpdate": ""},
        {"lastUpdate": "invalid"},
    ],
)
async def test_cloud_missing_identity_mode_or_freshness_rejected(change):
    data = dict(
        sn="SN", chargeMode=0, lastUpdate="2026-09-17T10:00:00Z", status="available"
    )
    data.update(change)
    adapter, _ = transport(data)
    with pytest.raises(Error):
        await adapter.read()


async def test_cloud_fast_preserves_existing_power_and_pv_omits_power():
    adapter, client = transport({})
    await adapter.write_mode(
        0, Observation(1, power=4.2, minimum_power=4.2, maximum_power=11)
    )
    client.set_charge_mode_gen2.assert_called_with("SN", 0, 4.2)
    await adapter.write_mode(1, Observation(0, power=4.2))
    client.set_charge_mode_gen2.assert_called_with("SN", 1, None)


@pytest.mark.parametrize("power", [None, -1, 0, float("nan"), float("inf"), 12])
async def test_fast_does_not_invent_or_raise_a_power_limit(power):
    adapter, client = transport({})
    with pytest.raises(Error):
        await adapter.write_mode(
            0, Observation(1, power=power, minimum_power=4.2, maximum_power=11)
        )
    client.set_charge_mode_gen2.assert_not_called()


async def test_disabled_policy_keeps_existing_path():
    coordinator = types.SimpleNamespace(
        charge_mode_policy=Policy(Adapter(Observation(0)), Store())
    )
    assert await policy_module.async_apply_policy(coordinator, "start") is False
    assert coordinator.charge_mode_policy.adapter.calls == []


async def test_cloud_last_charge_blocks_start_even_when_detail_says_idle():
    adapter, client = transport(
        dict(
            sn="SN",
            chargeMode=1,
            lastUpdate="2026-09-17T10:00:00Z",
            status="available",
            startStatus=False,
        )
    )
    client.fetch_last_charge.return_value = {"last_charge_work_status": 6}
    observation = await adapter.read()
    assert observation.active


async def test_real_cloud_switch_dispatches_through_guard_without_optimistic_on():
    from tests.test_switch import _make_switch, STANDBY_DATA

    entity = _make_switch(STANDBY_DATA)
    adapter = Adapter(Observation(1, power=4.2), Observation(0, power=4.2))
    entity.coordinator.charge_mode_policy = make_policy(adapter)
    entity.async_write_ha_state = MagicMock()
    await entity.async_turn_on()
    assert adapter.calls[-1] == "start"
    entity.api.change_status_gen2.assert_not_called()
    entity.async_write_ha_state.assert_not_called()


async def test_real_cloud_switch_failure_cannot_fall_through_to_legacy_start():
    from tests.test_switch import _make_switch, STANDBY_DATA
    from homeassistant.exceptions import HomeAssistantError

    entity = _make_switch(STANDBY_DATA)
    adapter = Adapter(Observation(1))
    # Existing tests load entities under independent package namespaces.
    entity_policy_class = sys.modules[
        "sems_wallbox_pkg_switch.charge_mode_policy"
    ].ChargeModePolicy
    entity.coordinator.charge_mode_policy = entity_policy_class(
        adapter, Store(), enabled=True, initial_mode=0, timeout=0.01, interval=0
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    entity.api.change_status_gen2.assert_not_called()
    assert "start" not in adapter.calls


async def test_real_cloud_select_uses_policy_and_persists_choice():
    from tests.test_select import _make_entity

    entity = _make_entity()
    adapter = Adapter(Observation(0), Observation(1))
    policy = make_policy(adapter)
    entity.coordinator.charge_mode_policy = policy
    entity.coordinator.async_request_refresh = AsyncMock()
    await entity.async_select_option("pv_priority")
    assert policy.store.saved == {"mode": 1}
    entity.api.set_charge_mode_gen2.assert_not_called()


async def test_real_cloud_stop_uses_policy():
    from tests.test_switch import _make_switch, STANDBY_DATA

    entity = _make_switch(STANDBY_DATA)
    adapter = Adapter(Observation(0))
    entity.coordinator.charge_mode_policy = make_policy(adapter)
    await entity.async_turn_off()
    assert adapter.calls == ["stop"]
    entity.api.change_status_gen2.assert_not_called()


async def test_real_modbus_switch_uses_same_guard():
    from tests.test_switch import _switch_mod

    coordinator = types.SimpleNamespace(
        charge_mode_policy=make_policy(Adapter(Observation(0))),
        schedule_delayed_refresh=MagicMock(),
    )
    entity = _switch_mod.ModbusStartStopSwitch(coordinator, "SN", MagicMock())
    await entity.async_turn_on()
    assert coordinator.charge_mode_policy.adapter.calls == ["read", "start"]
    await entity.async_turn_off()
    assert coordinator.charge_mode_policy.adapter.calls[-1] == "stop"


async def test_real_modbus_select_uses_same_persistence():
    from tests.test_select import _select_mod

    coordinator = types.SimpleNamespace(
        data={"SN": {"chargeMode": 0}},
        charge_mode_policy=make_policy(Adapter(Observation(0), Observation(2))),
        schedule_delayed_refresh=MagicMock(),
        async_request_refresh=AsyncMock(),
    )
    entity = _select_mod.ModbusChargeModeSelect(coordinator, "SN", MagicMock())
    await entity.async_select_option("pv_and_battery")
    assert coordinator.charge_mode_policy.store.saved == {"mode": 2}


async def test_storage_failure_does_not_apply_mode_or_start():
    store = Store()
    store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    adapter = Adapter(Observation(1))
    with pytest.raises(OSError):
        await make_policy(adapter, store).async_select_mode(0)
    assert adapter.calls == []


async def test_superseded_selection_during_save_cannot_send_stale_mode():
    store = Store()
    entered = asyncio.Event()
    release = asyncio.Event()
    save = store.async_save

    async def blocked_save(data):
        entered.set()
        await release.wait()
        await save(data)

    store.async_save = blocked_save
    adapter = Adapter(Observation(2))
    policy = make_policy(adapter, store)
    older = asyncio.create_task(policy.async_select_mode(1))
    await entered.wait()
    newer = asyncio.create_task(policy.async_select_mode(2))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(Error, match="superseded"):
        await older
    await newer
    assert store.saved == {"mode": 2}
    assert adapter.calls == ["read"]


async def test_modbus_adapter_reads_device_instead_of_entity_cache():
    adapter, client = transport(
        dict(sn="SN", chargeMode=2, set_charge_power=4.2, modbus_status_raw=1),
        modbus=True,
    )
    observation = await adapter.read()
    assert observation.mode == 2
    assert not observation.requires_advance
    client.read_all.assert_called_once()
    client.get_data_gen2.assert_not_called()
    await adapter.write_mode(0, observation)
    client.write_charge_mode.assert_called_once_with(0)


async def test_unexpected_power_change_after_mode_write_blocks_start():
    adapter = Adapter(Observation(1, power=4.2), Observation(0, power=11))
    with pytest.raises(Error):
        await make_policy(adapter).async_start()
    assert "start" not in adapter.calls


def test_timestamp_normalization_does_not_treat_timezone_change_as_new_data():
    marker = adapter_module._marker
    assert marker("2026-09-17T10:00:00Z") == marker("2026-09-17T12:00:00+02:00")
    assert marker(1700000000) == marker(1700000000000) == marker("1700000000000")
    assert marker("2026-09-17 10:00:00").startswith("local:")
    for invalid in [True, float("nan"), -1, float("inf"), "bad"]:
        assert marker(invalid) is None


async def test_incompatible_timestamp_representations_cannot_confirm():
    adapter = Adapter(
        Observation(
            0, report_marker="local:2026-09-17T10:00:00", requires_advance=True
        ),
        Observation(0, report_marker="utc:2026-09-17T10:00:01", requires_advance=True),
    )
    with pytest.raises(Error):
        await make_policy(adapter).async_start()
    assert "start" not in adapter.calls


async def test_start_waits_for_earlier_power_write_to_complete():
    adapter = Adapter(Observation(0))
    policy = make_policy(adapter)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def write_setting():
        entered.set()
        await release.wait()
        adapter.calls.append("power_written")

    setting = asyncio.create_task(
        policy.async_setting_write(write_setting, desired_mode=0)
    )
    await entered.wait()
    start = asyncio.create_task(policy.async_start())
    await asyncio.sleep(0)
    assert adapter.calls == []
    release.set()
    await setting
    await start
    assert adapter.calls == ["power_written", "read", "start"]


async def test_duplicate_start_is_coalesced_without_replay():
    adapter = Adapter(Observation(0))
    policy = make_policy(adapter)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_read = adapter.read

    async def read():
        entered.set()
        await release.wait()
        return await original_read()

    adapter.read = read
    pending = asyncio.create_task(policy.async_start())
    await entered.wait()
    await policy.async_start()
    release.set()
    await pending
    assert adapter.calls.count("start") == 1


@pytest.mark.parametrize("preferred, expected", [(0, "fast"), (None, None)])
async def test_select_exposes_saved_intent_separately_from_actual_mode(preferred, expected):
    from tests.test_select import _make_entity

    entity = _make_entity(chargeMode=1)
    entity.coordinator.charge_mode_policy = make_policy(
        Adapter(Observation(1)), initial_mode=preferred
    )
    assert entity.extra_state_attributes == {"preferred_mode": expected}
    assert entity._attr_current_option == "pv_priority"


@pytest.mark.parametrize("outcome", ["applied", "rejected", "ack_only", "stale_report"])
@pytest.mark.parametrize("reload_policy", [False, True])
async def test_external_pv_reset_through_real_switch_and_transport(
    outcome, reload_policy
):
    """Inject a device-side reset without using HA select or changing saved intent."""
    from tests.test_switch import _make_switch, STANDBY_DATA, SAMPLE_SN
    from homeassistant.exceptions import HomeAssistantError

    entity = _make_switch(STANDBY_DATA)
    runtime_policy = sys.modules["sems_wallbox_pkg_switch.charge_mode_policy"]
    runtime_adapter = importlib.import_module(
        "sems_wallbox_pkg_switch.charge_mode_adapter"
    )

    class Gateway:
        def __init__(self):
            self.mode = 0
            self.power = 4.2
            self.marker = 1800000000
            self.written = False
            self.calls = []

        def force_external_pv(self):
            self.mode = 1
            self.calls.append(("external_reset", self.mode))

        def get_data_gen2(self, serial):
            assert serial == SAMPLE_SN
            if self.written and outcome != "stale_report":
                self.marker += 1
            self.calls.append(("read", self.mode, self.marker))
            return dict(
                sn=serial,
                chargeMode=self.mode,
                _reported_charge_mode=self.mode,
                set_charge_power=self.power,
                min_charge_power=4.2,
                max_charge_power=11.0,
                lastUpdate=self.marker,
                status="available",
                startStatus=False,
            )

        def fetch_last_charge(self, serial):
            return {"last_charge_work_status": 0}

        def set_charge_mode_gen2(self, serial, mode, power):
            assert serial == SAMPLE_SN
            self.calls.append(("write_mode", mode, power))
            self.written = True
            if outcome == "rejected":
                return False
            if outcome in ("applied", "stale_report"):
                self.mode = mode
            return True

        def change_status_gen2(self, serial, action):
            assert serial == SAMPLE_SN
            # A physical Start is replaced by a trace assertion in this fake.
            assert action == "start"
            assert self.mode == 0
            assert self.calls[-1][0] == "read"
            assert self.calls[-1][2] > 1800000000
            self.calls.append(("start",))
            return True

    gateway = Gateway()

    async def execute(function, *args):
        return function(*args)

    hass = types.SimpleNamespace(async_add_executor_job=execute)
    transport_adapter = runtime_adapter.ModeTransportAdapter(hass, SAMPLE_SN, gateway)
    store = Store({"mode": 0})
    policy = runtime_policy.ChargeModePolicy(
        transport_adapter, store, enabled=True, initial_mode=2, timeout=0.01, interval=0
    )
    await policy.async_load()
    if reload_policy:
        await policy.async_close()
        policy = runtime_policy.ChargeModePolicy(
            transport_adapter,
            store,
            enabled=True,
            initial_mode=2,
            timeout=0.01,
            interval=0,
        )
        await policy.async_load()
    assert gateway.calls == []
    assert policy.desired_mode == 0

    # Deliberately bypass HA select: a select action would change the preference.
    gateway.force_external_pv()
    entity.coordinator.charge_mode_policy = policy
    assert store.saved == {"mode": 0}
    if outcome == "applied":
        await entity.async_turn_on()
        assert gateway.calls == [
            ("external_reset", 1),
            ("read", 1, 1800000000),
            ("write_mode", 0, 4.2),
            ("read", 0, 1800000001),
            ("start",),
        ]
    else:
        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on()
        assert not any(call[0] == "start" for call in gateway.calls)
    assert store.saved == {"mode": 0}
    assert policy.desired_mode == 0
    assert gateway.power == 4.2
    assert sum(call[0] == "write_mode" for call in gateway.calls) == 1
    entity.api.change_status_gen2.assert_not_called()


@pytest.mark.parametrize("initial_mode", [0, 1, 2])
async def test_saved_power_survives_device_reset_and_reload(initial_mode):
    store = Store({"mode": 0, "power": 4.2})
    adapter = Adapter(Observation(initial_mode, power=11), Observation(0, power=4.2))
    policy = make_policy(adapter, store)
    await policy.async_load()
    await policy.async_seed_power(11)
    await policy.async_start()
    assert adapter.calls == ["read", ("mode", 0, 4.2), "read", "start"]
    assert store.saved == {"mode": 0, "power": 4.2}
    restored = make_policy(adapter, store)
    await restored.async_load()
    assert restored.desired_power == 4.2


@pytest.mark.parametrize("accepted", [True, False])
async def test_reset_power_without_confirmed_restoration_blocks_start(accepted):
    adapter = Adapter(Observation(1, power=11), Observation(0, power=11))
    adapter.write_result = accepted
    policy = make_policy(adapter, Store({"mode": 0, "power": 4.2}))
    await policy.async_load()
    with pytest.raises(Error):
        await policy.async_start()
    assert "start" not in adapter.calls
    assert policy.desired_power == 4.2


async def test_explicit_power_request_survives_other_mode_selections():
    store = Store()
    adapter = Adapter(Observation(0, power=11), Observation(1, power=11))
    policy = make_policy(adapter, store)
    await policy.async_setting_write(AsyncMock(), desired_mode=0, desired_power=4.3)
    await policy.async_select_mode(1)
    assert store.saved == {"mode": 1, "power": 4.3}
    reloaded = make_policy(adapter, store)
    await reloaded.async_load()
    assert reloaded.desired_power == 4.3


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 0, -1, "11"])
async def test_invalid_persisted_power_rejected(value):
    policy = make_policy(Adapter(Observation(0)), Store({"mode": 0, "power": value}))
    with pytest.raises(Error):
        await policy.async_load()


async def test_cloud_power_entity_keeps_intent_after_reset_report():
    from tests.test_number import _make_entity

    entity = _make_entity()
    policy = make_policy(Adapter(Observation(0)), Store({"mode": 0, "power": 4.2}))
    await policy.async_load()
    entity.coordinator.charge_mode_policy = policy
    entity.coordinator.data[entity.sn]["set_charge_power"] = 11
    entity._handle_coordinator_update()
    assert entity.native_value == 4.2
    assert entity.extra_state_attributes["reported_power_limit"] == 11


async def test_modbus_fast_restores_limit_after_mode_write():
    client = MagicMock()
    client.write_charge_mode.return_value = True
    client.write_max_charge_power.return_value = True

    async def execute(function, *args):
        return function(*args)

    adapter = adapter_module.ModeTransportAdapter(
        types.SimpleNamespace(async_add_executor_job=execute),
        "TEST",
        client,
        modbus=True,
    )
    assert await adapter.write_mode(0, Observation(1, power=4.2))
    assert [call[0] for call in client.mock_calls] == [
        "write_charge_mode",
        "write_max_charge_power",
    ]
    client.write_max_charge_power.assert_called_once_with(4.2)


async def test_cloud_slider_persists_explicit_power_before_device_write():
    from tests.test_number import _make_entity

    entity = _make_entity()
    # The entity imports its own package instance; provide its policy implementation.
    module = sys.modules[
        entity.__class__.__module__.rsplit(".", 1)[0] + ".charge_mode_policy"
    ]
    store = Store({"mode": 0, "power": 4.2})
    policy = module.ChargeModePolicy(Adapter(Observation(0)), store, enabled=True)
    await policy.async_load()
    entity.coordinator.charge_mode_policy = policy
    await entity.async_set_native_value(4.3)
    assert store.saved == {"mode": 0, "power": 4.3}
    entity.coordinator.data[entity.sn]["set_charge_power"] = 11
    entity._handle_coordinator_update()
    assert entity.native_value == 4.3


async def test_power_change_during_preparation_preserves_one_start_at_latest_limit():
    entered, resume = asyncio.Event(), asyncio.Event()
    adapter = Adapter(Observation(0, power=4.2))
    original_read = adapter.read

    async def read():
        if not entered.is_set():
            entered.set()
            await resume.wait()
        return await original_read()

    adapter.read = read
    policy = Policy(adapter, Store(), enabled=True, timeout=2, interval=0)
    policy.desired_power = 4.2
    delivered = []

    async def start():
        delivered.append(policy.desired_power)
        return True

    async def power():
        adapter.observations = [Observation(0, power=11.0)]
        return True

    adapter.start = start
    starting = asyncio.create_task(policy.async_start())
    await entered.wait()
    setting = asyncio.create_task(policy.async_setting_write(power, desired_power=11.0))
    await asyncio.sleep(0)
    resume.set()
    await asyncio.wait_for(asyncio.gather(starting, setting), 2)
    assert delivered == [11.0]
    assert policy.store.saved["power"] == 11.0


async def test_stop_after_pending_power_cancels_retained_start_intent():
    entered, resume = asyncio.Event(), asyncio.Event()
    adapter = Adapter(Observation(0, power=4.2))
    original_read = adapter.read

    async def read():
        entered.set()
        await resume.wait()
        return await original_read()

    adapter.read = read
    policy = Policy(adapter, Store(), enabled=True, timeout=2, interval=0)
    policy.desired_power = 4.2
    starting = asyncio.create_task(policy.async_start())
    await entered.wait()
    setting = asyncio.create_task(
        policy.async_setting_write(AsyncMock(), desired_power=11.0)
    )
    await asyncio.sleep(0)
    stopping = asyncio.create_task(policy.async_stop())
    await asyncio.sleep(0)
    resume.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(starting, setting, stopping, return_exceptions=True), 2
    )
    assert isinstance(outcomes[0], Error)
    assert "start" not in adapter.calls
    assert adapter.calls.count("stop") == 1


async def test_repeated_identical_power_does_not_invalidate_pending_start():
    entered, resume = asyncio.Event(), asyncio.Event()
    adapter = Adapter(Observation(0, power=4.2))
    original_read = adapter.read

    async def read():
        entered.set()
        await resume.wait()
        return await original_read()

    adapter.read = read
    policy = Policy(adapter, Store(), enabled=True, initial_mode=0, timeout=2, interval=0)
    policy.desired_power = 4.2
    starting = asyncio.create_task(policy.async_start())
    await entered.wait()
    operation = AsyncMock()
    for _ in range(10):
        await policy.async_setting_write(operation, desired_mode=0, desired_power=4.2)
    resume.set()
    await asyncio.wait_for(starting, 2)
    operation.assert_not_called()
    assert adapter.calls.count("start") == 1


@pytest.mark.parametrize("last_power", [4.2, 11.0])
async def test_new_power_after_stop_is_not_suppressed_as_an_obsolete_duplicate(
    last_power,
):
    entered, resume = asyncio.Event(), asyncio.Event()
    adapter = Adapter(Observation(0, power=4.2))
    original_read = adapter.read

    async def read():
        entered.set()
        await resume.wait()
        return await original_read()

    adapter.read = read
    policy = Policy(adapter, Store(), enabled=True, timeout=2, interval=0)
    policy.desired_power = 4.2
    starting = asyncio.create_task(policy.async_start())
    await entered.wait()
    obsolete, latest = AsyncMock(), AsyncMock()
    old_power = asyncio.create_task(
        policy.async_setting_write(obsolete, desired_power=11.0)
    )
    await asyncio.sleep(0)
    stopping = asyncio.create_task(policy.async_stop())
    await asyncio.sleep(0)
    new_power = asyncio.create_task(
        policy.async_setting_write(latest, desired_power=last_power)
    )
    await asyncio.sleep(0)
    resume.set()
    results = await asyncio.wait_for(
        asyncio.gather(
            starting, old_power, stopping, new_power, return_exceptions=True
        ),
        2,
    )
    assert isinstance(results[0], Error)
    assert isinstance(results[1], Error)
    assert not isinstance(results[-1], BaseException)
    obsolete.assert_not_called()
    latest.assert_awaited_once()
    assert policy.desired_power == last_power
    assert "start" not in adapter.calls
    assert adapter.calls.count("stop") == 1


@pytest.mark.parametrize("reported_mode", [0, 1, 2])
async def test_disabled_mode_restoration_keeps_observed_mode_and_power_guard(reported_mode):
    adapter = Adapter(Observation(reported_mode, power=11),
                      Observation(reported_mode, power=4.2))
    adapter.preserves_power_all_modes = True
    policy = make_policy(adapter, remember_mode=False, initial_mode=(reported_mode + 1) % 3)
    await policy.async_seed_power(4.2)
    await policy.async_start()
    assert adapter.calls == ["read", ("mode", reported_mode, 4.2), "read", "start"]
    assert policy.desired_mode != reported_mode


async def test_failed_modbus_configuration_block_cannot_confirm_fast_or_start():
    modbus = importlib.import_module(PACKAGE + ".wallbox_modbus")
    client = modbus.WallboxModbusClient("192.0.2.10")
    blocks = {10000: [0] * 20, 10020: None, 10040: [0] * 20,
              10060: [0] * 20, 10084: None}
    client._read = lambda connection, address, count: blocks[address]
    report = client._read_all_inner(None)
    report["sn"] = "TEST"
    assert report["chargeMode"] is None
    assert report["set_charge_power"] is None
    client.read_all = lambda: report
    client.write_start_stop = MagicMock()
    async def execute(function, *args):
        return function(*args)
    adapter = adapter_module.ModeTransportAdapter(
        types.SimpleNamespace(async_add_executor_job=execute), "TEST", client, modbus=True)
    with pytest.raises(Error, match="valid charging mode"):
        await make_policy(adapter).async_start()
    client.write_start_stop.assert_not_called()


async def test_verification_deadline_interrupts_async_wait_before_start():
    adapter = Adapter(Observation(1, power=4.2))
    adapter.write_mode = AsyncMock(side_effect=lambda *args: None)
    async def blocked(*args):
        await asyncio.Event().wait()
    adapter.write_mode = blocked
    with pytest.raises(Error, match="fresh confirmation"):
        await asyncio.wait_for(make_policy(adapter).async_start(), 0.5)
    assert "start" not in adapter.calls


@pytest.mark.parametrize("mode", [0, 1, 2])
async def test_no_preference_preserves_observed_mode_after_power_seed_and_reload(mode):
    store = Store()
    adapter = Adapter(Observation(mode, power=4.2))
    policy = Policy(adapter, store, enabled=True, timeout=0.01, interval=0)
    await policy.async_load()
    await policy.async_seed_power(4.2)
    assert store.saved == {"mode": None, "power": 4.2}
    restored = Policy(adapter, store, enabled=True, timeout=0.01, interval=0)
    await restored.async_load()
    await restored.async_start()
    assert restored.desired_mode is None
    assert adapter.calls == ["read", "start"]


@pytest.mark.parametrize("saved_power", [None, 7.0])
async def test_minimum_power_seed_is_durable_and_never_replaces_saved_limit(saved_power):
    store = Store(None if saved_power is None else {"mode": None, "power": saved_power})
    adapter = Adapter(Observation(1, power=11.0))
    policy = Policy(adapter, store, enabled=True)
    await policy.async_load()
    await policy.async_seed_power(4.2)
    expected = 4.2 if saved_power is None else saved_power
    assert policy.desired_power == expected
    assert store.saved == {"mode": None, "power": expected}
    restored = Policy(adapter, store, enabled=True)
    await restored.async_load()
    await restored.async_seed_power(4.2)
    assert restored.desired_power == expected
    assert adapter.calls == []


@pytest.mark.parametrize("modbus", [False, True])
async def test_disabled_restoration_still_persists_real_entity_selection(modbus):
    from tests.test_select import _make_entity, _select_mod
    entity = _make_entity()
    adapter = Adapter(Observation(0))
    store = Store({"mode": 0, "power": 7.0})
    policy = Policy(adapter, store, enabled=False)
    await policy.async_load()
    entity.coordinator.charge_mode_policy = policy
    client = MagicMock()
    if modbus:
        original = entity
        entity = _select_mod.ModbusChargeModeSelect(original.coordinator, "SN", client)
        entity.hass = original.hass
        entity.async_write_ha_state = MagicMock()
    else:
        entity.api.set_charge_mode_gen2.return_value = True
    await entity.async_select_option("pv_priority")
    assert store.saved == {"mode": 1, "power": 7.0}
    assert adapter.calls == []
    restored = Policy(adapter, store, enabled=True)
    await restored.async_load()
    assert restored.desired_mode == 1 and restored.desired_power == 7.0
    if modbus:
        client.write_charge_mode.assert_called_once_with(1)
    else:
        entity.api.set_charge_mode_gen2.assert_called_once_with(entity.sn, 1, None)


@pytest.mark.parametrize("current", [0,1,2])
async def test_enabling_restoration_adopts_reported_mode_without_device_writes(current):
    store = Store({"mode":2,"power":7.0})
    adapter = Adapter(Observation(current,active=True,power=4.2))
    policy = Policy(adapter,store,enabled=True)
    await policy.async_load()
    await policy.async_adopt_current_mode()
    assert store.saved == {"mode":current,"power":7.0}
    assert adapter.calls == ["read"]
    restored = Policy(adapter,store,enabled=True)
    await restored.async_load()
    assert restored.desired_mode == current


@pytest.mark.parametrize("failure", ["missing_mode","storage"])
async def test_failed_mode_adoption_preserves_existing_intent(failure):
    store = Store({"mode":2,"power":7.0})
    adapter = Adapter(Observation(None if failure=="missing_mode" else 0))
    policy = Policy(adapter,store,enabled=True)
    await policy.async_load()
    if failure=="storage":
        store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    with pytest.raises((Error,OSError)):
        await policy.async_adopt_current_mode()
    assert store.saved == {"mode":2,"power":7.0}
    assert policy.desired_mode == 2
    assert adapter.calls == ["read"]


@pytest.mark.parametrize("cloud", [False, True])
async def test_confirmed_matching_charge_makes_start_idempotent(cloud):
    adapter = Adapter(
        Observation(0, power=4.2, active=True, charging=True,
                    report_marker="test:001", requires_advance=cloud),
        Observation(0, power=4.2, active=True, charging=True,
                    report_marker="test:002", requires_advance=cloud),
    )
    policy = make_policy(adapter)
    policy.desired_power = 4.2
    await policy.async_start()
    assert adapter.calls == (["read", "read"] if cloud else ["read"])


@pytest.mark.parametrize("mode,power", [(1, 4.2), (0, 11.0), (0, None)])
async def test_active_mismatch_never_writes_or_claims_start_success(mode, power):
    adapter = Adapter(Observation(mode, power=power, active=True, charging=True))
    policy = make_policy(adapter)
    policy.desired_power = 4.2
    with pytest.raises(Error, match="stop state not confirmed"):
        await policy.async_start()
    assert adapter.calls == ["read"]


async def test_stale_cloud_charging_does_not_fulfil_duplicate_start():
    adapter = Adapter(Observation(0, power=4.2, active=True, charging=True,
                                  report_marker="test:001", requires_advance=True))
    policy = make_policy(adapter)
    policy.desired_power = 4.2
    with pytest.raises(Error, match="fresh confirmation"):
        await policy.async_start()
    assert set(adapter.calls) == {"read"}


@pytest.mark.parametrize("close", [False, True])
async def test_new_start_after_stop_is_not_an_old_duplicate(close):
    adapter = Adapter(Observation(0, power=4.2))
    policy = Policy(adapter, Store(), enabled=True, initial_mode=0, timeout=2)
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.read

    async def read():
        entered.set()
        await release.wait()
        return await original()

    adapter.read = read
    first = asyncio.create_task(policy.async_start())
    await entered.wait()
    stop = asyncio.create_task(policy.async_stop())
    await asyncio.sleep(0)
    last = asyncio.create_task(policy.async_start())
    await asyncio.sleep(0)
    closing = asyncio.create_task(policy.async_close()) if close else None
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, stop, last, return_exceptions=True)
    if closing:
        await closing
    assert isinstance(results[0], policy_module.RequestSuperseded)
    assert results[1] is None
    if close:
        assert isinstance(results[2], policy_module.RequestSuperseded)
        assert "start" not in adapter.calls
    else:
        assert results[2] is None
        assert adapter.calls == ["read", "stop", "read", "start"]
    assert not policy._pending_start_intents


@pytest.mark.parametrize("cp,power,result", [(1, 0, "start"), (2, 4.2, "already"), (None, 0, "error"), (1, None, "error")])
async def test_modbus_raw_charging_requires_independent_cp_and_power(cp, power, result):
    adapter, client = transport(dict(sn="SN", chargeMode=0, set_charge_power=4.2,
                                    modbus_status_raw=3, modbus_car_connected=cp,
                                    modbus_power=power), modbus=True)
    client.write_start_stop.return_value = True
    policy = make_policy(adapter)
    policy.desired_power = 4.2
    if result == "error":
        with pytest.raises(Error):
            await policy.async_start()
    else:
        await policy.async_start()
    assert client.write_start_stop.call_count == (result == "start")
    client.write_charge_mode.assert_not_called()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "0", None])
async def test_invalid_initial_report_does_not_become_saved_power(value):
    store = Store({"mode": 1})
    adapter = Adapter(Observation(1))
    policy = make_policy(adapter, store)
    await policy.async_load()
    await policy.async_seed_power(value)
    assert policy.desired_power is None
    assert store.saved == {"mode": 1}
    assert adapter.calls == []
    # A later valid initialization may seed once, then survives reload and
    # cannot be replaced by an external report, valid or invalid.
    await policy.async_seed_power(4.2)
    restored = make_policy(adapter, store)
    await restored.async_load()
    await restored.async_seed_power(value)
    await restored.async_seed_power(7)
    assert restored.desired_power == 4.2
    assert store.saved == {"mode": 1, "power": 4.2}
    assert adapter.calls == []


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "0", None])
async def test_invalid_saved_power_still_rejected(value):
    policy = make_policy(Adapter(Observation(0)), Store({"mode": 0, "power": value}))
    with pytest.raises(Error, match="Invalid saved power limit"):
        await policy.async_load()
