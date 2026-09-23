"""Verify independent SEMS observations without using HTTP receipt time as freshness."""

import importlib
from unittest.mock import MagicMock

import pytest

from tests.test_native_transport import PACKAGE

module = importlib.import_module(PACKAGE + ".cloud_observation")


def response(body):
    result = MagicMock()
    result.json.return_value = body
    return result


def reader(*bodies):
    session = MagicMock()
    session.post.side_effect = [response(body) for body in bodies]
    return module.CloudObservationReader("user", "secret", session=session), session


LOGIN = {"code": 0, "data": {"token": "fake-token", "uid": "fake-uid"}}
STATUS = {
    "code": 0,
    "data": {
        "sn": "TEST",
        "lastUpdate": "2026-09-17 12:10:00",
        "power": "4.0",
        "set_charge_power": 4.2,
    },
}


def test_report_timestamp_and_power_are_preserved():
    client, session = reader(LOGIN, STATUS, STATUS)
    assert client.read("TEST") == STATUS["data"]
    assert client.read("TEST")["lastUpdate"] == STATUS["data"]["lastUpdate"]
    assert len(session.post.call_args_list) == 3
    assert all(
        call.kwargs["allow_redirects"] is False for call in session.post.call_args_list
    )


@pytest.mark.parametrize(
    "data", [None, {}, {"sn": "OTHER", "lastUpdate": "x"}, {"sn": "TEST"}]
)
def test_missing_identity_or_timestamp_is_not_fabricated(data):
    client, _ = reader(LOGIN, {"code": 0, "data": data})
    with pytest.raises(ConnectionError):
        client.read("TEST")


@pytest.mark.parametrize("failure", [
    {"msg": "Authorization has expired", "data": None},
    *({"code": code, "msg": "localized message", "data": None}
      for code in (100001, "100001", 100002, "100002")),
])
def test_expired_token_is_refreshed_once_for_read_only_request(failure):
    client, session = reader(
        LOGIN, failure, LOGIN, STATUS
    )
    assert client.read("TEST")["sn"] == "TEST"
    assert len(session.post.call_args_list) == 4


@pytest.mark.parametrize("failure", [
    {"msg": "Authorization has expired", "data": None},
    *({"code": code, "data": None} for code in (100001, "100001", 100002, "100002")),
])
def test_expired_token_retries_are_bounded(failure):
    client, session = reader(LOGIN, failure, LOGIN, failure)
    with pytest.raises(module.CloudAuthenticationError):
        client.read("TEST")
    assert len(session.post.call_args_list) == 4
    assert client._token is None


def test_authentication_error_does_not_request_device_data():
    client, session = reader({"hasError": True, "code": 100})
    with pytest.raises(ConnectionError):
        client.read("TEST")
    assert session.post.call_count == 1


@pytest.mark.parametrize("code", [0, "0"])
def test_success_code_accepts_numeric_and_string_forms(code):
    client, _ = reader(dict(LOGIN, code=code), dict(STATUS, code=code))
    assert client.read("TEST") == STATUS["data"]


@pytest.mark.parametrize("code", [1, "1", "error"])
def test_error_code_rejects_otherwise_valid_report(code):
    client, _ = reader(LOGIN, dict(STATUS, code=code))
    with pytest.raises(ConnectionError):
        client.read("TEST")


@pytest.mark.parametrize(
    "status,power,start_button,active",
    [
        ("EVDetail_Status_Title_Waiting", "0", True, False),
        ("EVDetail_Status_Title_Waiting", "4.1", True, True),
        ("EVDetail_Status_Title_Charging", "0", False, True),
        ("unknown", "0", True, True),
    ],
)
async def test_v3_start_button_is_not_a_charging_measurement(
    status, power, start_button, active
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".charge_mode_adapter")
    report = dict(
        STATUS["data"],
        chargeMode=0,
        status=status,
        power=power,
        startStatus=start_button,
    )
    client = SimpleNamespace(
        supports_timestamped_observation=True,
        fetch_status_observation=lambda sn: report,
        get_data_gen2=lambda sn: {
            "sn": sn, "_reported_charge_mode": 0,
            "set_charge_power": 5.0, "power": 5.0,
        },
    )
    hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args))
    )
    observation = await adapter_module.ModeTransportAdapter(hass, "TEST", client).read()
    assert observation.active is active
    assert observation.power == 5.0
    assert observation.requires_advance
    assert observation.report_marker == "local:2026-09-17T12:10:00.000000"


