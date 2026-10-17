# Configuration

Sentinel deliberately has very little to configure. `backend/app/config.py`
says so in its own docstring: *"a normal user never edits any of this."*
There are two layers, and mixing them up is the most common way to be
surprised:

1. **Process-level settings** — environment variables (`SENTINEL_*`), read
   once at start-up (`backend/app/config.py`). Changing one means restarting
   the container.
2. **Runtime settings** — stored in the database, either as columns on
   `Camera` or as JSON rows in the generic `app_settings` table
   (`backend/app/system/settings_store.py`). Changing one takes effect live,
   with no restart, because the value is re-read the next time it is needed
   (the next retention sweep, the next recorder restart, and so on).

Every default below is read directly from the source, not from memory — the
file is named so you can check it yourself.

## Process-level settings (environment variables)

All prefixed `SENTINEL_` except `TZ`, which Docker and most tools already
recognise. Set them in `compose.yml`'s `environment:` block or, for a manual
install, in the shell or a `.env` file next to the process.

| Variable | Default | What it does | Get it wrong and… |
| --- | --- | --- | --- |
| `SENTINEL_DATA_DIR` | `/data` | Database, thumbnails, snapshots, the install secret. | A path that is not writable fails start-up outright — this is one of the two critical health probes (`backend/app/main.py`). |
| `SENTINEL_RECORDINGS_DIR` | `/recordings` | Default recordings root, used until a path is chosen under **Storage** in the interface. | Not fatal by itself; if it is not writable, recording stays suspended with a clear reason on the dashboard until a working path is set. |
| `SENTINEL_HOST` | `0.0.0.0` | Interface the API binds to. | Bind to `127.0.0.1` and nothing outside the container/host can reach it — fine behind a reverse proxy on the same host, useless otherwise. |
| `SENTINEL_PORT` | `8090` | API and web interface port. | With the supplied host-network compose file this is the actual host port; it must also match any reverse-proxy upstream. |
| `SENTINEL_CORS_ORIGINS` | *(empty)* | Comma-separated extra origins allowed to call the API. | The bundled frontend is same-origin and never needs an entry here. Only add one if you are running the frontend dev server against a separately hosted backend. |
| `SENTINEL_FRAME_ANCESTORS` | *(empty)* | Comma-separated origins allowed to embed Sentinel, in addition to Sentinel itself. | Set the exact Home Assistant external origin and, when the mobile app switches to it, its private LAN HTTP origin, for example `https://ha.example.net,http://192.168.50.10:8123`. Public HTTP origins and wildcards are rejected. This controls framing only; it does not grant viewer or administrator access. |
| `SENTINEL_ENABLE_API_DOCS` | `false` | Exposes `/docs` and `/openapi.json`. | Harmless to enable — it does not weaken authentication — but there is no reason to turn it on outside development. |
| `SENTINEL_ENABLE_TAPO_BATTERY_PROVIDER` | `false` | Enables the experimental event-based provider for TC82-class battery cameras. | Keep it off until a real camera has passed **Test connection**. Enabling it alone contacts nothing and never creates a continuous recorder. |
| `SENTINEL_TAPO_BATTERY_LIVE_MAX_SECONDS` | `300` | Hard limit for one explicitly requested Direct Stream session (30–1800 seconds). | A longer value keeps a battery camera awake for longer; it still does not start automatically. |
| `SENTINEL_TAPO_BATTERY_SYNC_MAX_CLIPS` | `20` | Maximum event clips one manual sync can import (1–100). | Large values can wake the camera/hub and consume local storage; imports remain explicit and idempotent. |
| `SENTINEL_ACCESS_TOKEN_TTL_MINUTES` | `720` (12h) | How long a browser session cookie is valid. | Too short and users are logged out while actively using the interface; the value is not enforced server-side against a shorter idle timeout, only against this fixed TTL from login. |
| `SENTINEL_SESSION_COOKIE_NAME` | `sentinel_session` | Name of the session cookie. | Only matters if you run more than one Sentinel instance on the same browser origin and need them not to collide. |
| `SENTINEL_COOKIE_SECURE` | `false` | Marks the session cookie `Secure`, so the browser will only ever send it over HTTPS. | Set this to `true` **only** once Sentinel is actually served over HTTPS (typically by a reverse proxy in front of it) — if you set it while still serving plain HTTP, the browser silently refuses to send the cookie back, and every login appears to succeed and then immediately behaves as signed out. See `docs/security.md`. |
| `SENTINEL_VIEWER_ENABLED` | `false` | Enables the isolated footage-operator landing page and viewer API. | Enabling this alone does not expose video: a trusted Cloudflare Access hostname or a valid viewer key is still required. The operator can view live/playback and create permanent exports, but administration remains behind the normal login. |
| `SENTINEL_VIEWER_TRUSTED_HOSTS` | *(empty)* | Exact hostnames allowed for the Cloudflare Access viewer grant. | A hostname alone never authorises viewing. Keep the origin private. |
| `SENTINEL_VIEWER_CLOUDFLARE_ISSUER` | *(empty)* | Your Access team URL, `https://<team>.cloudflareaccess.com`; supplies the pinned issuer and public-key endpoint. | Missing or invalid configuration denies the Cloudflare grant. Normal user login and separate viewer keys still work. |
| `SENTINEL_VIEWER_CLOUDFLARE_AUDIENCES` | *(empty)* | Comma-separated Application Audience (AUD) tags from your own Access application. | Tokens for other applications, expired tokens and invalid signatures are rejected. |
| `SENTINEL_TRUSTED_PROXY_CIDRS` | *(empty)* | Exact proxy networks allowed to supply the visitor identity in `CF-Connecting-IP`. | Required before public login IP blocking can be enabled. Use the tunnel peer itself, for example `192.168.50.20/32`; never trust the whole LAN. Direct clients can forge this header, so Sentinel ignores it unless the TCP peer is in this list. |
| `SENTINEL_VIEWER_KEY` | *(unset)* | Shared restricted-operator key for an embedded Home Assistant Webpage dashboard; minimum 32 characters. | Store it only in the deployment `.env`, never Git or Proxmox notes. Supply it to the page in `#viewer_key=...`; the frontend exchanges it for an `HttpOnly` viewer-session cookie and retains a session-only header/WebSocket-subprotocol fallback for iOS WebViews. The key never enters request URLs and cannot call administration APIs or delete saved footage. |
| `SENTINEL_GO2RTC_HOST` | `127.0.0.1` | Interface go2rtc's API and RTSP listeners bind to. | This is a deliberate security boundary — see `docs/security.md`. Do not point it at `0.0.0.0` unless you also intend to authenticate go2rtc yourself; Sentinel does not. |
| `SENTINEL_GO2RTC_API_PORT` | `1984` | go2rtc's loopback API port. | Only matters if something else on the host already uses it. |
| `SENTINEL_GO2RTC_RTSP_PORT` | `8554` | go2rtc's loopback RTSP port, used by the recorder and motion worker. | Same as above. |
| `SENTINEL_GO2RTC_WEBRTC_PORT` | `8555` | The one go2rtc port that binds on **all** interfaces, because the browser's WebRTC peer connection needs to reach it directly. | With host networking this is also the host port. Make sure it does not collide with another service; MSE playback still works if WebRTC is unavailable. |
| `SENTINEL_GO2RTC_BINARY` | `go2rtc` | Executable name or path. | Resolved with the same lookup as any subprocess call; if it is not on `PATH` and not an absolute path, go2rtc fails to start and streaming/recording never come up — check **Settings → Diagnostics**, which reports whether the binary was found at all. |
| `SENTINEL_MANAGE_GO2RTC` | `true` | Whether Sentinel writes `go2rtc.yaml` and supervises the process itself. | Turn this off only if you run your own go2rtc elsewhere; Sentinel will then talk to whatever is listening at the configured host/ports and never touch it. Getting this wrong (an unmanaged go2rtc that never receives the generated config) means cameras are saved in Sentinel but never actually stream. |
| `SENTINEL_FFMPEG_BINARY` / `SENTINEL_FFPROBE_BINARY` | `ffmpeg` / `ffprobe` | Executables used for recording, motion frame extraction and stream probing. | Missing FFmpeg is the single most common cause of "nothing records." The diagnostics bundle's `tooling` section reports `null` when either is not found. |
| `SENTINEL_FFPROBE_TIMEOUT_SECONDS` | `15.0` | How long a stream probe (add-camera test, or the periodic indexer probe) waits before giving up. | Too short and a slow but working camera is reported as unreachable; the add-camera "Test" button surfaces the exact FFprobe error either way. |
| `SENTINEL_SEGMENT_SECONDS` | `60` | Intended default recording segment length. | **Currently has no effect.** `RecordingManager.start()` always reads the segment length from the database setting `recording.segment_seconds` with its own hard-coded default of 60, ignoring this environment variable entirely (`backend/app/recording/manager.py`). There is also no API or interface control to change the database value yet, so segment length is fixed at 60 seconds in this build regardless of what you set here. |
| `SENTINEL_RECORD_VIA_GO2RTC` | `true` | Recorder and motion worker start from go2rtc's local RTSP instead of the camera directly. | Keep this on normally. A recorder that cannot initialise a particular relayed H.264/NVR stream automatically moves only that camera to its direct URL and remembers the recovered source across restarts; motion and every healthy recorder remain on go2rtc. If the direct client is refused later, the next attempt returns to the relay and clears the saved preference. With this setting off, recorder and motion always open direct connections. |
| `SENTINEL_INDEX_INTERVAL_SECONDS` | `30.0` | How often the indexing sweep picks up newly written segments. | Purely a latency knob for how quickly a just-finished segment appears in the API/timeline; it does not affect recording correctness. |
| `SENTINEL_MOTION_FPS` | `5.0` | Frame rate the motion worker analyses. | Higher catches faster movement at proportionally higher CPU cost; lower can miss something crossing the frame quickly. This is the one motion-related setting that *is* live — see `docs/motion-tuning.md`. |
| `SENTINEL_MOTION_WIDTH` / `SENTINEL_MOTION_HEIGHT` | `640` / `360` | Intended analysis resolution. | **Currently has no effect.** The detector hard-codes its analysis size as `ANALYSIS_WIDTH`/`ANALYSIS_HEIGHT` constants in `backend/app/motion/detector.py`, and the frame extractor (`backend/app/motion/frames.py`) scales to those constants directly rather than reading these settings. Changing them does nothing today. |
| `SENTINEL_ENABLE_MDNS` | `true` | Advertise this Sentinel server as `_sentinel._tcp.local.` for Home Assistant discovery. | Disable it on networks or containers where multicast is blocked or noisy; startup continues even if announcement fails. This controls the server announcement, not ONVIF camera discovery. |
| `SENTINEL_LOG_LEVEL` | `INFO` | Root log level. | `DEBUG` is noisy but never leaks a secret — redaction runs at the logging root regardless of level (`docs/security.md`). |
| `TZ` | `UTC` | Local time in the log. | Read by the logging library for the local-time part of console log lines. Everything else is UTC: every recorder subprocess runs with `TZ=UTC` forced, so segment filenames never drift with daylight saving (`backend/app/recording/recorder.py`), and the interface shows times in each browser's own time zone. |
| `SENTINEL_TESTING` | `false` | Suppresses starting real subprocesses (FFmpeg, go2rtc) so the test suite can exercise the surrounding logic without them. | Never set this in a real deployment — with it on, no camera actually records or streams. |

