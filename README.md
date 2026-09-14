# Home Assistant Integration — UHPPOTE Access Controller

Custom integration for Wiegand TCP/IP access controllers using the UDP Short
Packet Format protocol (packet type `0x17`, driver >= 6.56), as described in the
manufacturer's *Short Packet Format / Extend Function V3* documentation.

Supported functions:

| Function | Code | Usage |
|---|---|---|
| Controller status | `0x20` | `binary_sensor` and `sensor` entities (polled and pushed) |
| Set the clock | `0x30` | automatic daylight-saving resync, clock button, `ha_accesscontrol.sync_time` |
| Read the clock | `0x32` | clock drift sensor |
| Remote door opening | `0x40` | `lock.open` + `ha_accesscontrol.open_door` service |
| Set door control | `0x80` | `lock` entities + `ha_accesscontrol.set_door_mode` |
| Read door control | `0x82` | `lock` state and attributes |
| Set receiving server | `0x90` | push channel (`local_push`) |
| Read receiving server | `0x92` | push channel takeover check |
| Controller discovery | `0x94` | configuration flow + `ha_accesscontrol.discover` service |

## Installation

### HACS - Easy install with updates

Add this integration with HACS:

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bontiv&repository=ha-accesscontrol&category=integration)

### Manual installation

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
6. Pick the number of doors. The leading digit of the serial number identifies
   the model, so the matching option is already selected; change it if the
   controller drives fewer doors than it supports.

Minimum Home Assistant version: **2024.11**.

## What the integration creates

For each controller, the integration creates one device carrying, per door:

- `lock.controller_223000123_door_1` — the door control mode
- `binary_sensor.<controller>_door_1_contact` — the door sensor (`device_class: door`)
- `binary_sensor.<controller>_door_1_relay` — the momentary relay (`device_class: lock`)
- `binary_sensor.<controller>_door_1_button` — the request-to-exit button (diagnostic)
- `number.<controller>_door_1_open_delay` — the relay release duration (config)

and, for the controller itself:

- `binary_sensor.<controller>_fire_alarm` and `<controller>_forced_lock`
- `binary_sensor.<controller>_controller_error` (diagnostic)
- `sensor.<controller>_last_card`, `_last_door`, `_last_direction`,
  `_last_record_type`, `_last_event`
- `sensor.<controller>_last_record_index` and `_clock_drift` (diagnostic)

The number of doors is chosen when the controller is added, preselected from
the serial number — its leading digit is 1, 2 or 4 according to the model — and
can be changed later through **Configure**, along with the timeout, retry
count, polling interval, clock and push settings.

### What `lock` means here

The controller has two independent notions of "open":

- the **control mode** is persistent: normally open, normally closed, or
  controlled by cards, buttons and schedules;
- the **relay** is momentary and falls back after the configured open delay.

Home Assistant has a state for each, so both are visible on the one entity:

| State | Meaning |
|---|---|
| `locked` | normally closed, or controlled by cards, buttons and schedules |
| `unlocked` | held normally open by configuration, across restarts |
| `open` | the relay is released right now |

`lock.lock` and `lock.unlock` drive the *control mode*. `lock.unlock` holds
the door normally open; `lock.lock` returns it to **online control**, not to
normally closed—that mode ignores cards and refuses remote opening, which is a
lockdown rather than the everyday state, and it stays reachable only through
the `set_door_mode` action. `lock.open` sends a
one-shot `0x40` pulse and leaves the mode alone: the entity reads `open` for
the length of the pulse, then returns to `locked`. It never passes through
`unlocked`, which would wrongly suggest the door had been reconfigured to stay
open.

In normally-open mode the relay is energised permanently; that reads as
`unlocked`, not `open`, so the two situations stay distinguishable.

### What `open` is based on

**Source of the "open" state** in the options picks the signal:

| Source | `open` means | Good for |
|---|---|---|
| `relay` (default) | the controller released the strike | knowing the door was *authorised* to open, even if nobody went through |
| `door contact` | the magnetic contact reports the leaf ajar | knowing the door is *physically* open, including when it is held or propped |

Both signals stay readable as `relay` and `door_contact` attributes whichever
one drives the state, so an automation can use the other without changing the
setting.

> Pick `door contact` only if a contact is actually wired. The controller
> reports an unconnected input as *open*, so the lock would sit at `open`
> permanently.

The normally-open exception applies to the relay only: the contact follows the
leaf, not the configuration, so a door held open by configuration but shut
still reads `unlocked`.

To open a door in one tap from a dashboard, use the lock tile's built-in
feature rather than a separate entity:

```yaml
type: tile
entity: lock.controller_223000123_door_1
features:
  - type: lock-open-door
```

The pulse length is the door's **open delay**, readable and writable through the
`number` entity (function `0x82` / `0x80`, `entity_category: config`). Because
the integration knows that value, it schedules a state refresh just after the
relay is due to fall back, instead of leaving the relay sensor stale until the
next poll.

> Function `0x80` writes the control mode and the open delay in the same
> packet, so the field that is not being changed has to be sent along.
>
> The mode is read back from the controller, which always reports it
> faithfully. The delay is **not**: it only carries a meaning in online
> control mode, and a door held normally open reports it as zero. Reading it
> back there and resending it would write that zero into the configuration for
> good, leaving a relay that barely clicks. The integration therefore only
> learns the delay from a reading taken in online mode, and otherwise resends
> the last known good value.

