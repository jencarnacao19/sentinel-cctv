# API reference

Everything lives under `/api/v1`, mounted by `backend/app/api/v1.py` from the
individual routers listed below. `GET /docs`
(enable with `SENTINEL_ENABLE_API_DOCS=true`) always reflects the running
build exactly, generated from the same routers.

## Authentication

Every endpoint except `GET /auth/status`, `POST /auth/setup`,
`POST /auth/login`, `POST /auth/logout`, `GET /system/health` and the isolated
viewer surface requires a caller to be identified as a **principal** — either
a signed-in user (session cookie) or an API token (`Authorization: Bearer
sctv_...` header). Permissions are enforced in `backend/app/auth/deps.py`:

- **Any principal** (`CurrentPrincipal`) — read access, and a handful of
  self-service actions.
- **Integration read** — `read_only` (the default) and `camera_control` API
  tokens can read camera telemetry, footage and HA storage status.
- **Camera control** — `camera_control` additionally permits operational
  recording/PTZ/privacy and supported local Tapo actions. It cannot change
  credentials, security/retention configuration, delete footage or manage tokens.
- **Administrator** (`AdminPrincipal`) — a signed-in human administrator only.
- **Viewer accounts** — people who only watch. They sign in like an
  administrator but never become a principal: every route outside the viewer
  treats them as signed out (`401`). They reach the viewer surface, `/auth/me`,
  `/auth/change-password` and their own appearance preferences.
  Migration 0018 maps existing tokens to `camera_control` for HA compatibility;
  they no longer have administrator privileges.

The bundled frontend authenticates with the cookie automatically; a script or
Home Assistant authenticates with a bearer token created under
**Settings → API tokens**:

```bash
curl -H "Authorization: Bearer sctv_xxxxxxxxxxxxxxxxxxxxxxxx" \
  https://sentinel.example.lan/api/v1/system
```

A request with no valid credential gets `401`; a valid credential without the
required permission gets `403`. For HA switches/buttons choose `camera_control`;
for a monitoring-only integration choose `read_only`.

The viewer is not a principal and cannot satisfy either dependency. It uses a
separate, restricted `CurrentViewer` guard described below, which accepts an
administrator session, a viewer account session, the Home Assistant viewer key
or a verified Cloudflare Access assertion.

## Endpoints

### System (`backend/app/system/router.py`, prefix `/system`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/system` | any | Name, version, uptime, setup flag, timezone, caller identity. |
| GET | `/system/health` | none | Used by Docker's own healthcheck; returns `503` when unhealthy. |
| POST | `/system/setup/complete` | admin | Marks the first-run setup finished. Until then the interface sends the administrator to the setup steps. |
| GET | `/system/privacy` | any | Persistent global camera pause, selected predefined reason and safe viewer message. |
| PUT | `/system/privacy` | admin / camera_control | Enable with `{"enabled":true,"reason":"guests"}` or resume with `{"enabled":false}`. Enabling stops live streams, recording and motion before returning. |
| GET | `/system/diagnostics` | any | Host/process metrics, per-subsystem status, tooling detection. |
| GET | `/system/diagnostics/download` | admin | Same report as a downloadable, pre-redacted JSON file. |
| POST | `/system/backup` | admin | Configuration backup (no recordings, no camera passwords) as a `.zip`. |
| POST | `/system/backup/inspect` | admin | Describe an uploaded backup without applying it. |
| POST | `/system/backup/restore` | admin | Replace the configuration database; requires a restart afterwards. |
| POST | `/system/config/export` | admin | Body `{"include_passwords": bool, "passphrase": str \| null}`. Returns `sentinel-config.yaml`: cameras, motion zones and recording settings for moving to another machine. Passwords are included only with a passphrase of at least 8 characters, encrypted with AES-256-GCM under a scrypt key. |
| POST | `/system/config/import` | admin | Multipart `file` and optional `passphrase`. Adds the cameras the file describes, skipping names that exist, and applies its settings except a storage policy that could delete footage here. Returns what was added and skipped, and why. |

