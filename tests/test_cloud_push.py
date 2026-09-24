"""Push hints cannot replace reports or cross transport lifetimes."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_native_transport import PACKAGE

module = importlib.import_module(PACKAGE + ".cloud_push")


def subject():
    owner = SimpleNamespace(
        local=False,
        transitioning=False,
        routing_epoch=1,
        _closed=False,
        last_update_success=True,
        update_interval=30,
        data={"unchanged": True},
        async_request_refresh=AsyncMock(),
        hass=SimpleNamespace(
            async_create_background_task=lambda c, n: asyncio.create_task(c)
        ),
    )
    push = module.CloudPush(owner, "TEST-SERIAL", lambda: {})
    push.DEBOUNCE = 0.001
    push.MIN_REFRESH_INTERVAL = 0.01
    push.CHECK_INTERVAL = 0.001
    push.RETRY_MIN = 0.005
    push.RETRY_MAX = 0.02
    return push


def event(push, **values):
    payload = {"sn": "TEST-SERIAL", "value": "untrusted", **values}
    return push.event(push.topics[1], json.dumps(payload).encode(), epoch=1)


@pytest.mark.asyncio
async def test_burst_only_refreshes_authoritative_reader_once():
    push = subject()
    for i in range(20):
        assert event(push, tid=str(i))
    await push._refresh_task
    push.owner.async_request_refresh.assert_awaited_once()
    assert push.owner.data == {"unchanged": True}
    assert push.owner.update_interval == 30
    await push.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "retained",
        "foreign_topic",
        "foreign_serial",
        "bad_json",
        "array",
        "large",
        "old_epoch",
    ],
)
async def test_invalid_hint_never_refreshes(kind):
    push = subject()
    args = dict(topic=push.topics[0], payload=b'{"sn":"TEST-SERIAL"}', epoch=1)
    if kind == "retained":
        args["retained"] = True
    elif kind == "foreign_topic":
        args["topic"] += "/other"
    elif kind == "foreign_serial":
        args["payload"] = b'{"sn":"other"}'
    elif kind == "bad_json":
        args["payload"] = b"bad"
    elif kind == "array":
        args["payload"] = b"[]"
    elif kind == "large":
        args["payload"] = b" " * 32769
    elif kind == "old_epoch":
        args["epoch"] = 0
    assert not push.event(**args)
    assert push._refresh_task is None


@pytest.mark.asyncio
async def test_duplicates_and_enveloped_payloads():
    push = subject()
    assert event(push, tid="one")
    assert not event(push, tid="one")
    assert push.event(
        push.topics[0],
        json.dumps({"message": json.dumps({"sn": "TEST-SERIAL", "tid": "two"})}),
        epoch=1,
    )
    await push._refresh_task
    assert push.refresh_count == 1
    await push.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["local", "transitioning", "epoch", "close"])
async def test_pending_refresh_cancelled_on_routing_change(change):
    push = subject()
    assert event(push)
    if change == "epoch":
        push.owner.routing_epoch += 1
    elif change == "close":
        await push.close()
    else:
        setattr(push.owner, change, True)
    if push._refresh_task is not None:
        await push._refresh_task
    push.owner.async_request_refresh.assert_not_awaited()
    await push.close()


@pytest.mark.asyncio
async def test_connection_failure_retries_without_poisoning_polling():
    push = subject()
    push._listen = AsyncMock(side_effect=ConnectionError("offline"))
    push.start()
    for _ in range(100):
        if push._listen.await_count >= 2:
            break
        await asyncio.sleep(0.001)
    assert push._listen.await_count >= 2
    assert push.owner.last_update_success and push.owner.update_interval == 30
    await push.close()


@pytest.mark.asyncio
async def test_tcp_disconnect_and_cloud_reconnect_and_unload():
    push = subject()
    entered = asyncio.Event()
    disconnected = asyncio.Event()
    calls = []

    async def listen(epoch):
        calls.append(epoch)
        push.connected = True
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            disconnected.set()

    push._listen = listen
    push.start()
    await asyncio.wait_for(entered.wait(), 1)
    push.owner.local = True
    push.owner.routing_epoch += 1
    await asyncio.wait_for(disconnected.wait(), 1)
    assert not event(push)
    entered.clear()
    push.owner.local = False
    await asyncio.wait_for(entered.wait(), 1)
    assert calls == [1, 2]
    await push.close()
    assert not push.connected and push.task is None
    await push.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "unload"])
async def test_hints_during_slow_refresh_are_coalesced_and_unload_drains(finish):
    """Events during HTTP work share one task; unload owns its cancellation."""
    push = subject()
    entered = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()

    async def slow_refresh():
        entered.set()
        try:
            await release.wait()
        finally:
            exited.set()

    push.owner.async_request_refresh.side_effect = slow_refresh
    try:
        assert event(push, tid="initial")
        task = push._refresh_task
        await asyncio.wait_for(entered.wait(), 1)
        for index in range(20):
            assert event(push, tid=f"during-read-{index}")
        assert push._refresh_task is task
        assert push.refresh_count == 1
        if finish == "complete":
            release.set()
            await task
            push.owner.async_request_refresh.assert_awaited_once()
            # A later hint still works; coalescing cannot permanently mute push.
            assert event(push, tid="after-read")
            await push._refresh_task
            assert push.owner.async_request_refresh.await_count == 2
        else:
            await push.close()
            assert task.done() and exited.is_set()
            assert push._refresh_task is None
            assert not event(push, tid="late-old-listener")
            push.owner.async_request_refresh.assert_awaited_once()
            replacement = subject()
            try:
                assert event(replacement, tid="initial")
                await replacement._refresh_task
                replacement.owner.async_request_refresh.assert_awaited_once()
                push.owner.async_request_refresh.assert_awaited_once()
            finally:
                await replacement.close()
    finally:
        release.set()
        await push.close()