> **`lock.unlock` is persistent.** It holds the door normally open in the
> controller's own configuration, across a Home Assistant restart, until it is
> locked again.

## The controller clock

The controllers store a plain wall clock. They have no timezone and **no
daylight-saving rules**, so at every transition their clock silently becomes
wrong by an hour, and every record they timestamp afterwards is wrong with it.

The integration therefore watches Home Assistant's own timezone and rewrites
the clock (`0x30`) when the local UTC offset changes. Enabled by default; turn
off **Keep the controller clock synchronised** in the options to manage it
yourself.

Three things happen:

- **At each transition.** The next change of the local UTC offset is computed
  from the configured timezone and a timer is armed for it. After it fires, the
  following one is armed. If the controller is unreachable at that moment the
  retry is ten minutes later, not six months.
- **At start-up.** If the clock is off by more than 60 seconds, it is rewritten
  straight away. This catches a transition that happened while Home Assistant
  was down, and ordinary drift. It needs firmware that reports its date
  (bytes 51-53 of the status packet); older units only get the scheduled resync.
- **When you change Home Assistant's timezone.** The timer is re-armed.

The `clock drift` diagnostic sensor carries two attributes:

| Attribute | Meaning |
|---|---|
| `dst_active` | whether daylight saving is currently in effect |
| `next_clock_sync` | when the next automatic resync is due, or `null` for a zone without daylight saving |

A zone with no daylight saving (UTC, Asia/Kolkata, …) arms no timer; only the
drift catch-up applies.

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
      - action: lock.open
        target:
          entity_id: lock.controller_223000123_door_1
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
      - action: lock.open
        target:
          entity_id: lock.controller_223000123_door_1
```

## Development container

`.devcontainer/` provides a ready-made environment for VS Code: **Dev
Containers: Reopen in Container** builds it, installs the `integration`
dependency group, and configures test discovery.

It pins **Python 3.12** deliberately — `pytest-homeassistant-custom-component`
does not install on 3.13 (see below) — so it is the simplest way to run the
whole suite on a machine that has a different Python.

Inside the container:

```bash
pytest                  # the whole suite
pytest tests/protocol   # protocol only
scripts/develop         # Home Assistant on http://localhost:8123
```

`scripts/develop` creates a throwaway Home Assistant configuration in
`config/` (git-ignored) and symlinks `custom_components/` into it, so the
running instance always reflects the working tree. Delete `config/` to start
over.

One limitation: the push channel (`0x90`) receives UDP, which Dev Containers
cannot forward. Test pushing against a real controller from a host install, or
run the container with host networking.

### Troubleshooting

**`is not a valid Windows path` when the container starts.** On Windows with
WSL installed, VS Code tries to bind-mount a Wayland socket from a WSL
distribution so that GUI applications work:

```
docker: Error response from daemon:
\wsl.localhost\<distro>\mnt\wslg\runtime-dir\wayland-0 is not a valid Windows path
```

Docker Desktop cannot bind-mount a UNC path, so the container never starts.
This is injected by the Dev Containers extension, not by `devcontainer.json`,
and cannot be disabled from this repository: the setting has `application`
scope, so it belongs in your VS Code **user** settings.

```json
"dev.containers.mountWaylandSocket": false
```

Nothing in this project needs a GUI inside the container.

## Testing outside Home Assistant

`tools/uhppote_cli.py` exercises the same protocol without dependencies:

```bash
python3 tools/uhppote_cli.py discover
python3 tools/uhppote_cli.py open --host 192.168.1.50 --serial 223000123 --door 1
```

### Running the test suite

Development dependencies are declared as [PEP 735](https://peps.python.org/pep-0735/)
dependency groups in `pyproject.toml`, so no separate requirements file is
needed. The suite is split in two:

| Package | Group | Covers |
|---|---|---|
| `tests/protocol/` | `test` | `api.py` alone: framing, BCD, status decoding, UDP exchanges, push listener |
| `tests/integration/` | `integration` | coordinator, entities, services, config flow, push wiring |

The protocol tests import nothing beyond the standard library, so they run on
any supported Python version with no Home Assistant installed:

```bash
python3 -m pip install --group test
python3 -m pytest
```

The Home Assistant tests need the heavier group, and **Python 3.12** --
`pytest-homeassistant-custom-component` pins `lru-dict` 1.3.0, which has no
wheel for 3.13 and does not build there:

```bash
python3 -m pip install --group integration
python3 -m pytest
```

Without that group, `tests/integration/` is skipped automatically, so the first
command always works. On a machine without Python 3.12, the container route
gives the same result:

```bash
docker run --rm -v "$PWD:/app" -w /app python:3.12-slim   bash -c 'pip install -q --upgrade "pip>=25.1" && pip install -q --group integration && pytest'
```

The status decoder is checked against the annotated packet captures printed in
the manufacturer's *Short Packet Format Examples* document (see
`tests/protocol/captures.py`), including an older firmware revision that leaves
the controller date (bytes 51-53) at zero.

## Security

The protocol has no authentication whatsoever: any host able to send a 64-byte
UDP datagram to the controller can open a door. Put these controllers on an
isolated VLAN, and treat the push port as untrusted input.

This is useful for validating the firewall and wiring before installation.

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
