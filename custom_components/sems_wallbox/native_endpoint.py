"""Portable, identity-checked Socket A configuration with durable recovery."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time

from .native_discovery import parse_discovery


def local_endpoint(address: str, port: int) -> str:
    """Validate the address advertised to the wallbox and encode Socket A."""
    ip = ipaddress.IPv4Address(address)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
        raise ValueError("Use a LAN address reachable from the wallbox")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Listener port must be between 1024 and 65535")
    return f"TCP,Client,{port},{ip}"


def check_endpoint(value: str) -> str:
    """Accept only a simple TCP client endpoint; never interpolate AT commands."""
    if not re.fullmatch(r"TCP,Client,[0-9]{1,5},[A-Za-z0-9.-]+(?:,TLS)?", value):
        raise ValueError("Unsupported original Socket A endpoint")
    if not 1 <= int(value.split(",")[2]) <= 65535:
        raise ValueError("Invalid original endpoint port")
    return value


class ManagementSession:
    """Bound one UDP management session to the configured IP and serial."""

    def __init__(self, peer: str, serial: str):
        self.peer = (str(ipaddress.IPv4Address(peer)), 48899)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            response = self.raw(b"HF-A11ASSISTHREAD")
            candidate = parse_discovery(response.encode("ascii"), self.peer)
            if candidate.serial != serial:
                raise ConnectionError("Management device identity does not match")
            try:
                self.raw(b"+ok")
            except TimeoutError:
                pass  # The enrollment acknowledgement has no reply on this module.
        except BaseException:
            self.socket.close()
            raise

    def raw(self, payload: bytes) -> str:
        """Drain stale input, send once and collect a bounded response."""
        self.socket.settimeout(0.05)
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            try:
                self.socket.recvfrom(8192)
            except TimeoutError:
                break
        self.socket.sendto(payload, self.peer)
        end = time.monotonic() + 2
        parts = []
        while time.monotonic() < end:
            self.socket.settimeout(
                min(0.3 if parts else 1.5, max(0.01, end - time.monotonic()))
            )
            try:
                data, peer = self.socket.recvfrom(8192)
            except TimeoutError:
                break
            if peer != self.peer:
                raise ConnectionError("Unexpected management response source")
            parts.append(data.decode("ascii").strip())
        if not parts:
            raise TimeoutError("No management response")
        return "\n".join(parts)

    def command(self, value: str) -> str:
        """Permit configuration reads and a validated Socket A destination only."""
        if value.startswith("AT+NETP="):
            check_endpoint(value.removeprefix("AT+NETP="))
        elif value not in ("AT+NETP", "AT+SOCKB", "AT+TCPLK"):
            raise ValueError("Unsupported management command")
        return self.raw(value.encode("ascii") + b"\r")

    def close(self):
        """Exit management without changing any other module settings."""
        try:
            self.raw(b"AT+Q\r")
        except (OSError, ValueError):
            pass  # Best-effort exit must not hide the primary result.
        finally:
            self.socket.close()


class EndpointManager:
    """Persist restoration intent before mutation; never overwrite another owner."""

    def __init__(
        self, peer, serial, advertised_host, port, store, *, factory=ManagementSession
    ):
        self.peer = str(ipaddress.IPv4Address(peer))
        self.serial = serial
        self.local = local_endpoint(advertised_host, port)
        self.store = store
        self.factory = factory
        self.journal = None
        self._lock = asyncio.Lock()

    async def _io(self, operation):
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Wait for a bounded in-flight mutation before any recovery can start.
            await task
            raise

    def _inspect(self):
        session = self.factory(self.peer, self.serial)
        try:
            current = session.command("AT+NETP")
            socket_b = session.command("AT+SOCKB")
            if not current.startswith("+ok="):
                raise ConnectionError("Socket A readback is missing")
            return check_endpoint(current[4:]), socket_b
        finally:
            session.close()

    def _change(self, expected, target):
        session = self.factory(self.peer, self.serial)
        try:
            current = session.command("AT+NETP")
            socket_b = session.command("AT+SOCKB")
            if socket_b != "+ok=NONE" or current not in (
                "+ok=" + expected,
                "+ok=" + target,
            ):
                raise ConnectionError(
                    "Network ownership conflict; external settings preserved"
                )
            if current != "+ok=" + target:
                try:
                    session.command("AT+NETP=" + target)
                except TimeoutError:
                    # Lost write ACK is ambiguous; read back without replaying the write.
                    pass
            if session.command("AT+NETP") != "+ok=" + target:
                raise ConnectionError("Endpoint change was not confirmed")
        finally:
            session.close()

    async def async_load(self):
        """Load pending recovery; configuration changes must not erase ownership."""
        saved = await self.store.async_load()
        if saved is not None:
            if (
                saved.get("peer") != self.peer
                or saved.get("serial") != self.serial
                or saved.get("local") != self.local
            ):
                raise ValueError(
                    "Recover the previous endpoint before changing TCP configuration"
                )
            check_endpoint(saved["original"])
        self.journal = saved

    async def async_activate(self):
        """Redirect only after the caller has opened its TCP listener."""
        async with self._lock:
            if self.journal is not None:
                raise ConnectionError(
                    "Pending endpoint ownership must be recovered first"
                )
            original, socket_b = await self._io(self._inspect)
            if socket_b != "+ok=NONE" or original == self.local:
                raise ConnectionError("Cannot take ownership of this network baseline")
            self.journal = {
                "peer": self.peer,
                "serial": self.serial,
                "local": self.local,
                "original": original,
            }
            await self.store.async_save(self.journal)
            await self._io(lambda: self._change(original, self.local))

    async def async_restore(self):
        """Restore a matching owned destination and retain intent on failure."""
        async with self._lock:
            if self.journal is None:
                return
            original = self.journal["original"]
            await self._io(lambda: self._change(self.local, original))
            await self.store.async_save(None)
            self.journal = None

    async def async_cloud_link(self):
        """Check TCP link separately; this does not prove fresh SEMS telemetry."""

        def read():
            session = self.factory(self.peer, self.serial)
            try:
                return session.command("AT+TCPLK") == "+ok=on"
            finally:
                session.close()

        return await self._io(read)
