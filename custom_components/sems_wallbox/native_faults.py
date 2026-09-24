"""Passive original-HCA fault details; reference labels are not full diagnoses.

See docs/NATIVE_FAULT_CODES.md for firmware evidence and omitted conditions.
These reports must never authorize charging or override the numeric status code.
"""

from dataclasses import dataclass

from .native_protocol import NativeFrame, check_serial

_REFERENCE_LABELS = {
    33: "stop", 38: "earth_err", 41: "env_temp_over", 42: "gun_temp_over",
    45: "relay_err", 48: "vol_over", 49: "vol_less", 54: "curr_over",
    57: "meter_comm_err", 66: "leak_curr", 67: "curr_less", 68: "meter_fault",
}
_CONDITION_BITS = {32, 33, 34, 38, 41, 42, 43, 45, 48, 49, 54, 55, 57, 58,
                   64, 65, 66, 67, 68, 69, 70}
_AUXILIARY_BITS = {72, 73, 74, 75, 76}


@dataclass(frozen=True)
class NativeFaultReport:
    """Allowlisted observations without serial, envelope or arbitrary text."""

    reference_labels: tuple[str, ...]
    unlabelled_condition_bits: tuple[int, ...]
    auxiliary_bits: tuple[int, ...]
    unknown_bits: tuple[int, ...]


def decode_fault_report(packet: NativeFrame, serial: str) -> NativeFaultReport:
    """Decode an optional report already verified by NativeDecoder.

    Args:
        packet: Checksummed frame from the enrolled native connection.
        serial: Configured device serial, checked independently of the envelope.

    Returns:
        Reference interpretations and all unexplained set bits.

    Raises:
        ValueError: Unexpected command, shape, serial, connector or marker.
    """
    raw = packet.raw
    if (packet.command != 108 or len(raw) != 99 or raw[33] != 1
            or raw[34:66].rstrip(b"\0") != check_serial(serial)):
        raise ValueError("Unexpected native fault report identity or layout")
    block = raw[66:98]
    if block[:4] != b"\x00\x00\x00\x01":
        raise ValueError("Unknown native fault report marker")
    bits = {bit for bit in range(256) if block[bit // 8] & (1 << (bit % 8))}
    return NativeFaultReport(
        tuple(_REFERENCE_LABELS[bit] for bit in sorted(bits & _REFERENCE_LABELS.keys())),
        tuple(sorted((bits & _CONDITION_BITS) - _REFERENCE_LABELS.keys())),
        tuple(sorted(bits & _AUXILIARY_BITS)),
        tuple(sorted(bits - ({24} | _CONDITION_BITS | _AUXILIARY_BITS))),
    )
