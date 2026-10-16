# Home Assistant integration

> **This integration needs a running Sentinel CCTV server.** It does not record
> or connect to cameras on its own: it connects to a Sentinel server installed
> from this repository and brings its cameras into Home Assistant. Install and
> set up the server first ([installation](../README.md#installation)), then add
> the integration.

The integration lives in [`custom_components/sentinel`](../custom_components/sentinel).
Your cameras appear in Home Assistant with snapshots, motion sensors and
recording control. Nothing goes through a cloud service, and Home Assistant
never connects to the cameras directly.

## What you get

One device per camera, with:

| Entity | What it does |
| --- | --- |
| `camera.<name>` | Snapshots served by Sentinel |
| `binary_sensor.<name>_motion` | On the instant motion starts — pushed, not polled |
| `sensor.<name>_recording` | `recording`, `idle`, `off` or `unavailable` |
| `sensor.<name>_bitrate` | Current stream bitrate |
| `sensor.<name>_motion_events_total` | Lifetime motion event count |
| `switch.<name>_recording` | Turn continuous recording on or off |

Plus one device for the server itself: storage used, storage free, recordings
size, retention period, and how many cameras are online.

Motion also fires `sentinel_motion_started` and `sentinel_motion_ended` on the
Home Assistant event bus, if you would rather trigger on those.

## Requirements

- Home Assistant 2024.12 or newer
- A running Sentinel CCTV server reachable from Home Assistant
- An API token from that server

## Installing

### Through HACS

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/jencarnacao19/sentinel-cctv`, category
   **Integration**
3. Install **Sentinel CCTV**, then restart Home Assistant

HACS installs only the `custom_components/sentinel` directory. The server is
installed separately, as described in the main README.

### By hand

Copy `custom_components/sentinel` from this repository into your Home Assistant
`custom_components` directory and restart. The final layout must be:

```text
/config/custom_components/sentinel/__init__.py
/config/custom_components/sentinel/manifest.json
```

After copying or updating the directory, restart Home Assistant completely;
reloading YAML is not enough for Python integration code.

## Setting it up

Sentinel announces itself over mDNS, so it usually appears on its own under
**Settings → Devices & services** as a discovered device. If it does not — some
networks and container setups block multicast — add it manually with **Add
integration → Sentinel CCTV**.

Either way it asks for an API token. Create one in Sentinel:

**Settings → API tokens → Create token**, name it "Home Assistant".

The token is shown once and never again, so paste it straight into Home
Assistant.

## Put the complete Sentinel site in the Home Assistant sidebar

The integration above creates devices and entities. To make the complete
Sentinel interface available from one place in Home Assistant as well, add a
**Webpage dashboard**:

1. Open **Settings → Dashboards → Add dashboard**.
2. Choose **Webpage**, call it **Sentinel CCTV**, and enable **Add to sidebar**.
3. Enter the dedicated HTTPS embed address described below.
4. Leave **Admin-only** off if normal Home Assistant users should see it.

Current Sentinel releases display a restricted footage console at `/` and keep
all configuration below `/admin` behind the administrator login. Normal Home
Assistant users can watch live video, operate cameras whose ONVIF PTZ support
was detected by a Sentinel administrator, use playback, download footage and
create permanent saved clips. They cannot discover/add/edit cameras, change
recording or retention settings, manage tokens, delete exports or remove their
protection.

Keep the native integration local by configuring Sentinel's LAN URL, for
example `http://192.168.1.50:8090`. The HTTPS Home Assistant frontend cannot
embed that HTTP URL because browsers block mixed content. For the Webpage only,
use a path-specific Cloudflare Access application that bypasses `/embed*`,
`/assets/*`, `/sw.js`, `/api/v1/viewer/*` and `/api/v1/streams/*`. Sentinel's
viewer key remains mandatory, while the main hostname and `/admin` stay behind
the normal Cloudflare Access login. No AdGuard, router change or port-forward is
needed.

Sentinel supports a separate random operator key; store it in the private
deployment `.env` and put it only in the Webpage URL fragment:

```text
https://cctv.example.com/embed#viewer_key=<same random value>&embed=1
```

Set `SENTINEL_FRAME_ANCESTORS` to the exact Home Assistant HTTPS origin. The
fragment is not sent to the web server or Cloudflare. Sentinel exchanges it
for an `HttpOnly` viewer-session cookie. Current releases also use a restricted
request header and validated WebSocket subprotocol fallback so iOS/HA WebViews
continue working when they block third-party cookies. The key never appears in
HTTP or WebSocket URLs. Never commit it to Git or put it in Proxmox notes. The
native integration still uses its own API token; the viewer key cannot replace
that token.

The Webpage dashboard and the native integration complement each other:

- the dashboard provides live view, hold-to-move PTZ on compatible cameras,
  playback and permanent clip export without exposing camera/settings controls;
  `/admin` is available only after the Sentinel administrator login;
- the integration provides Home Assistant entities, automations and motion
  events.

### iPhone

No manual DNS setting is required. Open the Sentinel CCTV item inside the Home
Assistant app; the `/embed` route works over HTTPS, and the native integration
continues talking to Sentinel on the LAN. If the app has previously shown
“Viewer access is not authorised”, update Sentinel to the current release and
reload this dashboard; the viewer no longer relies solely on a cross-site
cookie accepted by Safari.

## Automation example

```yaml
alias: Motion at the front door
triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_motion
    to: "on"
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: Motion
      message: Someone is at the front door.
      data:
        entity_id: camera.front_door
```

The integration does not send notifications itself. Home Assistant already does
that better than a CCTV server could, and it already knows about your phones,
your presence detection and your schedules.

## How it stays in sync

The integration holds a WebSocket to Sentinel, so motion appears the moment it
is detected rather than on the next poll. If that connection drops — Sentinel
restarting, the network hiccuping — it reconnects with backoff and resumes from
the last event it saw, so nothing is missed in the gap. While the socket is down
it falls back to polling every 15 seconds so entities stay roughly current, and
relaxes again once push is back.

If Sentinel goes away entirely, entities become *unavailable* rather than
freezing on their last value, and recover on their own when it returns.

## When it will not connect

**"Could not reach the server."**
Check the address, and that Sentinel is actually running. From the Home
Assistant host: `curl http://<address>:8090/api/v1/system/health`.

**"That token was rejected."**
Tokens can be revoked in Sentinel, and are shown only once when created. Create
a fresh one. If an existing setup starts failing this way, Home Assistant
prompts to re-enter a token rather than making you delete and re-add
everything.

**"This Sentinel server is too old."**
The server answered but has no Home Assistant endpoint, or speaks a different
version of it. Update Sentinel.

**It was never discovered automatically.**
mDNS does not cross subnets, and some container network modes block multicast
entirely. Add it manually — nothing is lost by doing so.

**Why does the camera entity show snapshots rather than a live stream?**
The current Sentinel/Home Assistant contract intentionally exposes authenticated
snapshots but no Home Assistant stream URL. Live playback remains available in
the Sentinel web interface. PTZ controls also live in that embedded interface;
they are not separate Home Assistant entities or services. This is a current
product boundary, not a Home Assistant configuration error.

## Updating a manual installation

Replace `/config/custom_components/sentinel` with the directory from the new
release, restart Home Assistant and check **Settings → System → Logs**. Entity
IDs and the config entry are preserved across code updates.

To remove the integration, first delete it under **Settings → Devices &
services**, then remove the directory and restart. Removing only the files
leaves the config entry behind.

## Diagnostics

**Settings → Devices & services → Sentinel CCTV → Download diagnostics.**

Tokens, passwords and any RTSP URL carrying credentials are removed before the
file is written, so it is safe to attach to a GitHub issue.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r tests/home_assistant/requirements.txt
cd tests/home_assistant
../../.venv/bin/pytest
```

The tests run against a faked HTTP layer rather than a mocked client, so the
real client, coordinator and entity code all execute. The server's API contract
they are built against is
[`backend/app/integrations/CONTRACT.md`](../backend/app/integrations/CONTRACT.md).

**They only run on Linux.** The Home Assistant test harness depends on aiodns,
uvloop and AF_UNIX sockets, none of which work on Windows; the suite skips
itself there with an explanation. CI runs on Linux, which is where these are
actually verified.
