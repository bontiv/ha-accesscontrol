#!/usr/bin/env python3
"""Standalone test tool that does not require Home Assistant.

Run it from any machine on the same network as the controller to validate the
wiring and firewall before installing the integration.

    python3 uhppote_cli.py discover
    python3 uhppote_cli.py open --host 192.168.1.50 --serial 223000123 --door 1
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

# Load api.py directly because the integration package imports Home Assistant,
# which is not required here.
_spec = importlib.util.spec_from_file_location(
    "uhppote_api",
    Path(__file__).resolve().parent.parent / "custom_components" / "ha-accesscontrol" / "api.py",
)
_api = importlib.util.module_from_spec(_spec)
sys.modules["uhppote_api"] = _api
_spec.loader.exec_module(_api)

UhppoteController = _api.UhppoteController
UhppoteError = _api.UhppoteError
discover = _api.discover


async def _discover(args: argparse.Namespace) -> int:
    found = await discover(
        broadcast_address=args.broadcast, timeout=args.timeout
    )
    if not found:
        print("No controller responded.", file=sys.stderr)
        return 1
    print(json.dumps([c.as_dict() for c in found], indent=2, ensure_ascii=False))
    return 0


async def _open(args: argparse.Namespace) -> int:
    controller = UhppoteController(
        args.host, args.serial, port=args.port, timeout=args.timeout
    )
    try:
        await controller.open_door(args.door)
    except UhppoteError as err:
        print(f"Failed: {err}", file=sys.stderr)
        return 1
    print(f"Door {args.door} opened on controller {args.serial}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="discover controllers")
    p_disc.add_argument("--broadcast", default="255.255.255.255")
    p_disc.add_argument("--timeout", type=float, default=3.0)
    p_disc.set_defaults(func=_discover)

    p_open = sub.add_parser("open", help="open a door")
    p_open.add_argument("--host", required=True)
    p_open.add_argument("--serial", type=int, required=True)
    p_open.add_argument("--door", type=int, default=1, choices=[1, 2, 3, 4])
    p_open.add_argument("--port", type=int, default=60000)
    p_open.add_argument("--timeout", type=float, default=2.5)
    p_open.set_defaults(func=_open)

    args = parser.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
