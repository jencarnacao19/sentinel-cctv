# Home Assistant integration contract

This is the interface the Home Assistant integration in
[`custom_components/sentinel`](../../../custom_components/sentinel) builds
against. It is written from the server side but describes exactly what the
integration should call and what it will receive. If the two disagree, **this
file is wrong and should be fixed** — the integration's client
(`custom_components/sentinel/api.py`, `models.py`, `coordinator.py`) was built
against an earlier draft of this contract, and the shapes below were checked
against that code so the two line up.

Everything here lives under the existing `/api/v1` prefix and the existing
authentication scheme (Argon2id sessions for the browser, revocable
`sctv_...` API tokens for machines) — nothing new was introduced for either.
Create a token for Home Assistant the same way as for anything else:
`POST /api/v1/auth/tokens` while signed in as an administrator, `{"name":
"Home Assistant", "scope": "camera_control"}`. The plaintext is shown once; paste it into the
integration's config flow.

---

## 1. Authentication

Every endpoint below requires a caller to be authenticated. Three ways in,
resolved in this order (`app.auth.deps.get_optional_principal`, unchanged by
this phase):

1. `Authorization: Bearer sctv_xxx` header. **This is what Home Assistant
   should use for both REST and the WebSocket** — a header never ends up in
   an access log the way a query string does.
2. The `sentinel_session` cookie (browser only; `HttpOnly`, set by
   `/auth/login`).
3. For a WebSocket handshake specifically (browsers cannot set custom
   headers on a `new WebSocket(...)` upgrade), a `?token=sctv_xxx` query
   parameter is also accepted. Home Assistant does not need this path since
   its aiohttp client can set headers, but it exists for the web UI and any
   client that cannot.

API tokens never have administrator privileges. New tokens default to
`read_only`: camera lists, snapshots, live/playback media, events, system
telemetry and the existing storage/retention reads. Choose `camera_control`
for HA recording switches and camera tests; it also permits manual PTZ,
per-camera recording, privacy and supported local Tapo controls. General
camera PATCH accepts only `recording_mode` for that scope;
configuration, camera deletion, footage deletion/protection, exports,
security, storage changes, backups, restore and token administration remain
human-administrator operations. Camera reboot and preset/patrol configuration
also remain administrative. Integration tokens alone cannot enter the separate
operator viewer surface, which includes persistent exports and PTZ.
Migration `0018_scoped_api_tokens` assigns existing tokens `camera_control`,
preserving their secrets, expiry/revocation and HA operation while removing
administration. A request with insufficient scope gets `403`.
A request with no credential, or an invalid/revoked/expired
one, gets `401` (REST) or a close with WebSocket code `1008` before the
socket is even accepted (never a silent empty stream).

## 2. Discovery: mDNS

Sentinel announces `_sentinel._tcp.local.` (via `zeroconf`) whenever
`SENTINEL_ENABLE_MDNS` is true (the default) and at least one non-loopback,
non-link-local IPv4 address can be found. Failure anywhere in this path —
no multicast route, a firewall, a container network mode that blocks
multicast — is caught, logged once as a warning, and never stops Sentinel
from starting or serving HTTP; the config flow's manual "host + token" entry
always works regardless of whether discovery does.

* **Service type:** `_sentinel._tcp.local.`
* **Instance name:** `<sanitised-hostname>._sentinel._tcp.local.` — the
  config flow already splits the announced name on the literal substring
  `._sentinel` to get a display name, so any instance name of this shape
  works.
* **Port:** the configured HTTP port (`8090` by default) — the same port
  serves the REST API and the WebSocket.
