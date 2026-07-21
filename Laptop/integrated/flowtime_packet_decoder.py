#!/usr/bin/env python3
"""Decode Flowtime's 20-byte raw EEG notification.

Validated against ``raw_eeg_20260716_230100.jsonl``:

* bytes 0..1: unsigned big-endian 16-bit packet sequence
* bytes 2..19: three time samples
* each time sample: left signed 24-bit big-endian, then right signed 24-bit

The decoder intentionally returns ADC counts.  Relative spectral power does
not require a proprietary voltage scale, and detrending removes the large DC
offset before the FFT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DecodedEEGPacket:
    sequence: int
    left: tuple[int, int, int]
    right: tuple[int, int, int]


def _signed_int24(data: list[int], offset: int) -> int:
    value = (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
    if value & 0x800000:
        value -= 1 << 24
    return value


def decode_eeg_packet(packet: Iterable[int]) -> DecodedEEGPacket:
    data = [int(value) for value in packet]
    if len(data) != 20:
        raise ValueError(f"Flowtime EEG packet must contain 20 bytes, got {len(data)}")
    if any(value < 0 or value > 255 for value in data):
        raise ValueError("Flowtime EEG packet contains a value outside byte range")

    sequence = (data[0] << 8) | data[1]
    left: list[int] = []
    right: list[int] = []
    for offset in (2, 8, 14):
        left.append(_signed_int24(data, offset))
        right.append(_signed_int24(data, offset + 3))
    return DecodedEEGPacket(sequence, tuple(left), tuple(right))


def sequence_distance(previous: int, current: int) -> int:
    """Return the forward 16-bit sequence distance, including wrap-around."""
    return (current - previous) & 0xFFFF
