"""Packet encoding, decoding and BCD helpers."""

from __future__ import annotations

from datetime import datetime

import pytest
import uhppote_api as api
from conftest import SERIAL

# ------------------------------------------------------------------ framing


def test_build_matches_documentation() -> None:
    """The documentation gives 0x0D4AB63B for serial number 223000123."""
    pkt = api.build_packet(api.FUNC_OPEN_DOOR, SERIAL, bytes([1]))

    assert len(pkt) == 64
    assert pkt[0] == 0x17  # type
    assert pkt[1] == 0x40  # function identifier
    assert pkt[2:4] == bytes(2)  # reserved
    assert pkt[4:8] == bytes([0x3B, 0xB6, 0x4A, 0x0D])  # serial, little-endian
    assert pkt[8] == 0x01  # door number
    assert pkt[9:] == bytes(55)  # padded with zeros


def test_build_writes_sequence() -> None:
    pkt = api.build_packet(api.FUNC_OPEN_DOOR, SERIAL, bytes([1]), sequence=0x01020304)

    assert pkt[40:44] == bytes([0x04, 0x03, 0x02, 0x01])
    assert api.payload_sequence(pkt[8:]) == 0x01020304


@pytest.mark.parametrize(
    ("payload", "sequence", "reason"),
    [
        (bytes(57), 0, "payload longer than the packet"),
        (bytes(33), 1, "payload overlapping the sequence number"),
    ],
)
def test_build_rejects_bad_payload(payload: bytes, sequence: int, reason: str) -> None:
    with pytest.raises(ValueError):
        api.build_packet(api.FUNC_OPEN_DOOR, SERIAL, payload, sequence=sequence)


@pytest.mark.parametrize("serial", [-1, 0x1_0000_0000])
def test_build_rejects_out_of_range_serial(serial: int) -> None:
    with pytest.raises(ValueError):
        api.build_packet(api.FUNC_OPEN_DOOR, serial)


def test_roundtrip() -> None:
    ptype, func, serial, payload = api.parse_packet(api.build_packet(0x94, 0))

    assert (ptype, func, serial) == (0x17, 0x94, 0)
    assert payload == bytes(56)


def test_parse_accepts_event_type() -> None:
    """Records pushed by the controller use type 0x19."""
    raw = bytearray(api.build_packet(api.FUNC_STATUS, SERIAL))
    raw[0] = api.PACKET_TYPE_EVENT

    ptype, func, serial, _payload = api.parse_packet(bytes(raw))

    assert (ptype, func, serial) == (0x19, 0x20, SERIAL)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (bytes(63), "too short"),
        (bytes(65), "too long"),
        (b"", "empty"),
        (bytes([0x18]) + bytes(63), "unknown type"),
    ],
)
def test_parse_rejects_bad_packets(raw: bytes, reason: str) -> None:
    with pytest.raises(api.UhppoteProtocolError):
        api.parse_packet(raw)


# ---------------------------------------------------------------------- BCD


@pytest.mark.parametrize(
    ("bcd", "value"),
    [(0x00, 0), (0x13, 13), (0x20, 20), (0x59, 59), (0x99, 99)],
)
def test_bcd_roundtrip(bcd: int, value: int) -> None:
    """The documentation works through 0x59 -> 59 and 2013 -> 0x20 0x13."""
    assert api.bcd_to_int(bcd) == value
    assert api.int_to_bcd(value) == bcd


@pytest.mark.parametrize("bad", [0x0A, 0xA0, 0xFF, 0x1F])
def test_bcd_to_int_rejects_invalid_nibbles(bad: int) -> None:
    with pytest.raises(ValueError):
        api.bcd_to_int(bad)


@pytest.mark.parametrize("bad", [-1, 100, 255])
def test_int_to_bcd_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError):
        api.int_to_bcd(bad)


def test_bcd_datetime_roundtrip() -> None:
    moment = datetime(2015, 4, 29, 21, 48, 0)

    raw = api.build_bcd_datetime(moment)

    assert raw == bytes([0x20, 0x15, 0x04, 0x29, 0x21, 0x48, 0x00])
    assert api.parse_bcd_datetime(raw) == moment


def test_bcd_datetime_all_zero_means_unset() -> None:
    assert api.parse_bcd_datetime(bytes(7)) is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (bytes([0x20, 0x15, 0x99, 0x29, 0x21, 0x48, 0x00]), "month 99"),
        (bytes([0x20, 0x15, 0x04, 0x32, 0x21, 0x48, 0x00]), "day 32"),
        (bytes(6), "too few bytes"),
        (bytes(8), "too many bytes"),
    ],
)
def test_bcd_datetime_rejects_invalid(raw: bytes, reason: str) -> None:
    with pytest.raises(ValueError):
        api.parse_bcd_datetime(raw)


# ----------------------------------------------------------------- payloads


def test_discovery_payload_decoding() -> None:
    payload = bytearray(56)
    payload[0:4] = bytes([192, 168, 1, 50])
    payload[4:8] = bytes([255, 255, 255, 0])
    payload[8:12] = bytes([192, 168, 1, 1])
    payload[12:18] = bytes([0x00, 0x66, 0x19, 0x39, 0x55, 0x2D])
    payload[18:20] = bytes([0x06, 0x56])  # version 6.56 (BCD)
    payload[20:24] = bytes([0x20, 0x19, 0x08, 0x29])  # 2019-08-29 (BCD)

    info = api.ControllerInfo.from_payload(SERIAL, bytes(payload))

    assert info.ip_address == "192.168.1.50"
    assert info.netmask == "255.255.255.0"
    assert info.gateway == "192.168.1.1"
    assert info.mac_address == "00:66:19:39:55:2d"
    assert info.version == "6.56"
    assert info.release_date == "2019-08-29"


@pytest.mark.parametrize(
    ("serial", "doors"),
    [
        (123000123, 1),  # single door, bidirectional
        (223000123, 2),  # two doors
        (423000123, 4),  # four doors
        (923000123, 4),  # unknown prefix falls back to the most capable model
        (0, 4),
    ],
)
def test_doors_from_serial(serial: int, doors: int) -> None:
    """The leading decimal digit of the serial number identifies the model."""
    assert api.doors_for_serial(serial) == doors


def test_controller_info_exposes_door_count() -> None:
    assert api.ControllerInfo.from_payload(223000123, bytes(56)).doors == 2


def test_receiver_payload_matches_documentation() -> None:
    """The example sets 192.168.168.101:61005 with a 5 s heartbeat."""
    payload = (
        api._parse_ip("192.168.168.101") + (61005).to_bytes(2, "little") + bytes([5])
    )

    assert payload.hex(" ") == "c0 a8 a8 65 4d ee 05"


@pytest.mark.parametrize(
    "bad", ["192.168.1", "192.168.1.1.1", "192.168.1.300", "", "not.an.ip.addr"]
)
def test_parse_ip_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        api._parse_ip(bad)
