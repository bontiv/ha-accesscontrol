# Home Assistant Integration — UHPPOTE Access Controller

Custom integration for Wiegand TCP/IP access controllers using the UDP Short
Packet Format protocol (packet type `0x17`, driver >= 6.56), as described in the
manufacturer's *Short Packet Format / Extend Function V3* documentation.

Supported functions:

| Function | Code | Usage |
|---|---|---|
| Remote door opening | `0x40` | `button` entities + `ha_accesscontrol.open_door` service |
| Controller discovery | `0x94` | configuration flow + `ha_accesscontrol.discover` service |

## Why use an integration instead of `python_script`?

Home Assistant's [`python_script`](https://www.home-assistant.io/integrations/python_script/)
integration runs in a sandbox where imports are forbidden. The `socket` module
is therefore unavailable, making it impossible to send a UDP datagram from a
`python_script`. A custom integration has network and `asyncio` access and
integrates with the configuration UI.

## Installation

1. Copy `custom_components/ha_accesscontrol/` into the
   `config/custom_components/` directory of your Home Assistant installation:

   ```text
   config/
   └── custom_components/
       └── ha_accesscontrol/
           ├── __init__.py
           ├── api.py
           ├── button.py
           ├── config_flow.py
           ├── const.py
           ├── manifest.json
           ├── services.yaml
           ├── strings.json
           └── translations/
               ├── en.json
               └── fr.json
   ```

2. If you installed a prerelease copy under `custom_components/ha-accesscontrol/`,
   remove that old directory before reinstalling.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → UHPPOTE Access
   Controller**.
5. Choose **Search the network** (`0x94` broadcast) or **Enter the address
   manually** (IP address and the serial number printed on the enclosure).

Minimum Home Assistant version: **2024.11**.

## What the integration creates

For each controller, the integration creates one device with one button per
door:

- `button.controller_223000123_open_door_1`
- `button.controller_223000123_open_door_2`
- …

Pressing a button sends the `0x40` packet and displays an error in the UI if the
controller does not respond or refuses the command.

The number of doors (1-4) is selected when adding the controller. The timeout
and retry count can then be changed through **Configure** on the integration.

## Services

### `ha_accesscontrol.open_door`

```yaml
action: ha_accesscontrol.open_door
data:
  serial: 223000123  # optional when only one controller is configured
  door: 1
```

### `ha_accesscontrol.discover`

Returns a service response and requires `response_variable`:

```yaml
action: ha_accesscontrol.discover
data:
  timeout: 3
  broadcast_address: 192.168.1.255
response_variable: result
```

```json
{
  "controllers": [
    {
      "serial": 223000123,
      "ip_address": "192.168.1.50",
      "netmask": "255.255.255.0",
      "gateway": "192.168.1.1",
      "mac_address": "00:66:19:39:55:2d",
      "version": "6.56",
      "release_date": "2019-08-29"
    }
  ]
}
```

## Automation examples

Open door 1 from a physical Zigbee button:

```yaml
automation:
  - alias: "Open the entrance door"
    triggers:
      - trigger: device
        domain: zha
        type: remote_button_short_press
        subtype: button_1
        device_id: <your_button>
    actions:
      - action: button.press
        target:
          entity_id: button.controller_223000123_open_door_1
```

Open through the service:

```yaml
script:
  open_gate:
    alias: "Open the gate"
    sequence:
      - action: ha_accesscontrol.open_door
        data:
          serial: 223000123
          door: 2
        continue_on_error: true
```

Expose door opening to a voice assistant:

```yaml
script:
  open_the_door:
    alias: "Open the door"
    sequence:
      - action: button.press
        target:
          entity_id: button.controller_223000123_open_door_1
```

## Testing outside Home Assistant

`tools/uhppote_cli.py` exercises the same protocol without dependencies:

```bash
python3 tools/uhppote_cli.py discover
python3 tools/uhppote_cli.py open --host 192.168.1.50 --serial 223000123 --door 1
```

This is useful for validating the firewall and wiring before installation.

## Tests

```bash
python3 tests/test_api.py
```

The tests verify packet encoding against the documentation example
(`0x0D4AB63B` = 223000123) and exercise the client against a simulated
controller: accepted opening, refused opening, timeout with retries, ignored
response from another controller, and complete discovery.

## Releases

Run the **Release** workflow from `main` and provide a version such as `1.1.0`
or `1.1.0-beta.1`. Repository Actions must have write permission, and branch
protection must allow `github-actions[bot]` to push the release commit to
`main`.

## Protocol notes

Every packet is exactly 64 bytes and is sent over UDP port 60000:

```text
byte 0      packet type           0x17 [fixed]
byte 1      function identifier   0x40 (open) / 0x94 (search)
bytes 2-3   reserved              0x0000
bytes 4-7   serial number         32-bit little-endian integer
bytes 8-63  payload               padded with zeros
```

Open door (`0x40`): byte 8 of the request contains the door number (1-4); byte
8 of the response is `0x01` on success and `0x00` otherwise.

Discovery (`0x94`): the request is broadcast with a zero serial number. Each
controller responds with its IP address (bytes 8-11), netmask (12-15), gateway
(16-19), MAC address (20-25), BCD driver version (26-27), and BCD release date
(28-31).

## Troubleshooting

**No controller found during discovery.** Broadcast traffic does not cross
routers or most VLANs. Home Assistant must be on the same subnet as the
controller. In Docker, host networking is required for broadcast discovery.
Otherwise, try the directed broadcast address (`192.168.1.255`) or use manual
setup.

**No response from the controller.** Check the IP address and serial number—the
serial number must exactly match the one printed on the enclosure—and verify
that no firewall blocks UDP port 60000 in either direction.

**The controller refused the open-door command.** The controller responded but
rejected the command. Check the door control mode: remote opening is ignored in
normally closed mode. Also verify that the door number corresponds to a wired
door.
