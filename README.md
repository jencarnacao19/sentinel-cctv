<div align="center">

<img src="frontend/public/favicon.svg" width="72" height="72" alt="">

# Sentinel CCTV

**Local-first video surveillance for RTSP cameras.**
Continuous recording, traditional motion detection, a real timeline, and a
[native Home Assistant integration](#home-assistant-integration). Runs locally
without a GPU.

</div>

---

> **Status: early development (`0.1.0`).** Continuous RTSP recording, motion
> events, timeline playback and protected Home Assistant embedding are available.
> Build from source and validate your cameras and storage before relying on the
> installation. See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for current
> capabilities and limitations.

## Why

Sentinel records camera streams continuously and marks motion on a searchable
timeline. It is designed for a small local server, with camera configuration,
playback and storage management available in the browser.

- **Continuous recording first.** Losing footage is the only unrecoverable
  failure. Everything else is a feature.
- **Video stream copy, not transcode.** With `-c:v copy` a camera costs a few
  percent of one core, so ten cameras run comfortably on a small home server.
  Only optional audio is normalised to AAC for MP4 compatibility.
- **Motion detection.** OpenCV background subtraction on the
  substream at 640×360 and 5 fps — enough to mark the timeline, cheap enough to
  ignore.
- **Nothing to hand-edit.** No `configuration.yaml`, no `go2rtc.yaml`, no
  `docker-compose` surgery. One command to install, then a browser.
- **Local recording and camera control.** No telemetry or analytics. Core
  recording works independently of Home Assistant and remote-access services.

## Architecture

```
CAMERAS / NVR
     │ RTSP / ONVIF
     ↓
   go2rtc            one connection per camera, shared by everything
     │
     ├──────────────────┐
     ↓                  ↓
RECORDING           MOTION WORKER
FFmpeg -c:v copy    OpenCV MOG2
main stream         substream 640x360 @ 5fps
     │                  │
     ↓                  ↓
  STORAGE          EVENT DATABASE
     └────────┬─────────┘
              ↓
        SENTINEL API  (FastAPI + SQLite WAL)
              │
      ┌───────┴────────┐
      ↓                ↓
 Web interface    Home Assistant
```

Cameras normally connect exactly once. go2rtc fans that stream out to the
recorder, motion worker and browser. If an NVR channel does not provide the
H.264 setup data a recorder needs after joining the relay, only that recorder
automatically retries the direct camera URL; motion and every healthy camera
remain shared through go2rtc. Sentinel remembers that compatible source across
restarts and returns to the relay automatically if the direct client later
fails. Home Assistant receives snapshots and event
metadata through Sentinel's authenticated API rather than opening another RTSP
connection.

## Requirements

| | |
| --- | --- |
| Host | Proxmox VE, or any machine that runs Docker |
| CPU | 4 cores is comfortable for ~10 cameras with stream copy |
| RAM | 4 GB |
| OS disk | 16 GB |
| Recordings | Separate storage, sized for your retention |
| GPU | **Not required.** Not used at all. |

## Installation

### Proxmox

There are two supported deployment paths:

- **Source deployment** — build the image inside the LXC from a local checkout.
  Follow the [source-build guide](docs/private-proxmox.md).
- **Published image deployment** — use `install-proxmox.sh` after a GHCR image
  has been published at the image name configured in the script.

The installer creates an unprivileged Debian LXC, installs Docker and Sentinel,
and prints the URL. It never formats a disk and never deletes recording storage.
Always inspect and dry-run it before use:

```bash
./install-proxmox.sh --print-config --storage local-lvm
./install-proxmox.sh --dry-run --storage local-lvm
```

For a movable Proxmox layout, keep the LXC root disk on `local-lvm` and attach
recordings as a separate managed mount point at `/recordings`. The root disk and
recording volume can then be moved independently from the Proxmox interface.

### Docker

```bash
git clone https://github.com/jencarnacao19/sentinel-cctv.git
cd sentinel-cctv
docker compose -f compose.yml -f compose.source.yml up -d --build
```

Once the image configured in `compose.yml` is available from your registry,
you can use `docker compose up -d` without the source-build override.

Then open `http://<host>:8090` and create your administrator account.

### Home Assistant integration

> **The Home Assistant integration needs this Sentinel server.** It does not
> record or connect to cameras by itself: it connects to a running Sentinel
> server and brings its cameras into Home Assistant. Install the server first,
> using one of the methods above.

The integration lives in this repository under
[`custom_components/sentinel`](custom_components/sentinel). Install it through
HACS: **HACS → three-dot menu → Custom repositories**, add
`https://github.com/jencarnacao19/sentinel-cctv` with category **Integration**,
install **Sentinel CCTV** and restart Home Assistant. Sentinel is then
discovered automatically, or can be added under **Settings → Devices &
services** with an API token created in Sentinel.

Cameras appear with snapshots, motion sensors, recording status and a
recording switch. See the [Home Assistant guide](docs/home-assistant.md) for
manual installation, the embedded dashboard and troubleshooting.

### Restricted footage operator

Sentinel can make `/` a footage console while keeping every configuration route
under `/admin` behind the administrator login. This is deliberately not another
account system. The operator grant can list enabled cameras, watch live video,
operate cameras whose ONVIF PTZ support has been detected, use the timeline and
playback, download footage and create permanent saved clips. It cannot
discover/add/edit/delete cameras, change retention or motion settings, manage
credentials/tokens, delete exports or remove their protection.

For the main remote hostname, put Sentinel behind Cloudflare Access and enable
only that exact hostname:

```dotenv
SENTINEL_VIEWER_ENABLED=true
SENTINEL_VIEWER_TRUSTED_HOSTS=cctv.example.net
SENTINEL_VIEWER_CLOUDFLARE_ISSUER=https://your-team.cloudflareaccess.com
SENTINEL_VIEWER_CLOUDFLARE_AUDIENCES=your-application-audience-tag
```

The exact hostname and a signed Cloudflare Access JWT must be present. Sentinel
checks the configured issuer, audience, signature and expiry against that team's
public signing keys. Missing verification settings disable this grant, not the
normal Sentinel login or independent viewer key. Obtain the team domain and
Application Audience (AUD) from your own Access application; do not invent them.
The origin must remain private behind the tunnel; never expose port `8090` directly.

The native Home Assistant integration should use Sentinel's LAN URL directly,
for example `http://<sentinel-lan-ip>:8090`. An HTTPS Home Assistant page cannot
embed that HTTP address because browsers block mixed content. For the Webpage
dashboard, create a more-specific Cloudflare Access application that bypasses
only `/embed*`, `/assets/*`, `/sw.js`, `/api/v1/viewer/*` and
`/api/v1/streams/*`. Sentinel's random viewer key remains mandatory for the
viewer APIs. The main `/` and every administration route stay behind the normal
Cloudflare Access application. This needs no router change or split DNS.

Live video uses MSE on desktop browsers. Safari/Home Assistant WebViews first
try protected WebRTC for low latency on the LAN; if a mobile network cannot
reach the private WebRTC media candidate, the player automatically switches to
same-origin HTTPS HLS through the existing tunnel. This fallback opens no
router port, does not add a TURN/cloud-video dependency and exposes neither the
go2rtc interface nor the long viewer key. All three paths retain the same
viewer-only authorisation boundary.

Generate a separate random key of at least 32 characters for the embedded
Webpage dashboard and pass it as a deployment secret:

```dotenv
SENTINEL_VIEWER_KEY=<random value, never commit this>
```

Configure the dashboard URL as
`https://cctv.example.net/embed#viewer_key=<same value>&embed=1`. The fragment is not
sent to Sentinel, Cloudflare or HTTP logs. The frontend saves it only for that
browser session, removes it from the visible address and exchanges it once for
an `HttpOnly` viewer-session cookie. To survive iOS and Home Assistant WebViews
that block third-party cookies, restricted API calls also send the key in the
viewer-only header and live video uses a validated WebSocket subprotocol. The
key never appears in an HTTP or WebSocket URL.
Set `SENTINEL_FRAME_ANCESTORS` to the exact HTTPS Home Assistant origin and, if
the mobile app uses it on Wi-Fi, the exact private LAN HTTP origin. Do not
create an unprotected public alias: without the viewer key or Access assertion,
the footage API returns `401`.

### ONVIF PTZ controls

Open **Cameras → Detect PTZ** as an administrator. Sentinel validates the
camera's ONVIF service without moving it and stores only its service address and
media profile token. Supported cameras then gain pan, tilt and stop controls,
plus zoom only when the camera advertises it, in both the administrator Live
page and the Home Assistant/operator camera wall; fixed or incompatible cameras
get no control overlay.

Movement starts on pointer-down/touch-down rather than waiting for a click. The
browser renews a held direction every 450 ms over the restricted operator API,
while Sentinel uses a short-lived LAN connection compatible with consumer ONVIF
servers. Releasing, losing focus or hiding the page sends an immediate all-axis
stop. The server has an independent 1.1-second dead-man stop, so a dropped phone
connection cannot leave a camera rotating. Sentinel omits ONVIF's optional
movement timeout because some Tapo models reject it.

Open **Cameras → Positions & patrol** to move the camera, save its current
position, revisit named presets and build a patrol from two or more positions.
When the camera advertises ONVIF preset tours, Sentinel stores and runs that
tour on the camera itself. Manual PTZ pauses it and the safety-critical Stop is
sent before the tour resumes. Cameras without native tours keep their presets
and manual PTZ; a configured Tapo cruise can remain the LAN-only fallback.

For Tapo pan/tilt cameras, **Cameras → Configure → Resume Tapo patrol after
PTZ** can restore the camera's proprietary horizontal or vertical patrol after
the operator releases a movement control (and after the server dead-man stop).
The ONVIF Stop remains immediate; patrol resumes in the background using the
camera's LAN interface. Leave this setting off for other brands. First enable
**Tapo app → Tapo Lab → Third-Party Compatibility** (or
**Settings → Third-Party Compatibility**, depending on the app version). If
the camera still reports invalid authentication, open **Cameras → Tapo
controls** and enter the password used to sign in to the Tapo app. Sentinel
encrypts it separately, never returns it through the API, and uses it locally
with the fixed username `admin`; the email address is neither requested nor
sent.

**Tapo LAN controls** appear only on cameras Sentinel believes are TP-Link
devices. It decides from the camera's own manufacturer and model — use **Identify
camera** in a camera's overflow menu to read those over ONVIF — and from the
vendor-specific shape of its stream address. It never decides from the address
itself: several channels of one recorder share one, so an NVR channel sitting
next to a real Tapo camera is not evidence of anything.

A camera that reports no manufacturer is shown as unidentified. If you know it
is a Tapo camera, set **Enable Tapo LAN controls** to *Always on* in
**Configure**; that setting is a three-way choice, and *Automatic* — the default
— means "decide from what the camera says".

Opening the panel performs LAN capability discovery and shows only functions the
individual camera confirms: status LED, physical lens privacy, day/night mode,
subject tracking, floodlight, supported on-camera detections, SD-card status and
patrol/preset status. Siren and reboot appear only when supported and require a
second confirmation. Firmware updates, SD formatting and factory reset stay
excluded. If part of the discovery does not answer, the controls that did are
still shown and the panel says how many checks were missed — one awkward
firmware method does not cost you the whole panel.

The **Camera Account** created in the Tapo app remains the credential for
RTSP/ONVIF on compatible mains-powered cameras. TC82/H200 battery cameras do
not expose that account or RTSP/ONVIF, so their separate add-camera flow asks
whether the address belongs to a standalone TC82 or an H200. Standalone media
keeps the fixed `admin` username internal; an optional local username appears
only for hub firmware that requires it.

The control plane is local-first by construction: RTSP recording, ONVIF PTZ,
presets and native tours never call an external service. Optional remote/cloud
features must run outside this request path and may fail without changing local
video, recording or camera control.

### Turning one camera's recording on or off

Each ordinary camera row on the **Cameras** page carries a recording switch, so
you do not have to open **Configure** to see whether a camera is recording or to
stop it. The state shown is the recorder's, not the setting's: a camera that is
configured to record but whose recorder did not start reads *Failed to start*,
and one held by privacy mode reads *Paused by privacy mode*.

Stopping asks for confirmation, because footage from the paused period cannot be
recovered afterwards. It affects that one camera's recorder and nothing else —
live view, ONVIF, motion detection, every other camera and global privacy mode
carry on unchanged. Recordings already on disk are kept. The setting survives a
restart, and the camera's recording profile, source and segment length are left
exactly as they were, so turning it back on resumes with the same configuration.

### Tapo battery cameras (experimental)

TC82-class battery cameras do not expose RTSP or ONVIF and therefore are not
added to Sentinel's continuous pipeline. Set
`SENTINEL_ENABLE_TAPO_BATTERY_PROVIDER=true`, restart Sentinel, then choose
**Cameras → Add manually → Tapo battery**. Use **Test connection** before saving.
Choose **Standalone TC82** for the camera's own private address, or **H200 hub**
when the address belongs to the hub and select its discovered child camera. The
standalone flow never asks for an account email.

The provider reads battery state over the LAN, opens one time-limited Direct
Stream only when an administrator requests it, and manually imports recent
camera-owned event clips into the normal timeline. It never registers the
camera with go2rtc, starts FFmpeg recording/motion analysis, or needs Internet
access while operating. The protected Home Assistant viewer can play imported
events; it deliberately does not keep a battery-camera live session open.

Firmware and hub behaviour vary, so a real-device test remains mandatory. See
[`docs/TC82_LOCAL_PROVIDER.md`](docs/TC82_LOCAL_PROVIDER.md) for the exact
boundaries and validation checklist.

### Privacy mode

An administrator can use **Privacy mode** in the navigation footer to pause
every camera inside Sentinel and choose the explanation shown instead of each
feed. This is enforced on the server, not as a cosmetic overlay: existing live
sessions close, go2rtc stops, recorders and motion workers stop, and new
snapshot, probe, discovery and PTZ requests are refused. The choice persists
across reboot and a failed resume stays paused. Existing playback and saved
clips remain available, camera power is unchanged, and the feature has no cloud
dependency.

PTZ changes the physical view, so the controls are available only to an
authenticated administrator or the same protected footage-operator grant used
for live/playback. They are never exposed by an unauthenticated public route.

### Timeline and storage intelligence

The playback workspace is designed for both an operator at a desktop and a
phone inside Home Assistant. It provides:

- one-tap 1 hour, 6 hour, 24 hour and 7 day windows;
- date/time jumping, previous/next windows and a return-to-now control;
- coverage, gap and motion summaries for the visible range;
- a chronological motion-event rail where every item is a playback target;
- keyboard, mouse and touch scrubbing, zooming and drag-to-select;
- 10-second seek, previous/next motion, 0.5x through 16x playback, a
  Home Assistant-friendly fullscreen control and permanent clip presets;
- the same playback, save and download capabilities for the restricted footage
  operator, without exposing configuration actions.

The operator header also exposes a deliberately limited system summary: camera
and recorder health, disk percentage/free space, recent recording rate and
projected retention. This route still requires a viewer grant and never returns
camera hosts, stream URLs, credentials, filesystem paths or mutable settings.
The administrator Storage page adds the private detail: exact mount path,
recording/snapshot/database breakdown, protected and damaged footage, oldest and
newest indexed video, current ingest rate, projected days remaining and usage
per camera.

### Phones and tablets

The web interface is responsive but deliberately does not cache itself for
offline use: stale CCTV state is less safe than an explicit connection error.
On narrow screens it uses a
drawer instead of the desktop sidebar, keeps navigation visible while scrolling,
honours notches and home-indicator safe areas, and provides at least 44 px touch
targets for controls. Forms, camera cards and actions collapse to the available
width without horizontal page scrolling. Live video and the timeline use native
inline playback and touch-aware controls.

### Permanent saved clips

From **Timeline**, choose the previous 15 seconds, 30 seconds, 1 minute or 5
minutes, or drag any custom time range and select **Save permanently**. Sentinel creates a
stand-alone MP4 in the recording volume and shows it under **Saved clips**, where
an administrator can download it at any time.

Saved clips are permanent by default: the age, size and free-space retention
sweeps may delete their original recording segments, but never the saved MP4.
The footage operator and administrator can download it; only an explicit
administrator action can delete it or mark it temporary. A temporary clip
expires after 24 hours. This protects against Sentinel's
automatic cleanup, not against deleting or losing the whole recording volume;
include that volume in infrastructure backups when the footage is irreplaceable.

## Operations

The Proxmox source-build guide installs the helper command `sentinel-ctl`:

```bash
sentinel-ctl status
sentinel-ctl health
sentinel-ctl logs
sentinel-ctl restart
sentinel-ctl update
```

Configuration, the SQLite database, encryption material and snapshots live in
the Docker volume `sentinel-data`. Footage lives at `/recordings`. Back up both;
a Proxmox backup of only the root disk does not include a recording mount point
that has been excluded from backups.

Do not expose port `8090` directly to the internet. Use a trusted VPN or reverse
proxy and keep the camera network reachable only from Sentinel.

## Development

Backend and frontend run independently; the Vite dev server proxies `/api` and
`/ws` to the backend.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps --no-build-isolation -e ".[dev]"
SENTINEL_DATA_DIR=./data SENTINEL_ENABLE_API_DOCS=true \
  uvicorn app.main:app --reload --port 8090
```

```bash
cd frontend
npm install
npm run dev
```

Checks, all of which run in CI:

```bash
cd backend && ruff check . && ruff format --check . && mypy && pytest
```

```bash
cd frontend && npm run lint && npx tsc -b && npm test && npm run build
```

## Security

- Passwords are hashed with **Argon2id**.
- Camera credentials are **encrypted at rest** with a key derived from a local
  secret created at install time, stored `0600`. They are never returned by the
  API, never rendered in the interface and never written to a log.
- Every log line passes through a **redaction filter** that removes RTSP
  userinfo, `password=`/`token:` pairs, `Authorization` values and Sentinel API
  tokens.
- Administrator sessions use `HttpOnly; SameSite=Lax` cookies. The separate
  embedded-viewer cookie is `HttpOnly; Secure; SameSite=None` so an HTTPS Home
  Assistant iframe can use it; its narrow capability still cannot reach any
  administration route. A header/WebSocket-subprotocol fallback keeps the
  embedded viewer working when an iOS WebView blocks that cross-site cookie;
  neither mechanism is accepted by administration dependencies.
- Restricted footage access is a separate capability, not a user or API token.
  Its responses omit RTSP URLs, hosts, usernames, recording policy and settings;
  the Home Assistant viewer key arrives in a URL fragment and is never accepted
  by an administration dependency. It may play/download footage and create
  protected exports, but cannot change configuration or delete saved footage.
- CORS is closed by default — the interface is served from the same origin as
  the API.
- Sign-in and token creation are rate limited.
- **Settings → Public login protection** can persistently block a verified
  Cloudflare visitor address after a configurable number of failed
  administrator sign-ins. The default threshold is one and the duration can be
  permanent or timed. Sentinel accepts `CF-Connecting-IP` only from an exact
  `SENTINEL_TRUSTED_PROXY_CIDRS` peer, never from arbitrary LAN traffic. The
  block applies only to sign-in on the configured public hostname; direct LAN
  login remains the recovery path, and the admin panel lists and removes blocks.
- The API binds to the LAN only. Sentinel does not configure port forwarding and
  does not talk to your router. For remote access, use the VPN or reverse proxy
  you already trust.

Found a security problem? See the [security policy](SECURITY.md) for the
security maintainer and confidential reporting instructions.

## Licence

[MIT](LICENSE) © João Encarnação
