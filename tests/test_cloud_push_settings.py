"""Broker discovery validates TLS and region without credential disclosure."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.test_cloud_push import module, subject as push_subject
from tests.test_sems_api import OutOfRetries, _make_api


def response(data, code="00000"):
    value = MagicMock()
    value.json.return_value = {"code": code, "data": data}
    return value


def subject():
    api = _make_api()
    api._ensure_web_token = MagicMock(return_value=True)
    api._build_web_headers = MagicMock(return_value={})
    api._web_api_base = "https://eu-sems.goodwe.com/"
    return api


def credentials(**kwargs):
    return {
        "clientId": "test",
        "userName": "user",
        "password": "secret",
        "brokerUrl": "wss://netty-wss-eu.iot.goodwe-power.com:8885/mqtt",
        **kwargs,
    }


def test_direct_validated_credentials():
    api = subject()
    with patch("requests.get", return_value=response(credentials())):
        assert api.fetch_mqtt_settings() == credentials()


@pytest.mark.parametrize(
    "url",
    [
        "ws://x.goodwe-power.com/mqtt",
        "wss://evil.example/mqtt",
        "wss://goodwe-power.com.evil.example/mqtt",
        "wss://user:pass@x.goodwe-power.com/mqtt",
    ],
)
def test_unsafe_broker_rejected(url):
    api = subject()
    with patch("requests.get", return_value=response(credentials(brokerUrl=url))):
        with pytest.raises(ValueError):
            api.fetch_mqtt_settings()


def test_regional_frontend_fallback():
    api = subject()
    config = MagicMock()
    config.text = '{"mqttUrlPolling":{"eu":"wss://eu.iot.goodwe-power.com:8885/mqtt","au":"wss://au.iot.goodwe-power.com/mqtt"}}'
    with patch(
        "requests.get", side_effect=[response(credentials(brokerUrl=None)), config]
    ) as get:
        assert (
            api.fetch_mqtt_settings()["brokerUrl"]
            == "wss://eu.iot.goodwe-power.com:8885/mqtt"
        )

    assert get.call_args_list[-1].kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_expired_token_retried_once():
    api = subject()
    with patch(
        "requests.get", side_effect=[response(None, "C0602"), response(credentials())]
    ) as get:
        assert api.fetch_mqtt_settings() == credentials()
        assert get.call_count == 2


def test_login_failure_becomes_optional_connection_error():
    api = subject()
    api._build_web_headers.side_effect = OutOfRetries("expired")
    with pytest.raises(ConnectionError):
        api.fetch_mqtt_settings()


def test_mqtt_token_renewal_waits_for_in_flight_stop():
    """A broker reconnect cannot replace the token of an unfinished Stop."""
    api = _make_api()
    api._plant_id = "plant-001"
    api._web_token = {"uid": "u", "token": "old", "timestamp": 1}
    stop_entered = Event()
    release_stop = Event()
    mqtt_started = Event()
    order = []

    def post(*args, **kwargs):
        stop_entered.set()
        assert release_stop.wait(2)
        order.append("stop_completed")
        return response(True)

    def get(*args, **kwargs):
        order.append("mqtt_request")
        return response(None, "C0602") if len(order) == 2 else response(credentials())

    def login():
        order.append("renew_token")
        return {"uid": "u", "token": "new", "timestamp": 2}

    def reconnect():
        mqtt_started.set()
        return api.fetch_mqtt_settings()

    with patch("requests.post", side_effect=post), patch(
        "requests.get", side_effect=get
    ), patch.object(api, "_fetch_web_token", side_effect=login) as renew:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stop = executor.submit(api.change_status_gen2, "SN001", "stop")
            try:
                assert stop_entered.wait(2)
                mqtt = executor.submit(reconnect)
                assert mqtt_started.wait(2)
                assert not mqtt.done()
            finally:
                release_stop.set()
            assert stop.result(timeout=2)
            assert mqtt.result(timeout=2) == credentials()
        renew.assert_called_once()
    assert order == ["stop_completed", "mqtt_request", "renew_token", "mqtt_request"]


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery_unavailable", [False, True])
async def test_listener_reconnect_renews_shared_session_and_resubscribes(
    monkeypatch, discovery_unavailable
):
    """Exercise real discovery and listener retry, replacing only HTTP and MQTT I/O."""
    api = _make_api()
    api._web_token = {"uid": "u", "token": "old", "timestamp": 1}
    push = push_subject()
    push.settings = api.fetch_mqtt_settings
    push.owner.hass.async_add_executor_job = asyncio.to_thread
    clients = []
    subscribed = asyncio.Event()
    disconnect = asyncio.Event()
    refresh = asyncio.Event()
    push.owner.async_request_refresh.side_effect = refresh.set

    class BrokerClient:
        """Model broker disconnect and a hint on the replacement connection."""

        def __init__(self, **kwargs):
            self.arguments = kwargs
            self.subscriptions = []
            self.closed = False
            self.index = len(clients)
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def subscribe(self, topics):
            self.subscriptions = topics
            subscribed.set()

        @property
        def messages(self):
            return self.receive()

        async def receive(self):
            if self.index == 0:
                await disconnect.wait()
                raise module.aiomqtt.MqttError("broker disconnected")
            yield SimpleNamespace(
                topic=push.topics[1],
                payload=b'{"sn":"TEST-SERIAL","tid":"after-renewal"}',
                retain=False,
            )
            await asyncio.Event().wait()

    monkeypatch.setattr(module.aiomqtt, "Client", BrokerClient)
    old = credentials(clientId="old-client", password="old-password")
    new = credentials(clientId="new-client", password="new-password")
    responses = [response(old)]
    if discovery_unavailable:
        responses.append(response(None, "unavailable"))
    responses.extend([response(None, "C0602"), response(new)])
    with (
        patch("requests.get", side_effect=responses) as get,
        patch.object(api, "_fetch_web_token", return_value={
            "uid": "u", "token": "new", "timestamp": 2
        }) as renew,
    ):
        push.start()
        try:
            await asyncio.wait_for(subscribed.wait(), 2)
            disconnect.set()
            await asyncio.wait_for(refresh.wait(), 2)
            assert len(clients) == 2
            assert clients[0].closed
            assert not clients[1].closed
            assert [c.arguments["password"] for c in clients] == [
                "old-password", "new-password"
            ]
            assert [c.arguments["identifier"] for c in clients] == [
                "old-client", "new-client"
            ]
            for client in clients:
                assert client.subscriptions == [(topic, 0) for topic in push.topics]
                assert client.arguments["transport"] == "websockets"
                assert client.arguments["port"] == 8885
                assert client.arguments["tls_context"] is not None
            assert get.call_count == 3 + int(discovery_unavailable)
            renew.assert_called_once()
            assert api._web_token["token"] == "new"
            assert api.login_attempts == api.successful_logins == 1
            assert api.session_recovery_attempts == 1
            assert api.last_login_at is not None
            assert push.subscription_count == 2
            assert push.last_subscription_at is not None
            assert push.owner.last_update_success
            assert push.owner.update_interval == 30
            assert push.owner.data == {"unchanged": True}
            assert push.event_counts == {"telemetry": 0, "charging": 1}
            push.owner.async_request_refresh.assert_awaited_once()
        finally:
            await push.close()
    assert all(client.closed for client in clients)
    assert not push.connected and push.task is None


def test_mqtt_discovery_reuses_default_original_login_session():
    """Discovery uses the same original-login token and returned regional gateway."""
    import json
    from tests.test_sems_api import _login_response, sems_api_module

    api = _make_api()
    login = _login_response({"token": "shared-session", "client": "semsPlusWeb"}, region="au")
    mqtt = credentials(brokerUrl="wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt")
    with patch("requests.post", return_value=login) as post, patch(
        "requests.get", return_value=response(mqtt)
    ) as get:
        assert api.test_authentication()
        assert api.fetch_mqtt_settings() == mqtt
    post.assert_called_once()
    assert post.call_args.args[0] == sems_api_module._LOGIN_URLS["original"]
    assert get.call_args.args[0].startswith("https://au-gateway.semsportal.com/web/sems/")
    assert json.loads(get.call_args.kwargs["headers"]["token"])["token"] == "shared-session"
    api.close()