### Auth (`backend/app/auth/router.py`, prefix `/auth`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/auth/status` | none | `{"initialised": bool, "setup_completed": bool}` — whether an administrator exists yet, and whether the setup steps were finished. |
| POST | `/auth/setup` | none | Create the administrator account. Refused once any account exists. Rate limited. |
| POST | `/auth/login` | none | Sets the session cookie; also returns the token in the body, with `is_admin` (false for a viewer account). Rate limited (5/minute/client). When public login protection is enabled, a verified Cloudflare visitor address can also be persistently blocked after the configured number of failures. |
| GET/PATCH | `/auth/login-security` | admin | Reads or changes public-login blocking, failure threshold and permanent/timed duration. Enabling fails unless the public hostname and exact trusted proxy network are configured. |
| GET | `/auth/login-blocks` | admin | Lists active public-login blocks without storing attempted passwords or successful-login history. |
| DELETE | `/auth/login-blocks/{address}` | admin | Removes one block. IPv6 addresses are URL-encoded by clients. |
| POST | `/auth/login-blocks/clear` | admin | Clears every block and pending failure after explicit confirmation. |
| POST | `/auth/logout` | none | Clears the session cookie. Takes no principal — anyone holding the cookie can clear it. |
| GET | `/auth/me` | user session only | The signed-in account's own record, viewer accounts included; rejects an API token with `403`. |
| GET | `/auth/whoami` | any | Identifies the caller, session or token, generically. |
| POST | `/auth/change-password` | user session only | Requires the current password. Viewer accounts may change their own. |
| GET | `/auth/users` | admin | Every account: administrator first, then viewer accounts. |
| POST | `/auth/users` | admin | Body `{"username", "password"}`. Adds a viewer account; a name already taken, in any letter case, is `409`. |
| PUT | `/auth/users/{id}/password` | admin | Body `{"password"}`. Sets a viewer account's password. Sessions already open stay open. |
| DELETE | `/auth/users/{id}` | admin | Removes a viewer account, ending its sessions everywhere. The administrator is refused with `409` on both routes. |
| GET | `/auth/tokens` | admin | List API tokens (hashes only; never the plaintext again). |
| POST | `/auth/tokens` | admin | Create a token; the plaintext is in the response exactly once. Rate limited. |
| DELETE | `/auth/tokens/{id}` | admin | Revoke a token immediately. |

