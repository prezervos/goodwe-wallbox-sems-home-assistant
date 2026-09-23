"""Exercise endpoint ownership without real network mutations."""

import importlib

import pytest

from tests.test_native_transport import PACKAGE

module = importlib.import_module(PACKAGE + ".native_endpoint")


class Store:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, value):
        self.data = value


class Module:
    def __init__(self):
        self.endpoint = "TCP,Client,22001,cloud.example.test,TLS"
        self.socket_b = "+ok=NONE"
        self.commands = []
        self.fail_write = False
        self.store = Store()

    def factory(self, peer, serial):
        return self

    def command(self, value):
        self.commands.append(value)
        if value == "AT+NETP":
            return "+ok=" + self.endpoint
        if value == "AT+SOCKB":
            return self.socket_b
        if value.startswith("AT+NETP="):
            assert self.store.data is not None, "Recovery intent must precede mutation"
            if self.fail_write:
                return "+err"
            self.endpoint = value.split("=", 1)[1]
            return "+ok"
        raise AssertionError(value)

    def close(self):
        pass

    def manager(self):
        return module.EndpointManager(
            "192.0.2.10", "TEST", "192.0.2.20", 18899, self.store, factory=self.factory
        )


async def test_endpoint_roundtrip_and_restart_recovery():
    device = Module()
    manager = device.manager()
    await manager.async_load()
    await manager.async_activate()
    assert device.endpoint == manager.local
    replacement = device.manager()
    await replacement.async_load()
    await replacement.async_restore()
    assert device.endpoint == "TCP,Client,22001,cloud.example.test,TLS"
    assert device.store.data is None


async def test_endpoint_preserves_external_owner_and_recovery_intent():
    device = Module()
    manager = device.manager()
    await manager.async_activate()
    device.endpoint = "TCP,Client,12345,other.example.test"
    with pytest.raises(ConnectionError, match="ownership conflict"):
        await manager.async_restore()
    assert device.endpoint.endswith("other.example.test")
    assert device.store.data is not None


async def test_failed_write_preserves_recovery_journal():
    device = Module()
    device.fail_write = True
    manager = device.manager()
    with pytest.raises(ConnectionError):
        await manager.async_activate()
    assert device.store.data is not None
    await manager.async_restore()
    assert device.store.data is None


async def test_socket_b_conflict_prevents_takeover():
    device = Module()
    device.socket_b = "+ok=TCP,Server,502"
    with pytest.raises(ConnectionError):
        await device.manager().async_activate()
    assert not any(value.startswith("AT+NETP=") for value in device.commands)
    assert device.store.data is None


@pytest.mark.parametrize(
    "value",
    ["TCP,Client,1,x\rAT+Z", "UDP,Client,1,x", "TCP,Client,0,x", "TCP,Server,12,x"],
)
def test_endpoint_command_injection_and_invalid_modes_rejected(value):
    with pytest.raises(ValueError):
        module.check_endpoint(value)


async def test_changed_config_cannot_erase_pending_recovery():
    device = Module()
    manager = device.manager()
    await manager.async_activate()
    changed = module.EndpointManager(
        "192.0.2.11", "TEST", "192.0.2.20", 18899, device.store
    )
    with pytest.raises(ValueError):
        await changed.async_load()


async def test_lost_write_ack_uses_readback_without_replaying_write():
    device = Module()
    original = device.command

    def command(value):
        result = original(value)
        if value.startswith("AT+NETP="):
            raise TimeoutError("Write ACK lost")
        return result

    device.command = command
    manager = device.manager()
    await manager.async_activate()
    assert device.endpoint == manager.local
    assert sum(value.startswith("AT+NETP=") for value in device.commands) == 1
    await manager.async_restore()
    assert device.store.data is None
    assert sum(value.startswith("AT+NETP=") for value in device.commands) == 2
