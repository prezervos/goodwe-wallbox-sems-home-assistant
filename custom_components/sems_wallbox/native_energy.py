"""Read-only cumulative storage snapshot; distinct from cloud session energy."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .native_protocol import NativeFrame, _frame, check_serial


@dataclass(frozen=True)
class NativeEnergy:
    """Validated lifetime counters, without private storage record contents."""

    energy_kwh: float
    duration_seconds: int
    sessions: int


def energy_request(identity: bytes, sequence: int) -> bytes:
    """Encode the single cumulative-block read, never a bulk-history request.

    Args:
        identity: Verified native session envelope.
        sequence: Outgoing one-byte message sequence.

    Returns:
        Command 5, parameter 27, type 2 with the verified fixed-size payload.
    """
    body = struct.pack("<HHBBIBH", 0, 0, 1, 1, 27, 1, 4)
    return _frame(5, body + struct.pack("<I", 2) + bytes(36), identity, sequence)


def decode_energy(packet: NativeFrame, serial: str) -> NativeEnergy:
    """Validate a complete cumulative snapshot before exposing its counters.

    Args:
        packet: Checksummed frame produced by NativeDecoder.
        serial: Enrolled device identity.

    Returns:
        Lifetime energy in kWh, total seconds and lifetime session count.

    Raises:
        ValueError: Wrong device/type/shape or invalid embedded storage checksum.
    """
    raw = packet.raw
    if (packet.command != 602 or len(raw) != 496
            or raw[34:66] != check_serial(serial).ljust(32, b"\0")
            or raw[66] != 2 or struct.unpack_from("<HHH", raw, 67) != (1, 1, 422)):
        raise ValueError("Unexpected cumulative snapshot identity or layout")
    data = raw[73:-1]
    if data[:2] != b"\xeb\x90" or sum(data[:-2]) & 65535 != int.from_bytes(data[-2:], "little"):
        raise ValueError("Invalid cumulative storage magic or checksum")
    energy, seconds = struct.unpack_from("<II", data, 412)
    sessions = struct.unpack_from("<I", data, 3)[0]
    return NativeEnergy(energy / 100, seconds, sessions)