## Runtime settings (edited through the browser or the API)

These live in the `app_settings` table and are grouped by feature. "Where"
says how you actually change the value in this build — several of these are
implemented end-to-end in the API but have no control in the web interface
yet, which is worth knowing before you go looking for a button that is not
there.

| Setting | Default | Where | What happens if you get it wrong |
| --- | --- | --- | --- |
| `storage.path` | falls back to `SENTINEL_RECORDINGS_DIR` | API only (`PATCH /api/v1/storage/settings`) — the Storage page shows the current path but does not offer a field to change it | Sentinel validates the path before accepting it (creates it, checks it is writable) and refuses an unusable one with a specific reason; it never silently falls back. |
| `storage.retention_days` | `14` | Storage page, and per-camera override on `Camera.retention_days` (API only — no per-camera field in the interface yet) | Set to something absurdly small and Sentinel still never deletes footage younger than two minutes (`MINIMUM_AGE_SECONDS` in `backend/app/storage/retention.py`), so you cannot accidentally wipe what is still being written. |
| `storage.max_bytes` | none (unlimited) | API only (`PATCH /api/v1/storage/settings`, minimum 1 GiB) — not exposed on the Storage page | A limit set below what is already on disk triggers an immediate oldest-first sweep down to the limit on the next retention tick, respecting protection. |
| `storage.min_free_bytes` | 5 GiB | Storage page ("Always keep free") | The default fits the installer's 16 GiB root disk while retaining enough room for a ten-minute sweep interval. Set to `0` and Sentinel can fill the disk until FFmpeg fails; increase it on a large/high-write recording volume. |
| `recording.segment_seconds` | `60` | Nothing writes this today — no router calls `set_setting` for it | See the `SENTINEL_SEGMENT_SECONDS` row above; effectively fixed at 60 seconds. |
| `motion.pre_roll_seconds` | `5` | API-implemented (read by `backend/app/motion/service.py`) but no endpoint exposes it for writing | Controls how much footage before the first confirmed motion frame is included in an event's playback window. |
| `motion.post_roll_seconds` | `10` | Same as above | Controls how much footage after motion ends is still counted as part of the event. |
| `motion.end_grace_seconds` | `8` | Same as above | How long motion must be absent before an event is considered finished — see `docs/motion-tuning.md` for what this actually changes in practice. |
| `system.timezone` | `UTC` | Nothing writes this either | Read once by `GET /api/v1/system` and returned as-is; always `"UTC"` in this build. |
| `setup.completed` | `false` | Nothing writes this | Reserved for a first-run wizard flag; the interface currently decides whether setup is needed by checking whether any user account exists (`GET /api/v1/auth/status`), not this flag, so it stays `false` forever without affecting anything. |