* **TXT record:** `version` (Sentinel's version string) and `id` (the same
  stable install identifier as `unique_id` in the handshake below, so a
  client that only has the mDNS record can still recognise "have I seen
  this box before" without an HTTP round trip). The current config flow
  does not read TXT properties — it takes `host`/`port`/`name` from the
  mDNS answer and does its own HTTP handshake to get everything else — so
  nothing here is load-bearing yet, but the fields are real and stable.
* **IPv4 only.** No AAAA records are published in this phase. A host with
  only a link-local self-assigned address (`169.254.0.0/16`, no DHCP/static
  IPv4) announces nothing and logs why.

## 3. The handshake: `GET /api/v1/integrations/home-assistant`

The one new REST endpoint this phase adds. It is the first call the config
flow makes (manual entry or after zeroconf discovery) and the call the
coordinator repeats on every poll — it doubles as both "what does this
server look like" and "is this token still valid" (a `401` here is exactly
how the integration notices a revoked token and asks Home Assistant to
reauthenticate).

A `404` on this exact path is not "try again" — it means the Sentinel build
being talked to predates Home Assistant support entirely, and the
integration reports that distinctly rather than a confusing connection
error.

```
GET /api/v1/integrations/home-assistant
Authorization: Bearer sctv_xxx
```

```json
{
  "unique_id": "0123456789abcdef0123456789abcdef",
  "name": "Sentinel CCTV",
  "version": "0.1.0",
  "api_version": 1,
  "websocket_path": "/api/v1/events/stream",
  "cameras": [
    {
      "id": "b2f1...uuid",
      "name": "Porch",
      "enabled": true,
      "status": "online",
      "manufacturer": "Tapo",
      "model": "C210",
      "recording_mode": "continuous",
      "recording_state": "recording",
      "motion_enabled": true,
      "motion_active": false,
      "motion_zones": [
        {"id": "z3f9a1c2b", "name": "Gate", "active": false}
      ],
      "capabilities": {
        "snapshot": true,
        "live_stream": true,
        "recording": true,
        "motion_events": true
      },
      "stream": {
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "fps": 15.0,
        "audio_codec": null,
        "bitrate_kbps": 2048
      }
    }
  ]
}
```

Field notes:

* **`unique_id`** — stable for the life of the install (derived one-way from
  the same local secret everything else derives keys from; see
  `app/integrations/identity.py`). Home Assistant keys its config entry on
  this, so it survives an IP address or hostname change and correctly
  refuses to silently rebind to a *different* Sentinel that happens to come
  up at the same old address later.
* **`api_version`** — `1`. The integration rejects a server reporting a
  different number rather than guessing; bump this only when a change here
  would make older integration code misread the payload, not for additive
  fields.
* **`websocket_path`** — always the full path including `/api/v1`, ready to
  turn into a URL by substituting `ws(s)://<host>` for `http(s)://<host>`.
* **`cameras`** — everything a `camera`/`binary_sensor`/`sensor` entity needs
  without a second request. `recording_state` is one of exactly
  `"recording" | "idle" | "off" | "unavailable"` (an unrecognised value is
  worse than a missing one for an enum-typed HA sensor, so Sentinel only
  ever sends these four). `motion_active` reflects whether a `MotionEvent`
  with no `ended_at` exists right now for that camera — the state a
  `binary_sensor` should show *before* the first `motion.started` frame
  arrives on the socket. `motion_zones` lists the camera's enabled
  *detection* zones (ignore zones and disabled zones are never listed), each
  with whether it sees motion right now; each one becomes its own motion
  `binary_sensor`, keyed on its `id`, which survives renaming the zone. The
  list is empty when motion detection is off for the camera. `capabilities` is additive information the current
  integration code does not yet read (it infers the same facts from
  `recording_mode`/`enabled`/`motion_enabled`); it is included for a client
  that would rather not duplicate that inference.
* This same shape, unwrapped as a bare list, is what `GET /api/v1/cameras`
  (owned by `app/cameras/router.py`, unchanged by this phase) already
  returns for `id`, `name`, `enabled`, `status`, `manufacturer`, `model`,
  `recording_mode`, `motion_enabled` and `stream`. The integration's parser
  (`CameraInfo.from_payload`) already falls back to that endpoint if a
  server's handshake omits `cameras` entirely, and tolerates every field
  above being absent except `id` and `name`. **Nothing about `/cameras`
  needed to change for this to work.**

## 4. Reused endpoints — nothing new needed