### Cameras (`backend/app/cameras/router.py`, prefix `/cameras`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/cameras` | any | `?enabled_only=true` to filter. |
| POST | `/cameras` | admin | Add a camera; also pushes the change to go2rtc and the recorder. |
| POST | `/cameras/test-stream` | admin | Probe a URL that has not been saved. Rate limited (20/minute — it spawns FFprobe). |
| GET | `/cameras/{id}` | any | |
| PATCH | `/cameras/{id}` | admin | Partial update; see the password semantics below. `recording_profile` accepts `auto`, `passthrough`, `resilient` or `normalised`. `recording_source_mode` accepts `auto`, `relay` or `direct`; both explicit modes remain LAN-only and disable failover for that camera. `recording_segment_seconds` accepts 10–300 or `null` to inherit the system default. Changing any of these settings restarts only that recorder. `tapo_patrol_mode` accepts `horizontal`, `vertical` or `null`; optional `tapo_password` is write-only and separate from RTSP/ONVIF credentials. `recording_enabled` starts or stops that camera's recorder, exactly as `PUT /cameras/{id}/recording` does. `tapo_controls_enabled` is tri-state: `null` leaves the decision to detection, `true`/`false` are an administrator overriding it. A `tapo_battery` camera, which only the dev build can add, refuses every change with `422`. |
| DELETE | `/cameras/{id}` | admin | Cascades to its segments and events. |
| POST | `/cameras/{id}/test` | admin | Probes the saved camera and updates its status/codec fields. Rate limited. |
| PUT | `/cameras/{id}/recording` | admin / camera_control | Starts or stops **only this camera's** recorder. Body `{"enabled": bool, "confirm": bool}`; `confirm` must be `true` to stop, because the footage from the paused period cannot be recovered. Answers with the *observed* recorder state — `recording`, `off`, `paused`, `failed` or `unsupported` — not an echo of the request, so a start that failed is reported as `failed`. `409` for a camera that has no continuous recorder. Live view, ONVIF, motion detection, the other cameras and global privacy mode are untouched. |
| POST | `/cameras/{id}/identify` | admin | One read-only ONVIF `GetDeviceInformation`; stores the manufacturer and model and returns the resulting Tapo-control decision. Nothing on the camera changes and the serial number is deliberately not read. Rate limited. `422` when no ONVIF service answers. |
| GET | `/cameras/{id}/snapshot` | any | JPEG, `?width=` optional (64–3840). Rate limited. |
| POST | `/cameras/{id}/ptz/detect` | admin | Detects ONVIF PTZ, presets, native home and tour capacity without moving; persists the validated service/profile. Rate limited. |
| GET | `/cameras/{id}/ptz/setup` | any | Reads named ONVIF presets, capacity and the active LAN patrol backend. |
| POST | `/cameras/{id}/ptz/presets` | admin | Saves the current physical position under a name. |
| POST | `/cameras/{id}/ptz/presets/{token}/goto` | any | Moves to a camera-owned ONVIF preset. |
| DELETE | `/cameras/{id}/ptz/presets/{token}` | admin | Removes a preset; an active native tour must be stopped first. |
| PUT | `/cameras/{id}/ptz/patrol` | admin | Creates or updates and starts a camera-owned ONVIF preset tour. |
| DELETE | `/cameras/{id}/ptz/patrol` | admin | Stops and disables automatic ONVIF/Tapo patrol without disabling manual PTZ. |
| POST | `/cameras/{id}/ptz/move` | any | Sends a normalised `pan`/`tilt`/`zoom` velocity (`-1..1`) with camera and server dead-man timeouts. |
| POST | `/cameras/{id}/ptz/stop` | any | Stops pan/tilt and zoom immediately. Never rate limited, so a stop cannot be blocked by a burst ceiling. A configured ONVIF patrol, or Tapo LAN fallback, resumes asynchronously afterwards. |

Every camera in a `GET /cameras` response carries two derived fields:

- **`tapo_controls`** — `{supported, source, detected_vendor, reason, can_opt_in}`.
  `source` is one of `manual`, `kind`, `metadata`, `other_vendor` or `unknown`.
  `reason` is a sentence intended for the operator. `can_opt_in` is true only for
  `unknown`, which is the one case where offering the manual setting is honest:
  a camera positively identified as another vendor is not a maybe.
- **`recording_state`** — what the recorder is doing (`recording`, `off`,
  `paused`, `failed`, `unsupported`), read from the process rather than from
  `recording_enabled`. The two disagreeing is exactly the condition the
  interface has to be able to show.

Sentinel decides `tapo_controls` from the camera's manufacturer, its model and
the vendor-specific shape of its stream path — never from its address, because
several channels of one recorder share one. See
`backend/app/cameras/vendor.py`.

