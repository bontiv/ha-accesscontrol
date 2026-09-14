"""Decoding of the controller status packet (function 0x20)."""

from __future__ import annotations

from datetime import datetime

import pytest
import uhppote_api as api
from captures import STATUS_OLD, STATUS_RECENT
from conftest import DOC_SERIAL, SERIAL


def test_decodes_documented_capture() -> None:
    _ptype, _func, serial, payload = api.parse_packet(STATUS_RECENT)
    assert serial == DOC_SERIAL

    status = api.ControllerStatus.from_payload(serial, payload)

    assert status.last_record_index == 4
    assert status.record_type == api.RECORD_CARD
    assert status.granted is False
    assert status.door == 1
    assert status.direction == 1  # in
    assert status.card_number == 3659533  # 0x0037D70D
    assert status.event_time == datetime(2015, 4, 29, 16, 37, 39)
    assert status.reason_code == 6
    assert status.door_sensors == (True, True, False, False)
    assert status.buttons == (False, False, False, False)
    assert status.error_code == 0
    assert status.relays == (False, False, False, False)
    assert status.fire is False
    assert status.forced_lock is False
    assert status.controller_time == datetime(2015, 4, 29, 16, 42, 25)
    assert status.has_event is True


def test_tolerates_firmware_without_date() -> None:
    """Bytes 51-53 are absent on older firmware and must not be guessed."""
    _ptype, _func, serial, payload = api.parse_packet(STATUS_OLD)

    status = api.ControllerStatus.from_payload(serial, payload)

    assert status.event_time == datetime(2013, 4, 3, 21, 9, 24)
    assert status.controller_time is None


def test_decodes_bitfields_and_offsets() -> None:
    payload = bytearray(56)
    payload[6] = 3  # door
    payload[7] = 2  # out
    payload[20:24] = bytes([0, 0, 1, 1])  # door sensors 3 and 4 open
    payload[24:28] = bytes([1, 0, 1, 0])  # buttons 1 and 3 pressed
    payload[28] = 7  # error code
    payload[41] = 0b0000_1010  # relays 2 and 4 released
    payload[42] = 0b0000_0011  # forced lock + fire

    status = api.ControllerStatus.from_payload(SERIAL, bytes(payload))

    assert (status.door, status.direction) == (3, 2)
    assert status.door_sensors == (False, False, True, True)
    assert status.buttons == (True, False, True, False)
    assert status.relays == (False, True, False, True)
    assert status.error_code == 7
    assert status.has_event is False, "record type 0 means 'no record'"


@pytest.mark.parametrize(
    ("flags", "forced_lock", "fire"),
    [(0x00, False, False), (0x01, True, False), (0x02, False, True), (0x03, True, True)],
)
def test_separates_fire_from_forced_lock(
    flags: int, forced_lock: bool, fire: bool
) -> None:
    """Byte 50 carries the forced lock in bit 0 and the fire alarm in bit 1."""
    payload = bytearray(56)
    payload[42] = flags

    status = api.ControllerStatus.from_payload(SERIAL, bytes(payload))

    assert status.forced_lock is forced_lock
    assert status.fire is fire


@pytest.mark.parametrize(
    ("record_type", "has_event"),
    [
        (api.RECORD_NONE, False),
        (api.RECORD_CARD, True),
        (api.RECORD_DOOR, True),
        (api.RECORD_ALARM, True),
        (api.RECORD_OVERWRITTEN, False),
    ],
)
def test_has_event(record_type: int, has_event: bool) -> None:
    payload = bytearray(56)
    payload[4] = record_type

    assert api.ControllerStatus.from_payload(SERIAL, bytes(payload)).has_event is (
        has_event
    )


def test_rejects_short_payload() -> None:
    with pytest.raises(api.UhppoteProtocolError):
        api.ControllerStatus.from_payload(SERIAL, bytes(20))


def test_survives_corrupt_event_timestamp() -> None:
    """A bad BCD timestamp must not discard the rest of the packet."""
    payload = bytearray(56)
    payload[4] = api.RECORD_CARD
    payload[6] = 3
    payload[12:19] = bytes([0xFF] * 7)

    status = api.ControllerStatus.from_payload(SERIAL, bytes(payload))

    assert status.event_time is None
    assert status.door == 3


def test_survives_corrupt_controller_clock() -> None:
    payload = bytearray(56)
    payload[29:32] = bytes([0xFF, 0xFF, 0xFF])
    payload[43:46] = bytes([0x15, 0x04, 0x29])

    status = api.ControllerStatus.from_payload(SERIAL, bytes(payload))

    assert status.controller_time is None


def test_as_dict_is_json_friendly() -> None:
    import json

    _ptype, _func, serial, payload = api.parse_packet(STATUS_RECENT)
    data = api.ControllerStatus.from_payload(serial, payload).as_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["event_time"] == "2015-04-29T16:37:39"
    assert data["door_sensors"] == [True, True, False, False]