Everything else the integration needs is already implemented by earlier
phases and already reachable with an API token, because `CurrentPrincipal`
has always accepted one. This phase adds no new routes for any of these;
they are listed here only so the contract is complete in one place.

| Purpose | Endpoint | Owner |
| --- | --- | --- |
| Full camera list (fallback if `cameras` were omitted from the handshake) | `GET /api/v1/cameras` | `app.cameras.router` |
| Still image | `GET /api/v1/cameras/{id}/snapshot` | `app.cameras.router` |
| Re-probe a camera on demand | `POST /api/v1/cameras/{id}/test` | `app.cameras.router` |
| Change recording mode (the one HA-writable field) | `PATCH /api/v1/cameras/{id}` `{"recording_mode": "..."}` | `app.cameras.router` |
| Server health | `GET /api/v1/system/health` | `app.system.router` |
| Server info (uptime, timezone) | `GET /api/v1/system` | `app.system.router` |
| Storage usage | `GET /api/v1/storage` | `app.storage.router` |
| Retention policy (for a "days kept" sensor) | `GET /api/v1/storage/settings` | `app.storage.router` |
| Recent motion events / lifetime count per camera | `GET /api/v1/events?camera_id=&limit=&offset=` | `app.motion.router` |

`GET /api/v1/storage`'s `available`, `reason`, `total_bytes`, `used_bytes`,
`free_bytes`, `recordings_bytes` and `segment_count` fields are exactly what
a storage sensor needs; `storage.warning` / `storage.critical` on the event
socket (below) is what tells the coordinator to re-read this endpoint
immediately rather than waiting for the next scheduled poll.

## 5. Realtime: `WS /api/v1/events/stream`

```
GET /api/v1/events/stream?since=<seq>
Authorization: Bearer sctv_xxx
Upgrade: websocket
```

`since` is optional. Omit it for a fresh connection; pass the last `seq`
your client actually processed to resume instead of re-reading everything
over REST.

### Connection sequence

1. The server resolves the caller **before** accepting the socket. No
   credential, or an invalid one → the connection is closed with WebSocket
   code `1008` and never accepted — no frame of any kind is sent first.
2. Once accepted, exactly one of these arrives immediately:
   * No `since` given, or `since` given and the gap is small enough to
     fill: a `hello` frame, followed by any replayed events (in order, real
     `seq` numbers, only when resuming and there is something to replay).
   * `since` given but older than anything the server still has buffered
     (256 events of backlog, or the process restarted and the counter
     reset to zero): a `resync_required` frame instead — re-read the REST
     state you care about, then keep consuming the socket from this point
     forward; nothing before it will be replayed.
3. From then on, every bus event Sentinel publishes that this connection is
   subscribed to (there is no filtering in this phase — every connection
   gets every event type) arrives as its own frame, in order, exactly once.
4. A `heartbeat` frame arrives from the server every ~20 seconds of
   otherwise-idle connection. This is in addition to, not instead of,
   ordinary WebSocket ping/pong at the protocol level (which uvicorn/
   `websockets` already answers transparently — aiohttp's own
   `heartbeat=` keepalive is satisfied by that alone and needs nothing
   application-level from the client). A client that also wants to send its
   own liveness probe may send `{"type": "ping"}` as a text frame at any
   time and will get `{"type": "pong"}` back.
5. A connection that sends nothing at all — no pong, no ping, not even a
   protocol-level frame — for about 75 seconds is closed by the server.
   This is about freeing an abandoned socket, not about rate-limiting a
   healthy one.

### Frame shapes

Every frame is a JSON object with a `type` key; a frame without one is
never sent by Sentinel (the integration's own parser drops anything that
lacks `type`, so this matters).

```json
{"type": "hello", "resumed": false, "replayed": 0}
```

```json
{"type": "hello", "resumed": true, "replayed": 3}
{"type": "motion.started", "seq": 101, "ts": "2026-08-16T10:15:03.120000Z", "data": {"...": "..."}}
{"type": "motion.ended",   "seq": 102, "ts": "2026-08-16T10:15:11.400000Z", "data": {"...": "..."}}
```