### Restricted footage operator (`backend/app/viewer/router.py`, prefix `/viewer`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/viewer/session` | viewer key | Exchanges `X-Sentinel-Viewer-Key` for a session-scoped `HttpOnly` viewer cookie; returns `204` with `Cache-Control: no-store`. |
| GET | `/viewer/cameras` | viewer | Enabled cameras with only ID, name, status, substream/PTZ flags and stream characteristics; no RTSP URL, host, username or settings. |
| POST | `/viewer/cameras/{id}/ptz/move` | viewer | Operates a previously detected PTZ camera; rate limited for hold-to-move use. |
| POST | `/viewer/cameras/{id}/ptz/stop` | viewer | Immediate all-axis stop through the operator boundary. |
| GET | `/viewer/system/status` | viewer | Safe, non-cacheable operator summary: health, camera/recorder counts, disk usage, ingest rate, saved-clip size, projected retention and the active privacy reason. Never returns paths, hosts, URLs, credentials or mutable settings. |
| GET | `/viewer/reliability` | viewer | Narrow per-camera reliability for enabled cameras only: state, whether recording is fresh, the newest recording time and a fixed issue label. Recorder process state, restart counts, the input in use, the classified fault and incident history are administrator-only. |
| GET | `/viewer/cameras/{id}/timeline` | viewer | Recording coverage, gaps and motion for an enabled camera. |
| GET | `/viewer/cameras/{id}/playback/manifest` | viewer | Ordered playback segments with viewer-only download URLs. |
| GET | `/viewer/cameras/{id}/playback/seek` | viewer | Resolves a wall-clock instant to a playable segment and offset. |
| GET | `/viewer/recordings/{segment_id}/download` | viewer | Streams one segment through the confined range-download implementation. |
| POST | `/viewer/exports` | viewer | Creates a rate-limited, permanently protected saved clip. |
| GET | `/viewer/exports` | viewer | Lists saved clips for one enabled camera. |
| GET | `/viewer/exports/{job_id}` | viewer | Reads saved-clip progress and metadata. |
| GET | `/viewer/exports/{job_id}/download` | viewer | Downloads a completed saved clip. |

Viewer authentication is one of: an existing human session; the
exact configured Cloudflare Access hostname plus a cryptographically verified
JWT for the configured issuer and audience, not merely a nonempty header; or the
separate restricted key, supplied as `X-Sentinel-Viewer-Key` or by its viewer
session cookie. The key is never accepted from an HTTP query string. An API
token alone cannot use this separate operator surface to bypass its scope.

There are deliberately no viewer routes for discovery, camera/settings
changes, export deletion or protection removal. PTZ is a narrow operator action
against an already configured camera, not permission to change its
configuration or credentials.

### Motion zones (`backend/app/motion/router.py`, `zones_router`, prefix `/cameras`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/cameras/{id}/motion-zones` | any | `{"camera_id", "zones": [...]}` for this camera. |
| PUT | `/cameras/{id}/motion-zones` | admin | Body `{"zones": [...]}`. Replaces the zone list wholesale; restarts the camera's motion worker. |
| GET | `/cameras/{id}/motion-preview` | admin | A JPEG frame used by the **Motion zones** editor. |

A zone is `{"id", "name", "kind", "enabled", "sensitivity", "points"}`. `kind`
is `detect` or `ignore`. `points` holds 3–128 `[x, y]` pairs normalised to
`0`–`1`. `sensitivity` (`0`–`100`) applies to `detect` zones only; `null` uses
the camera's. Leave `id` out of a new zone and the server assigns one; keep it
on an existing zone so its Home Assistant sensor survives a rename. A camera
has at most 32 zones, 16 of them `detect`, and zone names must differ.

When a camera has `detect` zones, only motion inside them counts, and each zone
reports its own motion as `motion.zone_started` and `motion.zone_ended` on the
event stream. Motion events list the zones they touched in `zones`. Before 2.0
this API took a bare list of polygons; zones saved then load as `ignore`.