If you need to change one of the settings above that has no interface control
yet, the API accepts it directly. For example, to raise the total-size cap
with an admin session already open in the browser (so the session cookie is
already set), open the network tab and issue the request from there, or use
an API token:

```bash
curl -X PATCH https://sentinel.example.lan/api/v1/storage/settings \
  -H "Authorization: Bearer sctv_..." \
  -H "Content-Type: application/json" \
  -d '{"max_bytes": 2000000000000}'
```

There is currently no endpoint to change `motion.pre_roll_seconds`,
`motion.post_roll_seconds`, `motion.end_grace_seconds`,
`recording.segment_seconds` or `system.timezone` — changing those means
writing directly to the `app_settings` row in the SQLite database while
Sentinel is stopped, which is not something to do casually and is not
documented further here on purpose.

## Sane values for a 4-camera install

The defaults above need nothing changed. `SENTINEL_GO2RTC_WEBRTC_PORT` (8555)
and the API port (8090) are the only two that need to reach the browser; the
`compose.yml` ulimit (`nofile: 8192`) and memory ceiling (`3g`) that ship by
default are comfortably above what four cameras and their substreams need.

## Sane values for a 16-camera install

- **File descriptors.** `compose.yml`'s own comment explains the ceiling:
  *"FFmpeg opens a handful of file descriptors per camera; the default soft
  limit of 1024 is tight once you pass roughly twenty streams."* Sixteen
  cameras with a substream each is already 32 go2rtc streams, so keep the
  shipped `ulimits.nofile` (8192) rather than lowering it.
- **Memory.** The `3g` limit in `compose.yml` is a ceiling, not a
  reservation. Sixteen motion workers plus sixteen recorders is a real but
  modest amount of process overhead on top of whatever OpenCV and FFmpeg use
  per stream; raise the limit if the container is ever OOM-killed rather than
  lowering camera count first — check **Settings → Diagnostics** for the
  actual RSS before guessing.
- **`storage.min_free_bytes`.** The 5 GiB default is deliberately compatible
  with the installer's small root disk. At sixteen cameras writing
  continuously, retention runs every ten minutes (`RETENTION_TICK_SECONDS` in
  `backend/app/recording/manager.py`) and the write rate can consume most of
  that margin between sweeps. Raise it substantially when recordings live on a
  dedicated, larger volume.
- **CPU.** Nothing in this codebase has been measured on a real fleet this
  size — the README's own guidance ("4 cores is comfortable for ~10 cameras
  with stream copy") is the only load figure that exists, and motion
  detection at 5 fps/640×360 adds roughly another 1% of one core per camera
  on top of that. Sixteen cameras is beyond what has actually been measured;
  treat the figure as a starting point to watch, not a guarantee.