```json
{"type": "resync_required", "seq": 340}
```

```json
{"type": "heartbeat", "server_seq": 340, "ts": "2026-08-16T10:16:00.000000Z"}
```

**Why `hello` and `heartbeat` never carry a bare `seq`, but `resync_required`
deliberately does:** a client that tracks "have I missed anything" by
checking that each frame's `seq` is exactly one more than the last one it
saw (the integration repository does exactly this,
`models.has_sequence_gap`) would misfire on a control frame that is not
part of the actual event sequence — `hello` in particular can be followed
by replayed events with *older* sequence numbers than the moment of
connecting, which is correct and must not look like corruption. Putting the
current position on `resync_required` under the name `seq` is deliberate
the other way: it makes an already-gap-detecting client notice the jump and
refresh over REST on its own, with no special case needed for the frame
type at all, and correctly re-bases it so the next real event does not
*also* look like a gap. A client that does not implement gap detection can
instead key off `type == "resync_required"` explicitly. Either works.

### Event catalogue

The `data` object of every substantive event. Every key that could carry a
credential is already stripped before publication (`app.events.bus._clean`
runs on every payload; this phase does not and must not bypass it), so
nothing below ever contains a stream URL with embedded credentials, however
one might have found its way into a caller's payload.

| `type` | `data` |
| --- | --- |
| `camera.added` | `camera_id`, `camera` (name), `host` |
| `camera.removed` | `camera_id`, `camera` |
| `camera.online` | `camera_id`, `camera` |
| `camera.offline` | `camera_id`, `camera`, `reason` (nullable) |
| `motion.started` | `camera_id`, `camera`, `event_id`, `started_at`, `zones` |
| `motion.ended` | `camera_id`, `event_id`, `duration_seconds`, `peak_score`, `zones` |
| `motion.zone_started` | `camera_id`, `camera`, `zone_id`, `zone` (name), `event_id` (nullable), `at` |
| `motion.zone_ended` | `camera_id`, `camera`, `zone_id`, `zone` (name), `event_id` (nullable), `at` |
| `recording.started` | `camera_id`, `camera`, `segment_seconds` |
| `recording.stopped` | `camera_id` |
| `storage.warning` | `deleted`, `freed_bytes`, `reason` |
| `storage.critical` | `reason` |

Two honest limitations, stated plainly so nobody builds an entity on a
false assumption:

* **`camera.online` / `camera.offline` are best-effort, not a continuous
  liveness poll.** Nothing in Sentinel today polls every camera on a timer
  to notice it went dark on its own — that is future work, not this phase.
  These two fire on the transitions `app.cameras.service.test_camera`
  already computes: a probe (manual "Test" in the UI, or the same call
  Home Assistant can make via `POST /cameras/{id}/test`) that newly
  succeeds after not being online, or newly fails after having been online.
  A camera that quietly stops answering between two such probes does not
  get an automatic `camera.offline` until the next one happens.
* **There is no cross-host live video handed to Home Assistant.** go2rtc
  binds `127.0.0.1` only and is not reachable from another machine — that
  is deliberate (`app.streaming.manager`'s own docstring: its unauthenticated
  admin API must never be reachable on the LAN). The browser gets live
  video through Sentinel's own authenticated WebSocket proxy
  (`/api/v1/streams/{id}/ws`, unchanged); a `camera` entity in Home Assistant
  should use the snapshot endpoint above for a still image and link out to
  Sentinel's own UI for actual live viewing, the same way the integration's
  own `CameraInfo.stream_url` is left absent rather than pointing at
  something that would not actually stream across hosts.

## 6. Versioning

`api_version` in the handshake is the only thing either side needs to check
before trusting the rest of this document. It moves only when a field
changes shape or meaning in a way old client code would misread — a new
event `type`, a new optional field, or a new endpoint never bumps it. A
client encountering an event `type` it does not recognise, or a JSON field
it does not expect, ignores exactly that unknown part and keeps working;
this is a design requirement on both sides of the contract, not just a
suggestion for one of them.