### Live streams (`backend/app/streaming/router.py`, prefix `/streams`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/streams/{id}` | any | Everything the player needs: stream names, WebSocket URL, snapshot URL, codec. |
| POST | `/streams/{id}/webrtc` | principal | Forward an SDP offer to go2rtc; `?sub=true` for the substream. |
| POST | `/streams/{id}/viewer-webrtc` | viewer | Same WebRTC negotiation through the restricted viewer grant; used by iOS Home Assistant WebViews. |
| POST | `/streams/{id}/hls-session` | principal | Opens a same-origin fMP4/HLS fallback for native Safari playback. Returns an opaque, camera-scoped media URL rather than a credential. `?sub=true` selects the substream. |
| POST | `/streams/{id}/viewer-hls-session` | viewer | Viewer-authorised form of the HTTPS fallback. The long viewer key is used only on this POST and never appears in the returned URL. |
| GET | `/streams/{id}/hls/{grant}/*` | short-lived grant | Proxies only the playlist/init/segments for the camera that issued the opaque grant. Grants expire after 45 seconds idle or 15 minutes total, are not cached and are cleared by privacy mode. |
| WS | `/streams/{id}/ws` | any | Bidirectional proxy to go2rtc's player socket (MSE and WebRTC signalling both go over this). Authenticated at the handshake, before it is accepted. |
| WS | `/streams/{id}/viewer-ws` | viewer | Same media proxy through the isolated viewer guard. The embedded dashboard uses its HttpOnly viewer cookie or a validated `sentinel.viewer.*` WebSocket subprotocol when the WebView blocks third-party cookies; the key never enters the URL. The normal camera wall relies on the protected Cloudflare hostname. |
| GET | `/streams/{id}/status` | any | go2rtc process state and whether this stream is known to it. |

While global privacy mode is active, new HTTP camera operations return `423`
with the selected safe explanation. Existing live WebSockets are closed with
the same explanation and managed go2rtc is stopped, which also terminates
already-negotiated WebRTC media. Native HLS grants are cleared and every
playlist/segment request also enforces privacy mode. Historical recording and export routes remain
available by design.

### Recordings (`backend/app/recording/router.py`, prefix `/recordings`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/recordings` | any | Filters: `camera_id`, `start`, `end`, `protected`, `has_motion`, `limit` (≤500), `offset`. |
| GET | `/recordings/status` | any | Storage path/availability and per-camera recorder process status. |
| GET | `/recordings/{id}` | any | |
| PATCH | `/recordings/{id}` | admin | Set `protected`. See `docs/recording.md` for what this does and does not cascade to. |
| GET | `/recordings/{id}/download` | any | Supports HTTP range requests, so the browser can seek inside it. |

### Camera reliability (`backend/app/reliability/router.py`, prefix `/reliability`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/reliability/cameras` | admin | Per-camera operational state (`healthy`, `degraded`, `offline`, `recovering`, `disabled`, `privacy_paused`), recording freshness, newest completed segment, recorder process state and input (`relay`/`direct`), restart counts, the classified stream fault, the active incident and recent history. Reads only telemetry Sentinel already holds; opens no camera connection and writes nothing. |
| GET | `/reliability/incidents` | admin | Filters: `camera_id`, `active_only`, `limit` (≤200), `offset`. Newest first. |
| POST | `/reliability/cameras/{id}/retry` | admin | Requests a refresh of that camera's relay definition and restarts its configured recorder and motion worker. Returns `423` during privacy mode, `409` for a disabled camera, `429` for the per-client limit (6 per 5 minutes) or the per-camera cooldown (60 s). Reports the actions requested and the state *before* the request; it never claims the relay or video recovered. |
| POST | `/reliability/cameras/{id}/source/requalify` | admin | Clears a camera's learned recorder-source penalties so it returns to the shared relay on the recorder's next start. Interrupts nothing now. `423` during privacy mode. |

`GET /reliability/cameras` gained a `quality` block per camera: measured
recorded coverage over the trailing hour, gap count and longest gap, damaged
and short segment counts, restarts in the window, the counted
stream-corruption rate and category, the active recording profile, and why the
current recorder source was chosen. `GET /viewer/reliability` gained only
`coverage_percent` — an operator needs the magnitude, and the reasons stay
administrator-only.

Incidents open only after a fault has persisted for about ninety seconds and
close only after three minutes of sustained recovery, so an ordinary supervisor
reconnect is not recorded as an outage. While privacy mode is active the
incident list is frozen: none are opened and none are closed, because a
deliberate pause is neither an outage nor a recovery.

Reasons and stream faults come from closed vocabularies. Camera output is
classified into a label and discarded — no captured FFmpeg text, URL, host,
path or credential is stored on an incident or returned by these routes.

