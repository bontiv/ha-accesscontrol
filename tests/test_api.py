"""Protocol tests using a simulated controller."""

from __future__ import annotations

import asyncio
import struct
import sys

import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "uhppote_api",
    _Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ha-accesscontrol"
    / "api.py",
)
_api = _ilu.module_from_spec(_spec)
sys.modules["uhppote_api"] = _api
_spec.loader.exec_module(_api)

FUNC_OPEN_DOOR = _api.FUNC_OPEN_DOOR
FUNC_SEARCH = _api.FUNC_SEARCH
ControllerInfo = _api.ControllerInfo
UhppoteController = _api.UhppoteController
UhppoteRefused = _api.UhppoteRefused
UhppoteTimeout = _api.UhppoteTimeout
build_packet = _api.build_packet
parse_packet = _api.parse_packet

SERIAL = 223000123  # 0x0D4AB63B, the documentation example


# -------------------------------------------------------------------- packets


def test_build_matches_documentation() -> None:
    """The documentation gives 0x0D4AB63B for serial number 223000123."""
    pkt = build_packet(FUNC_OPEN_DOOR, SERIAL, bytes([1]))
    assert len(pkt) == 64
    assert pkt[0] == 0x17  # type
    assert pkt[1] == 0x40  # function identifier
    assert pkt[2] == 0x00 and pkt[3] == 0x00  # reserved
    assert pkt[4:8] == bytes([0x3B, 0xB6, 0x4A, 0x0D])  # serial, little-endian
    assert pkt[8] == 0x01  # door number
    assert pkt[9:] == bytes(55)  # padded with zeros
    print("  open-door packet:", pkt[:10].hex(" "))


def test_roundtrip() -> None:
    pkt = build_packet(0x94, 0)
    func, serial, payload = parse_packet(pkt)
    assert (func, serial) == (0x94, 0)
    assert payload == bytes(56)


def test_discovery_payload_decoding() -> None:
    payload = bytearray(56)
    payload[0:4] = bytes([192, 168, 1, 50])
    payload[4:8] = bytes([255, 255, 255, 0])
    payload[8:12] = bytes([192, 168, 1, 1])
    payload[12:18] = bytes([0x00, 0x66, 0x19, 0x39, 0x55, 0x2D])
    payload[18:20] = bytes([0x06, 0x56])  # version 6.56 (BCD)
    payload[20:24] = bytes([0x20, 0x19, 0x08, 0x29])  # 2019-08-29 (BCD)

    info = ControllerInfo.from_payload(SERIAL, bytes(payload))
    assert info.ip_address == "192.168.1.50"
    assert info.netmask == "255.255.255.0"
    assert info.gateway == "192.168.1.1"
    assert info.mac_address == "00:66:19:39:55:2d"
    assert info.version == "6.56"
    assert info.release_date == "2019-08-29"
    print("  decoded discovery response:", info)


# ------------------------------------------------------ simulated controller


class FakeController(asyncio.DatagramProtocol):
    """Respond like a real controller on an ephemeral port."""

    def __init__(self, serial: int, *, grant: bool = True, silent: bool = False):
        self.serial = serial
        self.grant = grant
        self.silent = silent
        self.received: list[bytes] = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.append(data)
        if self.silent:
            return
        func, serial, payload = parse_packet(data)
        reply = bytearray(64)
        reply[0] = 0x17
        reply[1] = func
        struct.pack_into("<I", reply, 4, self.serial)
        if func == FUNC_OPEN_DOOR:
            reply[8] = 0x01 if self.grant else 0x00
        elif func == FUNC_SEARCH:
            reply[8:12] = bytes([127, 0, 0, 1])
            reply[12:16] = bytes([255, 255, 255, 0])
            reply[16:20] = bytes([127, 0, 0, 1])
            reply[20:26] = bytes([0x00, 0x66, 0x19, 0x39, 0x55, 0x2D])
            reply[26:28] = bytes([0x06, 0x56])
            reply[28:32] = bytes([0x20, 0x19, 0x08, 0x29])
        self.transport.sendto(bytes(reply), addr)


async def _start_fake(**kwargs):
    loop = asyncio.get_running_loop()
    proto = FakeController(SERIAL, **kwargs)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: proto, local_addr=("127.0.0.1", 0)
    )
    return transport, proto, transport.get_extra_info("sockname")[1]


async def test_open_door_success() -> None:
    transport, proto, port = await _start_fake()
    try:
        ctrl = UhppoteController("127.0.0.1", SERIAL, port=port, timeout=1.0)
        await ctrl.open_door(2)
    finally:
        transport.close()
    assert len(proto.received) == 1
    assert proto.received[0][1] == 0x40
    assert proto.received[0][8] == 2
    print("  door opening accepted, 1 packet sent")


async def test_open_door_refused() -> None:
    transport, proto, port = await _start_fake(grant=False)
    try:
        ctrl = UhppoteController("127.0.0.1", SERIAL, port=port, timeout=1.0)
        try:
            await ctrl.open_door(1)
        except UhppoteRefused:
            print("  refusal reported correctly")
        else:
            raise AssertionError("a refusal should have been raised")
    finally:
        transport.close()


async def test_timeout_and_retries() -> None:
    transport, proto, port = await _start_fake(silent=True)
    try:
        ctrl = UhppoteController(
            "127.0.0.1", SERIAL, port=port, timeout=0.3, retries=2
        )
        try:
            await ctrl.open_door(1)
        except UhppoteTimeout:
            pass
        else:
            raise AssertionError("a timeout should have been raised")
    finally:
        transport.close()
    assert len(proto.received) == 3, f"expected 3 sends, received {len(proto.received)}"
    print("  timeout: 3 sends (1 initial attempt + 2 retries)")


async def test_wrong_serial_ignored() -> None:
    transport, proto, port = await _start_fake()
    proto.serial = 999999  # respond with a different serial number
    try:
        ctrl = UhppoteController(
            "127.0.0.1", SERIAL, port=port, timeout=0.3, retries=0
        )
        try:
            await ctrl.open_door(1)
        except UhppoteTimeout:
            print("  response from another controller ignored correctly")
        else:
            raise AssertionError("the response should have been ignored")
    finally:
        transport.close()


async def test_invalid_door() -> None:
    ctrl = UhppoteController("127.0.0.1", SERIAL)
    for bad in (0, 5, -1):
        try:
            await ctrl.open_door(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"door {bad} should have been rejected")
    print("  out-of-range door numbers rejected")


async def test_discover_roundtrip() -> None:
    """Ensure discovery sends a 0x94 packet and decodes the response."""
    transport, proto, port = await _start_fake()
    try:
        found = await _api.discover(
            broadcast_address="127.0.0.1", port=port, timeout=0.6
        )
    finally:
        transport.close()

    assert proto.received[0][1] == 0x94
    assert proto.received[0][4:8] == bytes(4), (
        "the request must contain a zero serial number"
    )
    assert len(found) == 1
    assert found[0].serial == SERIAL
    assert found[0].ip_address == "127.0.0.1"
    assert found[0].version == "6.56"
    print("  end-to-end discovery:", found[0].serial, found[0].ip_address)


async def main() -> None:
    print("Packets:")
    test_build_matches_documentation()
    test_roundtrip()
    test_discovery_payload_decoding()
    print("Network exchanges:")
    await test_open_door_success()
    await test_open_door_refused()
    await test_timeout_and_retries()
    await test_wrong_serial_ignored()
    await test_discover_roundtrip()
    print("Input validation:")
    await test_invalid_door()
    print("\nAll tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