@pytest.mark.parametrize("advances", [True, False])
async def test_configured_ceiling_and_fresh_telemetry_are_both_required(advances):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".charge_mode_adapter")
    policy_module = importlib.import_module(PACKAGE + ".charge_mode_policy")
    reports = []

    def read_report(sn):
        reports.append(sn)
        return dict(STATUS["data"], chargeMode=0, status="standby", power="0",
                    lastUpdate=1800000000 + (len(reports) if advances else 0))

    client = SimpleNamespace(
        supports_timestamped_observation=True,
        fetch_status_observation=read_report,
        get_data_gen2=lambda sn: {
            "sn": sn, "_reported_charge_mode": 0, "set_charge_power": 5.0,
        },
        change_status_gen2=MagicMock(return_value=True),
        set_charge_mode_gen2=MagicMock(return_value=True),
    )
    hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args))
    )
    adapter = adapter_module.ModeTransportAdapter(hass, "TEST", client)
    policy = policy_module.ChargeModePolicy(adapter, AsyncMock(), timeout=0.02, interval=0)
    policy.desired_power = 5.0
    if advances:
        await policy.async_start()
        client.change_status_gen2.assert_called_once_with("TEST", "start")
    else:
        with pytest.raises(policy_module.ModeVerificationError):
            await policy.async_start()
        client.change_status_gen2.assert_not_called()
    client.set_charge_mode_gen2.assert_not_called()


@pytest.mark.parametrize("settings", [None, {"sn": "OTHER"}])
async def test_configuration_failure_cannot_fall_back_to_v3_allocation(settings):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".charge_mode_adapter")
    client = SimpleNamespace(
        supports_timestamped_observation=True,
        fetch_status_observation=lambda sn: dict(
            STATUS["data"], chargeMode=0, status="standby", power="0"
        ),
        get_data_gen2=lambda sn: settings,
    )
    hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args))
    )
    with pytest.raises(adapter_module.ModeVerificationError, match="configuration"):
        await adapter_module.ModeTransportAdapter(hass, "TEST", client).read()


@pytest.mark.parametrize("overrides,expected", [
    ({}, True),
    ({"lastUpdate": "2026-09-21 12:00:00"}, False),
    ({"lastUpdate": None}, False),
    ({"sn": "OTHER"}, False),
    ({"status": "charging"}, False),
    ({"status": "unknown"}, False),
    ({"power": "4.2"}, False),
    ({"power": None}, False),
    ({"power": False}, False),
    ({"power": "NaN"}, False),
    ({"lastUpdate": "2026-09-21T12:00:01Z"}, False),
])
async def test_rejected_stop_requires_advancing_idle_measurement(overrides, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".charge_mode_adapter")
    baseline = dict(sn="TEST", status="waiting", power="0",
                    lastUpdate="2026-09-21 12:00:00")
    fresh = dict(baseline, lastUpdate="2026-09-21 12:00:01", **{
        key: value for key, value in overrides.items() if key != "lastUpdate"
    })
    if "lastUpdate" in overrides:
        fresh["lastUpdate"] = overrides["lastUpdate"]
    reports = iter([baseline, fresh])
    client = SimpleNamespace(
        supports_timestamped_observation=True,
        change_status_gen2=MagicMock(return_value=False),
        fetch_status_observation=lambda sn: next(reports, fresh),
    )
    hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args))
    )
    adapter = adapter_module.ModeTransportAdapter(hass, "TEST", client)
    confirm = adapter._confirm_stopped
    adapter._confirm_stopped = lambda: confirm(timeout=0.02, interval=0)
    assert await adapter.stop() is expected
    client.change_status_gen2.assert_called_once_with("TEST", "stop")


@pytest.mark.parametrize("acknowledged,timestamped", [(True, True), (False, False)])
async def test_stop_preserves_ack_and_unsupported_transport_behavior(acknowledged, timestamped):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter_module = importlib.import_module(PACKAGE + ".charge_mode_adapter")
    client = SimpleNamespace(
        supports_timestamped_observation=timestamped,
        change_status_gen2=MagicMock(return_value=acknowledged),
        fetch_status_observation=MagicMock(),
    )
    hass = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args))
    )
    assert await adapter_module.ModeTransportAdapter(hass, "TEST", client).stop() is acknowledged
    client.fetch_status_observation.assert_not_called()


@pytest.mark.parametrize("owned", [False, True])
def test_reader_close_respects_session_ownership(owned):
    from unittest.mock import patch
    session = MagicMock()
    with patch.object(module.requests, "Session", return_value=session):
        client = module.CloudObservationReader("user", "secret", session=None if owned else session)
    client.close()
    client.close()
    assert session.close.call_count == int(owned)
    with pytest.raises(ConnectionError, match="closed"):
        client.read("TEST")
    session.post.assert_not_called()


def test_reader_close_waits_for_inflight_read():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch

    entered = threading.Event()
    release = threading.Event()
    session = MagicMock()

    def post(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return response(STATUS)

    session.post.side_effect = post
    with patch.object(module.requests, "Session", return_value=session):
        client = module.CloudObservationReader("user", "secret")
    client._token = {"token": "test"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(client.read, "TEST")
        try:
            assert entered.wait(2)
            close = pool.submit(client.close)
            assert not close.done()
            session.close.assert_not_called()
        finally:
            release.set()
        assert read.result(timeout=2) == STATUS["data"]
        close.result(timeout=2)
    session.close.assert_called_once_with()