### Tapo battery cameras

Not part of releases. They are being developed on the `dev` branch and its
`ghcr.io/jencarnacao19/sentinel-cctv:dev` image. A release refuses to add one
(`422`). A camera the dev build added stays in the database and is listed with
`kind=tapo_battery`, but it cannot be changed, recorded, streamed or sent Tapo
commands. `DELETE /cameras/{id}` removes it.

### Local Tapo control (`backend/app/cameras/tapo_router.py`, prefix `/cameras`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/cameras/{id}/tapo/capabilities` | admin | What this individual camera positively confirmed it supports. Cached and persisted; `?refresh=true` forces a new probe. Also returns `partial` and `unchecked`: a discovery that only partly answered keeps what it confirmed and names the vendor methods that did not reply. |
| GET | `/cameras/{id}/tapo/state` | admin | Current value of every supported control, from one batched read. |
| POST | `/cameras/{id}/tapo/reboot` | admin | Requires `{"confirm": true}`. Audited. |
| POST | `/cameras/{id}/tapo/siren/start` · `/siren/stop` | admin | Requires `{"confirm": true}`. Audited at warning level. |
| POST | `/cameras/{id}/tapo/{feature}` | admin | One write against a confirmed capability. `feature` is checked against a closed list, so a path segment can never become a vendor method name. |

All of these are administrator-only, rate limited on the shared expensive-operation
bucket, and refused with `423` while privacy mode is active — checked before the
network, not after. Errors map `unsupported → 409`, `authentication → 502`,
`unavailable → 503`.

Every route here also refuses, with `409` and the reason, a camera Sentinel does
not believe is a TP-Link/Tapo device. That is enforced server-side and not only
hidden in the interface: probing another vendor's camera on a private control
port is an unauthenticated connection attempt against a device that never
advertised the protocol, and on an NVR it is one attempt per channel against the
same box.

**Timing.** One request's worth of vendor work shares a hard 14-second deadline
across every attempt and fallback, sized to finish inside a reverse proxy's
origin window so a slow camera produces a Sentinel error rather than the proxy's
own error page. Discovery asks for the twenty-four methods Sentinel can actually
use rather than the vendor helper's eighty-seven, and splits a batch the camera
rejects until the offending method is isolated — so one method a firmware
mishandles costs that capability, not the panel. Blocking vendor calls run on a
dedicated four-thread pool: a timed-out vendor call cannot be cancelled and
keeps its worker until the OS gives up on the socket, and on the shared pool a
few unresponsive cameras were measured slowing every other request in the
process by three orders of magnitude.

The connection target is built from the stored `camera.host` and validated as a
private/local address; no route accepts a host, URL or port. Firmware upgrade,
firmware check, SD-card format and factory reset are excluded by name and never
called. `pytapo` 3.4.18 is LAN-only — every request goes to the camera's own
address, and the TP-Link cloud password is never transmitted.

### Motion events (`backend/app/motion/router.py`, prefix `/events`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/events` | any | Filters: `camera_id`, `start`, `end`, `protected`, `min_duration_seconds`, `limit` (≤500), `offset`. |
| GET | `/events/status` | any | Per-camera motion worker status (frames seen, reconnects, last error). |
| GET | `/events/{id}` | any | |
| PATCH | `/events/{id}` | admin | Set `protected` — on the event row only, see `docs/recording.md`. |
| GET | `/events/{id}/thumbnail` | any | JPEG. |
| GET | `/events/{id}/snapshot` | any | JPEG, full resolution. |

### Timeline and playback (`backend/app/recording/timeline_router.py`, prefix `/cameras`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/cameras/{id}/timeline` | any | Coverage, gaps and motion bands for a UTC `start`/`end` range. |
| GET | `/cameras/{id}/playback/manifest` | any | Ordered playable segments for a UTC range. |
| GET | `/cameras/{id}/playback/seek` | any | Resolve UTC `at` to a segment, offset or the next footage. |

