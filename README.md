# Home Assistant Integration — UHPPOTE Access Controller

Custom integration for Wiegand TCP/IP access controllers using the UDP Short
Packet Format protocol (packet type `0x17`, driver >= 6.56), as described in the
manufacturer's *Short Packet Format / Extend Function V3* documentation.

Supported functions:

| Function | Code | Usage |
|---|---|---|
| Controller status | `0x20` | `binary_sensor` and `sensor` entities (polled and pushed) |
| Set the clock | `0x30` | `ha_accesscontrol.sync_time` service |
| Read the clock | `0x32` | clock drift sensor |
| Remote door opening | `0x40` | `button` entities, `lock.open`, `ha_accesscontrol.open_door` |
| Set door control | `0x80` | `lock` entities + `ha_accesscontrol.set_door_mode` |
| Read door control | `0x82` | `lock` state and attributes |
| Set receiving server | `0x90` | push channel (`local_push`) |
| Read receiving server | `0x92` | push channel takeover check |
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

For each controller, the integration creates one device carrying, per door:

- `lock.controller_223000123_door_1` — the door control mode
- `button.controller_223000123_open_door_1` — a one-shot release
- `binary_sensor.<controller>_door_1_contact` — the door sensor (`device_class: door`)
- `binary_sensor.<controller>_door_1_relay` — the momentary relay (`device_class: lock`)
- `binary_sensor.<controller>_door_1_button` — the request-to-exit button (diagnostic)

and, for the controller itself:

- `binary_sensor.<controller>_fire_alarm` and `<controller>_forced_lock`
- `binary_sensor.<controller>_controller_error` (diagnostic)
- `sensor.<controller>_last_card`, `_last_door`, `_last_direction`,
  `_last_record_type`, `_last_event`
- `sensor.<controller>_last_record_index` and `_clock_drift` (diagnostic)

The number of doors is derived from the serial number — its leading digit is
1, 2 or 4 according to the model — and can be overridden through **Configure**,
along with the timeout, retry count, polling interval and push settings.

### What `lock` means here

The controller has two independent notions of "open":

- the **control mode** is persistent: normally open, normally closed, or
  controlled by cards, buttons and schedules;
- the **relay** is momentary and falls back after the configured open delay.

`lock.lock` and `lock.unlock` drive the *control mode*, so the entity state is
stable enough to use in automations. `lock.open` sends a one-shot `0x40` pulse
and leaves the mode alone. The momentary relay is published as its own binary
sensor, which is what flips for the three seconds of a badge read.

> **`lock.unlock` is persistent.** It holds the door normally open in the
> controller's own configuration, across a Home Assistant restart, until it is
> locked again.

## Real-time events

Every new record fires a `ha_accesscontrol_event` bus event:

```yaml
automation:
  - alias: "Announce a refused badge"
    triggers:
      - trigger: event
        event_type: ha_accesscontrol_event
        event_data:
          record_type: card
          granted: false
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            Card {{ trigger.event.data.card_number }} refused on door
            {{ trigger.event.data.door }}
            (reason {{ trigger.event.data.reason_code }})
```

### Push (`0x90`)

By default the integration polls function `0x20`. Enabling **Let the controller
push records** in the options registers Home Assistant as the controller's
receiving server, so records arrive as they happen and polling becomes a
watchdog only.

Two caveats before enabling it:

- **A controller stores a single receiving server.** Turning this on replaces
  whatever vendor software was registered, which will stop receiving events.
  The integration logs the previous value before taking over, and restores
  nothing on unload unless the controller still points at Home Assistant.
- **The UDP port must be reachable.** If Home Assistant runs in a container
  with bridge networking, publish the configured port (60002 by default) to the
  host, or no record will ever arrive.

## Services

### `ha_accesscontrol.open_door`

```yaml
action: ha_accesscontrol.open_door
data:
  serial: 223000123  # optional when only one controller is configured
  door: 1
```

### `ha_accesscontrol.set_door_mode`

```yaml
action: ha_accesscontrol.set_door_mode
data:
  serial: 223000123  # optional when only one controller is configured
  door: 1
  mode: controlled   # normally_open | normally_closed | controlled
  delay: 3           # optional, seconds; left unchanged when omitted
```

Function `0x80` writes the mode and the delay in the same packet, so the
integration reads the current delay back before writing to avoid clobbering it.

### `ha_accesscontrol.sync_time`

```yaml
action: ha_accesscontrol.sync_time
data:
  serial: 223000123  # optional when only one controller is configured
```

A drifting controller clock silently invalidates every event timestamp and time
profile, so the `clock drift` diagnostic sensor is worth an alert.

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

### Running the test suite

Development dependencies are declared as [PEP 735](https://peps.python.org/pep-0735/)
dependency groups in `pyproject.toml`, so no separate requirements file is
needed. With pip 25.1 or later:

```bash
python3 -m pip install --group test
python3 -m pytest
```

or, with [uv](https://docs.astral.sh/uv/):

```bash
uv run --group test pytest
```

The tests exercise `api.py` alone, which imports nothing beyond the standard
library, so they run on any supported Python version without Home Assistant
installed. The `integration` group additionally pulls in
`pytest-homeassistant-custom-component` for tests of the Home Assistant layer.

The status decoder is checked against the annotated packet captures printed in
the manufacturer's *Short Packet Format Examples* document (see
`tests/captures.py`), including an older firmware revision that leaves the
controller date (bytes 51-53) at zero.

## Security

The protocol has no authentication whatsoever: any host able to send a 64-byte
UDP datagram to the controller can open a door. Put these controllers on an
isolated VLAN, and treat the push port as untrusted input.

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
