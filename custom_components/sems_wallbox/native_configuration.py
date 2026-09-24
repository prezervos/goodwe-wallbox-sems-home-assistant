"""Original-HCA Auto start configuration and local envelope over Socket A."""

from dataclasses import dataclass
import struct

from .native_protocol import NativeFrame, _frame, check_serial


@dataclass(frozen=True)
class NativeConfiguration:
    """Non-private fields from a verified original-HCA configuration snapshot."""

    auto_start: bool
    scheduled: bool


def configuration_request(identity: bytes, sequence: int) -> bytes:
    """Request the single type-1 configuration block, without acknowledging it."""
    body = struct.pack("<HHBBIBH", 0, 0, 1, 1, 27, 1, 4)
    return _frame(5, body + struct.pack("<I", 1) + bytes(36), identity, sequence)


def decode_configuration(packet: NativeFrame, serial: str) -> NativeConfiguration:
    """Validate identity, layout and both checksums before exposing Auto start.

    Args:
        packet: Complete native frame.
        serial: Enrolled device serial.

    Returns:
        Actual Auto start and schedule flags; no private configuration payload.

    Raises:
        ValueError: Unknown layout, identity, checksum or flag encoding.
    """
    raw = packet.raw
    if (
        packet.command != 602
        or len(raw) != 700
        or raw[34:66] != check_serial(serial).ljust(32, b"\0")
        or raw[66] != 1
        or struct.unpack_from("<HHH", raw, 67) != (1, 1, 626)
        or sum(raw[6:-1]) & 255 != raw[-1]
    ):
        raise ValueError("Unexpected Auto start configuration identity or layout")
    data = raw[73:-1]
    if (
        sum(data[:624]) & 65535 != int.from_bytes(data[624:], "little")
        or data[527] not in (0, 1)
        or data[529] not in (0, 1)
    ):
        raise ValueError("Invalid Auto start configuration checksum or flags")
    return NativeConfiguration(bool(data[527]), bool(data[529]))


def auto_start_request(
    identity: bytes, sequence: int, serial: str, enabled: bool
) -> bytes:
    """Encode a native boundary followed by a local Auto start envelope.

    The original HCA shares its UART parser between Socket A and local commands.
    A complete command-0 no-op precedes the local envelope in ONE TCP write.
    No Bluetooth adapter is involved. Module packet splitting can reject this
    route, so callers MUST confirm via a fresh configuration read, never ACK.

    Args:
        identity: Enrolled native session identity.
        sequence: Outgoing native message sequence.
        serial: Sixteen-character original-HCA serial.
        enabled: Requested Auto start state.

    Returns:
        Complete native frame plus checksummed local command 7.

    Raises:
        ValueError: Unsupported serial or non-boolean request.
    """
    serial_bytes = check_serial(serial)
    if len(serial_bytes) != 16 or type(enabled) is not bool:
        raise ValueError("Invalid original-HCA Auto start request")
    body = b":" + serial_bytes + b"\x01\x07\x00" + bytes([16 if enabled else 17])
    local = b"#BLE#" + body + bytes([sum(body) & 255, 13])
    return _frame(0, b"", identity, sequence) + local
