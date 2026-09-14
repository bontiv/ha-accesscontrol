"""Packet captures copied verbatim from the manufacturer's documentation.

These come from the annotated examples in *Short Packet Format Examples V3*,
so the decoder is checked against real controller output rather than against
our own reading of the specification tables.
"""

from __future__ import annotations

from conftest import hexpkt

# Serial 0x0DB5851D = 229999901, a two-door controller.

# Firmware reporting its full date in bytes 51-53.
STATUS_RECENT = hexpkt(
    "17 20 00 00 1D 85 B5 0D 04 00 00 00 01 00 01 01"
    "0D D7 37 00 20 15 04 29 16 37 39 06 01 01 00 00"
    "00 00 00 00 00 16 42 25 00 00 00 00 00 00 00 00"
    "00 00 00 15 04 29 00 00 00 00 00 00 00 00 00 00"
)

# Older firmware: bytes 51-53 stay at zero, only the time of day is reported.
STATUS_OLD = hexpkt(
    "17 20 00 00 1D 85 B5 0D 04 00 00 00 01 00 01 01"
    "0D D7 37 00 20 13 04 03 21 09 24 06 01 01 00 00"
    "00 00 00 00 00 22 28 26 00 00 00 00 00 00 00 00"
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
)