### Clip exports (`backend/app/recording/timeline_router.py`, prefix `/exports`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/exports` | admin | Queue a camera/time-range MP4. `protected` defaults to `true`, making it a permanent saved clip. |
| GET | `/exports` | any | List jobs; optional `camera_id`, pagination. |
| GET | `/exports/{id}` | any | Job state and error detail. |
| PATCH | `/exports/{id}` | admin | Set `protected`. Permanent clips have no automatic expiry; temporary completed clips expire 24 hours after this change. |
| GET | `/exports/{id}/download` | any | Download a completed export. |
| DELETE | `/exports/{id}` | admin | Cancel an active job or remove a finished one. |

### Storage (`backend/app/storage/router.py`, prefix `/storage`)

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/storage` | admin / integration | Private diagnostics: exact path; recordings/saved clips/snapshots/database/other; protected and damaged footage; recording rate, projections and per-camera usage. `shares_system_disk` is true when recordings sit on the same filesystem as Sentinel's own data, normally the small system disk. |
| GET | `/storage/settings` | admin / integration | Current retention policy. |
| PATCH | `/storage/settings` | human admin | `path`, `retention_days`, `max_bytes`, `min_free_bytes` — see `docs/configuration.md` for which of these the interface actually exposes today. |
| POST | `/storage/sweep` | admin | Run retention immediately instead of waiting for the ten-minute tick. Reclaims recording segments **and** motion evidence — the snapshot and thumbnail kept for each motion event — on the same policy. `events_deleted` and `event_bytes_freed` are reported separately from `deleted`/`freed_bytes`, so a sweep that only cleared thumbnails cannot read as one that reclaimed footage. |
| GET | `/storage/timeline-range` | any | Earliest/latest footage per camera, used by the timeline calendar. |

### Camera discovery (`backend/app/discovery/router.py`, prefix `/discovery`)

All discovery operations require an administrator and expensive probes are rate
limited. Host targets are restricted to local/private address space.

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/discovery/vendors` | admin | Known camera and NVR URL profiles. |
| POST | `/discovery/scan` | admin | Bounded ONVIF WS-Discovery scan. |
| POST | `/discovery/profiles` | admin | Resolve ONVIF media profiles into stream URLs. |
| POST | `/discovery/probe-known` | admin | Try documented vendor URL patterns. |
| POST | `/discovery/scan-nvr` | admin | Probe a bounded range of NVR channels. |
| POST | `/discovery/import` | admin | Save a batch of discovered cameras. |

The administrator frontend exposes ONVIF discovery, media-profile selection,
vendor probing and manual configuration. WS-Discovery requires Docker host
networking because it uses link-local multicast; every discovery route remains
administrator-only.

### Home Assistant and realtime

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/integrations/home-assistant` | any | Versioned server identity and camera-state handshake. |
| WS | `/events/stream` | any | Authenticated event stream with resume, heartbeat and JSON ping/pong. |

## Examples

### Sign in

```bash
curl -i -X POST https://sentinel.example.lan/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "correct horse battery staple"}'
```

```json
{
  "access_token": "eyJzdWIiOi...body.signature",
  "token_type": "bearer",
  "expires_at": "2026-08-16T02:00:00Z"
}
```

The same response also sets the `sentinel_session` cookie — the frontend
never touches `access_token` directly, it relies on the cookie.

### Add a camera and test it first

```bash
curl -X POST https://sentinel.example.lan/api/v1/cameras/test-stream \
  -H "Authorization: Bearer sctv_..." -H "Content-Type: application/json" \
  -d '{"url": "rtsp://192.168.50.50:554/stream1", "username": "sentinel", "password": "camera-password"}'
