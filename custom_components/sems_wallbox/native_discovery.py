"""Optional read-only UDP discovery for compatible GoodWe network modules."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from ipaddress import IPv4Address

from .native_protocol import check_serial


@dataclass(frozen=True)
class DiscoveredWallbox:
    """A discovery candidate, to be selected and validated by the user."""

    host: str
    mac: str
    serial: str


def parse_discovery(data: bytes, peer: tuple[str, int]) -> DiscoveredWallbox:
    """Validate an advertised address, MAC and serial against its UDP source.

    Args:
        data: UDP discovery response bytes.
        peer: Source IP address and port.
    Returns:
        A structurally valid candidate; discovery is not authentication.
    Raises:
        ValueError: Malformed response or unexpected source/address.
    """
    if len(data) > 1024 or peer[1] != 48899:
        raise ValueError("Unexpected discovery response")
    host, mac, serial = data.decode("ascii").strip().split(",")
    host = str(IPv4Address(host))
    if host != peer[0] or not re.fullmatch(r"[0-9A-Fa-f]{12}", mac):
        raise ValueError("Discovery source/MAC mismatch")
    check_serial(serial)
    return DiscoveredWallbox(host, mac.upper(), serial)


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.devices: dict[str, DiscoveredWallbox] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            device = parse_discovery(data, addr)
        except (ValueError, UnicodeError):
            return
        if len(self.devices) < 128:
            self.devices[device.serial] = device

    def error_received(self, exc: Exception) -> None:
        # Discovery is optional; a timeout returns candidates already received.
        pass


async def async_discover(
    target: str = "255.255.255.255", *, timeout: float = 3
) -> list[DiscoveredWallbox]:
    """Probe a supplied address/broadcast without entering configuration mode.

    Args:
        target: IPv4 address or subnet broadcast to probe.
        timeout: Bounded discovery window in seconds.
    Returns:
        Discovered candidates; an empty list does not prove device absence.
    Raises:
        ValueError: Invalid address or timeout.
        OSError: The local UDP socket could not be opened.
    """
    target = str(IPv4Address(target))
    if not 0 < timeout <= 10:
        raise ValueError("Invalid discovery timeout")
    receiver = _Receiver()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: receiver, local_addr=("0.0.0.0", 0), allow_broadcast=True
    )
    try:
        transport.sendto(b"HF-A11ASSISTHREAD", (target, 48899))
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(receiver.devices.values())
