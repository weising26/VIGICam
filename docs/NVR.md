# VIGI NVR support

Add a VIGI NVR once and every camera recorded on it comes into Home Assistant, reached
through the NVR. You don't need direct access to each camera, so this also works for
cameras on an NVR's built-in PoE ports, where the cameras sit on the NVR's own subnet.

This uses TP-Link's official **VIGI NVR OpenAPI** (document v1.4). It is a different API
from the camera one the rest of this integration uses, so NVR entries have their own
entities and options.

## Two ways in

The integration picks one automatically when you add the NVR:

| | OpenAPI (port 20443) | Web interface (ports 443 + 8443) |
|---|---|---|
| Needs | Newer firmware with OpenAPI turned on | Any firmware the web page works on |
| Cameras, online status, live view, snapshots | Yes | Yes |
| Pushed detection events | Yes | Not yet |
| Arm/disarm, alarm mode, disks, reboot | Yes | Not yet |
| PTZ (pan/tilt buttons, presets, `vigicam.ptz` services) | Not yet | Yes |

It tries OpenAPI first. If the NVR doesn't answer on 20443, it logs in to the NVR's web
interface instead, the same way the NVR's own web page does. That route was worked out
from a VIGI NVR1008H(UN) 2.20 on firmware 1.1.2 (Build 240815), which has no OpenAPI.

On the web route, live view comes from the NVR's stream service on port 8443 (MPEG-TS
over a WebSocket). Home Assistant can't play that directly, so the integration relays it
inside Home Assistant as a plain stream, at a local address with a random token that
changes on every restart. Snapshots are one frame grabbed from that stream.

### PTZ on the web route

For each channel whose camera reports pan/tilt, you get **Pan left**, **Pan right**,
**Tilt up** and **Tilt down** buttons (each press nudges the camera for half a second)
and a **PTZ preset** select listing the presets saved on the NVR. The integration's
existing services work on those channels too, targeting the channel's camera entity:

```yaml
action: vigicam.ptz
data:
  entity_id: camera.front_door
  direction: left        # left, right, up, down, zoom_in, zoom_out
  speed: 0.6             # 0.1 to 1.0
  duration: 1.5          # seconds, then it stops
```

`vigicam.ptz_stop` and `vigicam.goto_preset` (by preset name) work the same way.

## Before you start (OpenAPI route)

1. Check that your NVR is on TP-Link's
   [Devices Supported by VIGI Open API](https://www.tp-link.com/en/vigi-open-api/product-list/)
   list, and update it to the latest firmware.
2. In the NVR's web interface go to **Settings > Network > OpenAPI**, enable it, and
   leave the port at **20443**.
3. Leave RTSP enabled (port 554) with **RTSPS and SRTP off**, so Home Assistant can play
   the streams.

## Add the NVR

**Settings > Devices & Services > Add Integration > VIGI & InSight Cameras > NVR**, then
enter the NVR's IP address and admin username and password.

You get one **NVR device**, plus one device per channel linked to it ("via" the NVR).

## What you get

| Where | Entity | Notes |
|---|---|---|
| Each channel | Camera | Live view over RTSP from the NVR (`/live/<ch>/<stream>/avm`). Snapshots come from the NVR's snapshot API. Sub-stream by default, with the main stream as an option. |
| Each channel | Online | Connectivity, from the NVR's channel list. |
| Each channel | Motion, Person Detected, Vehicle Detected, Line Crossing, Intrusion | Pushed by the NVR (see below). |
| Each channel | Tamper, Area Entry, Area Exit, Loitering, Crowd Gathering, Alarm Input | Same, disabled by default. |
| Each channel | Active defense (switch) | Only for cameras that report arm/disarm support. |
| Each channel | Trigger alarm output (button) | Disabled by default. |
| NVR | Alarm mode (switch) | On = `alarm`, off = `disarming`. |
| NVR | Disk N free space / capacity / status | One set per disk. |
| NVR | Cameras online | Count, with the total channel count as an attribute. |
| NVR | PoE power used | PoE models only. |
| NVR | Reboot, Trigger alarm output | The alarm button is disabled by default. |

Add or remove a camera on the NVR and the entry reloads by itself to match.

## Detection events

The NVR OpenAPI has no event stream to poll: the NVR **pushes** each event to an
"event server". The NVR has three event-server slots. On setup the integration:

- registers a local-only Home Assistant webhook,
- reuses the slot that already points at it, or else takes the first **empty** slot,
- never overwrites a slot that something else is using. If all three are taken, a
  Repairs notice tells you so.

The slot is released when you delete the NVR entry.

The NVR sends one message per detection and no "ended" message, so each event sensor
turns off after a hold time (15 s by default; change it in the NVR's options).

Every pushed message is also fired on the event bus as **`vigicam_nvr_event`**, including
codes with no entity (disk full, PoE overload, IP conflict, and so on):

```yaml
trigger:
  - platform: event
    event_type: vigicam_nvr_event
    event_data:
      channel: 2
action:
  - service: notify.mobile_app_phone
    data:
      message: "{{ trigger.event.data.events }} on {{ trigger.event.data.channel_name }}"
```

Event data: `entry_id`, `nvr`, `channel`, `channel_name`, `type`, `sub_types`, `events`
(for example `["line_cross", "person"]`), `exception` (for example `disk_full`), `localtime`.

### If events don't arrive

- The NVR posts to Home Assistant's **internal URL** (Settings > System > Network). If
  the NVR can't reach that address (Docker with a bridge network, for example), set
  **Address the NVR should send events to** in the NVR's options, such as
  `192.168.1.10:8123`.
- Check the event-server list on the NVR: one slot should point at
  `/api/webhook/<id>` on your Home Assistant address.
- Turn on debug logging for `custom_components.vigicam` to see every push as it arrives.

## Options

- Use the main (HD) stream for live view
- Receive detection events from the NVR
- Seconds an event sensor stays on after the last event
- Address the NVR should send events to

## Limits and unknowns

The NVR document leaves some things unclear, and this was written from the document
rather than against every firmware:

- **PTZ** is in the API, but the document doesn't define the `direction` codes, so PTZ
  isn't exposed yet. `VIGINVR.ptz_move()` is there for when they're known.
- The document gives two different names for the push's multipart boundary and device
  fields. The parser accepts both.
- The NVR reports no model or firmware through the OpenAPI, so the NVR device shows
  "VIGI NVR".
- Two-way audio (talk) and recording playback aren't implemented.

## Testing

`tests/test_nvr_api.py` runs the client against a fake HTTPS NVR that enforces the
digest login. It runs in CI with the rest of `tests/`.

`tests_ha/` runs the NVR entry inside Home Assistant (config flow, setup, entities, a
pushed event over the webhook, slot handling, reload on channel change). It needs
`pytest-homeassistant-custom-component`:

```bash
pip install pytest-homeassistant-custom-component av ha-ffmpeg PyTurboJPEG
pytest tests_ha -o asyncio_mode=auto
```