```

```json
{
  "ok": true,
  "error": null,
  "codec": "h264",
  "resolution": "2560x1440",
  "width": 2560,
  "height": 1440,
  "fps": 15.0,
  "audio_codec": null,
  "bitrate_kbps": 4096,
  "can_stream_copy": true
}
```

```bash
curl -X POST https://sentinel.example.lan/api/v1/cameras \
  -H "Authorization: Bearer sctv_..." -H "Content-Type: application/json" \
  -d '{
        "name": "Driveway",
        "main_stream_url": "rtsp://sentinel:camera-password@192.168.50.50:554/stream1",
        "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
        "recording_mode": "continuous",
        "motion_sensitivity": 55
      }'
```

The response (`CameraResponse`) never includes the password, only
`"has_credentials": true`:

```json
{
  "id": "3f8c9e2a-...",
  "name": "Driveway",
  "manufacturer": null,
  "model": null,
  "nvr_group": null,
  "enabled": true,
  "recording_mode": "continuous",
  "retention_days": null,
  "motion_enabled": true,
  "motion_sensitivity": 55,
  "host": "192.168.50.50",
  "main_stream_url": "rtsp://192.168.50.50:554/stream1",
  "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
  "username": "sentinel",
  "has_credentials": true,
  "status": "offline",
  "status_message": null,
  "last_seen_at": null,
  "stream": {
    "codec": null, "width": null, "height": null,
    "fps": null, "audio_codec": null, "bitrate_kbps": null
  },
  "created_at": "2026-08-15T09:00:00Z",
  "updated_at": "2026-08-15T09:00:00Z"
}
```

### List recent motion events for one camera

```bash
curl -H "Authorization: Bearer sctv_..." \
  "https://sentinel.example.lan/api/v1/events?camera_id=3f8c9e2a-...&limit=5"
```

```json
{
  "events": [
    {
      "id": "9b1e...",
      "camera_id": "3f8c9e2a-...",
      "started_at": "2026-08-15T08:12:03Z",
      "ended_at": "2026-08-15T08:12:19Z",
      "duration_seconds": 16.0,
      "peak_score": 0.041,
      "pre_roll_start": "2026-08-15T08:11:58Z",
      "post_roll_end": "2026-08-15T08:12:29Z",
      "protected": false,
      "has_snapshot": true,
      "thumbnail_url": "/api/v1/events/9b1e.../thumbnail",
      "snapshot_url": "/api/v1/events/9b1e.../snapshot",
      "playback_url": "/event/9b1e..."
    }
  ],
  "total": 37,
  "limit": 5,
  "offset": 0
}
```

`playback_url` is a legacy event route; use the **Timeline** page or the
timeline/playback endpoints for current playback.

### Review a retention policy

Use Storage while signed in as a human administrator. Integration tokens cannot
change retention or run a sweep. POST `/storage/settings/preview` accepts proposed
settings and returns measured space, affected counts/dates, preserved footage,
warnings and an expiring `preview_token`. It never deletes files.

For a tighter policy, PATCH `/storage/settings` must include that token and
`confirmation: "APPLY RETENTION POLICY"`. A stale review returns409 and must be
requested again. Saving authorizes subsequent automatic sweeps; do not treat a
policy save as non-destructive merely because deletion occurs asynchronously.
Protected footage and active/recent recordings remain excluded. Storage path
changes must be made separately before reviewing a policy for the new volume.

### Health check (no credential needed)

```bash
curl https://sentinel.example.lan/api/v1/system/health
```

```json
{
  "status": "healthy",
  "database": true,
  "storage": true,
  "go2rtc": true,
  "recording": true,
  "components": {
    "database": { "state": "ok" },
    "storage": { "state": "ok" },
    "go2rtc": { "state": "ok", "data": { "streams": 3 } },
    "recording": { "state": "ok", "data": { "expected": 3, "running": 3 } },
    "motion": { "state": "ok", "data": { "expected": 2, "running": 2 } }
  },
  "version": "2.0.0",
  "uptime_seconds": 154302.9
}
```

`components` may carry more or fewer keys than the four in the always-present
top-level fields (`database`, `storage`, `go2rtc`, `recording`) — those four
are the stable contract for an external monitor; everything under
`components` is additional detail.
