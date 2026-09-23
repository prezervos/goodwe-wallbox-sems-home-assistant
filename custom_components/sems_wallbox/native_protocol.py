"""GoodWe HCA native Socket A framing and verified status/control fields.

This protocol is distinct from Modbus TCP and Bluetooth. Unknown commands,
history retirement and firmware operations are deliberately not encoded.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeFrame:
    """A complete frame whose declared length and checksum were verified."""

    command: int
    sequence: int
    identity: bytes
    raw: bytes


class NativeDecoder:
    """Decode fragmented/coalesced TCP data with a bounded frame size."""

    def __init__(self, max_length: int = 4096) -> None:
        self.buffer = bytearray()
        self.max_length = max_length

    def feed(self, data: bytes) -> list[NativeFrame]:
        """Consume bytes and return verified frames.

        Args:
            data: Received TCP bytes.
        Returns:
            Complete frames; an incomplete final frame remains buffered.
        Raises:
            ValueError: Invalid header, size, information byte or checksum.
        """
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 4:
            size = int.from_bytes(self.buffer[2:4], "little")
            if self.buffer[:2] != b"\xaa\xf5" or not 31 <= size <= self.max_length:
                raise ValueError("Invalid native frame header or length")
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            if raw[4] != 16 or sum(raw[6:-1]) & 255 != raw[-1]:
                raise ValueError("Invalid native information byte or checksum")
            frames.append(
                NativeFrame(int.from_bytes(raw[6:8], "little"), raw[5], raw[8:30], raw)
            )
            del self.buffer[:size]
        return frames


@dataclass(frozen=True)
class NativeStatus:
    """Observed settings and measurements; limit and actual power are separate."""

    serial: str
    state: int
    mode: int
    limit_kw: float
    power_kw: float
    currents_a: tuple[float, float, float]
    voltages_v: tuple[float, float, float]
    fault_code: int
    connection: int
    session_seconds: int
    minimum_power: bool | None = None
    session_energy_kwh: float | None = None

    @property
    def charging(self) -> bool:
        """Return whether measured current and power confirm charging."""
        return self.state == 2 and self.power_kw > 0 and any(self.currents_a)

    @property
    def stopped(self) -> bool:
        """Return whether an idle/end state and zero measurements confirm Stop."""
        return self.state in (0, 3) and self.power_kw == 0 and not any(self.currents_a)


def check_serial(serial: str) -> bytes:
    """Validate a configured ASCII device identity and return encoded bytes."""
    value = serial.encode("ascii")
    if not 1 <= len(value) <= 32 or b"\0" in value:
        raise ValueError("Invalid wallbox serial")
    return value


def identifies(packet: NativeFrame, serial: str) -> bool:
    """Check serial-bearing messages against the configured device."""
    sizes = {102: 69, 104: 239, 106: 122, 202: 319, 2104: 239}
    return len(packet.raw) == sizes.get(packet.command) and packet.raw[34:66].rstrip(
        b"\0"
    ) == check_serial(serial)


def decode_status(packet: NativeFrame, serial: str) -> NativeStatus:
    """Read verified single-connector telemetry.

    Args:
        packet: Frame returned by NativeDecoder.
        serial: Configured wallbox serial.
    Returns:
        Observed state, mode, limit, power, phases and raw diagnostic code.
    Raises:
        ValueError: Wrong command, device, connector or unknown mode.
    """
    if (
        packet.command not in (104, 2104)
        or not identifies(packet, serial)
        or packet.raw[67] != 1
    ):
        raise ValueError("Unexpected native status identity or layout")
    body = packet.raw[30:-1]
    mode = body[204]
    if mode not in (0, 1, 2):
        raise ValueError("Unknown native charging mode")
    return NativeStatus(
        serial,
        body[39],
        mode,
        struct.unpack_from("<I", body, 157)[0] / 10,
        struct.unpack_from("<I", body, 153)[0] / 10,
        tuple(struct.unpack_from("<H", body, i)[0] / 10 for i in (73, 75, 77)),
        tuple(struct.unpack_from("<H", body, i)[0] / 10 for i in (67, 69, 71)),
        struct.unpack_from("<I", body, 41)[0],
        body[45],
        struct.unpack_from("<I", body, 81)[0],
        {0: False, 170: True}.get(body[207]),
        # HCA session float * 100; firmware/capture evidence is documented in
        # docs/TCP_SESSION_ENERGY_DISCOVERY_20260923.md. Resets after Stop.
        session_energy_kwh=struct.unpack_from("<I", body, 85)[0] / 100,
    )


def _frame(command: int, body: bytes, identity: bytes, sequence: int) -> bytes:
    if len(identity) != 22 or type(sequence) is not int or not 0 <= sequence <= 255:
        raise ValueError("Invalid native session envelope")
    data = struct.pack("<H", command) + identity + body
    return (
        b"\xaa\xf5"
        + struct.pack("<HBB", len(data) + 7, 16, sequence)
        + data
        + bytes([sum(data) & 255])
    )


def acknowledgement(packet: NativeFrame) -> bytes | None:
    """Acknowledge registration/heartbeat/status; never retire stored bills."""
    if packet.command == 106 and len(packet.raw) == 122:
        command, body = 105, bytes(139)
    elif packet.command == 102 and len(packet.raw) == 69:
        command, body = 101, bytes(6)
    elif packet.command == 104 and len(packet.raw) == 239 and packet.raw[67] == 1:
        command, body = 103, bytes(3) + b"\x01"
    else:
        return None
    return _frame(command, body, packet.identity, packet.sequence)


def login(identity: bytes, sequence: int = 0) -> bytes:
    """Resume a verified device session without acknowledging billing data."""
    return _frame(105, bytes(139), identity, sequence)


def encode_command(
    action: str,
    identity: bytes,
    sequence: int,
    *,
    mode: int | None = None,
    tenths_kw: int | None = None,
    session_id: str = "",
    minimum_power: bool | None = None,
) -> bytes:
    """Encode an allowlisted operation with transport-specific values.

    Args:
        action: status, mode, power, minimum_power, start or stop.
        identity: Verified 22-byte session envelope.
        sequence: One-byte message sequence.
        mode: Native mode 0/1/2 for standalone parameter 48.
        tenths_kw: Requested power in tenths of kW; transport enforces model bounds.
        session_id: Unique ASCII charging-session identifier for Start.
        minimum_power: Permit minimum grid support in PV modes (parameter49).
    Returns:
        Checksummed native command bytes.
    Raises:
        ValueError: Unsupported action or values outside the validated scope.
    """
    if action == "status":
        command, body = 1, struct.pack("<HBBBIBHI", 0, 0, 1, 0, 47, 1, 4, 0)
    elif action == "mode":
        if type(mode) is not int or mode not in (0, 1, 2):
            raise ValueError("Native mode must be 0, 1 or 2")
        command, body = 1, struct.pack("<HBBBIBHI", 0, 0, 1, 1, 48, 1, 4, mode)
    elif action == "minimum_power":
        if type(minimum_power) is not bool:
            raise ValueError("Minimum power requires a boolean")
        command, body = 1, struct.pack("<HBBBIBHI", 0, 0, 1, 1, 49, 1, 4,
                                      170 if minimum_power else 0)
    elif action == "power":
        if type(tenths_kw) is not int or not 14 <= tenths_kw <= 220:
            raise ValueError(
                "Native power must be an integer from 14 to 220 tenths of kW"
            )
        command, body = 1, struct.pack("<HBBBIBHI", 0, 0, 1, 1, 47, 1, 4, tenths_kw)
    elif action == "stop":
        command, body = (
            5,
            struct.pack("<HHBBIBHI", 0, 0, 1, 1, 2, 1, 4, 0x55) + bytes(36),
        )
    elif action == "start":
        encoded = session_id.encode("ascii")
        if not 1 <= len(encoded) <= 31 or b"\0" in encoded:
            raise ValueError("Start needs a unique 1..31-byte ASCII session ID")
        command, body = (
            7,
            struct.pack(
                "<HHBIIII8sB32sBI32sH",
                0,
                0,
                1,
                0,
                0,
                0,
                0,
                bytes(8),
                0,
                b"HOME_ASSISTANT",
                0,
                0,
                encoded,
                0,
            ),
        )
    else:
        raise ValueError("Unsupported native command")
    return _frame(command, body, identity, sequence)
