# Changelog

All notable changes to Sentinel CCTV are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is in early development. Current capabilities and validation
requirements are described in [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## [Unreleased]

## [2.0.1] - 2026-10-31

### Fixed

- The Home Assistant integration loads every camera on Home Assistant 2026.10.
  That release refuses devices linked with `via_device`, so every camera but one
  failed with "Error adding entity None for domain camera"; camera devices now
  link to the server with `via_device_id`, and older releases keep `via_device`.
- The camera wall opens without waiting for the administrator sign-in status.
  Behind an access proxy that keeps the administration API private, as for the
  Home Assistant panel, that request failed and was retried a second later
  before any camera started.

## [2.0.0] - 2026-10-18

A major version because two changes can break an existing set-up: the
motion-zones API has a new format, and Tapo battery cameras leave releases for
the dev build.

### Added

- **Setup in the browser.** After the administrator account, Sentinel walks a
  new installation through where recordings go and for how long (warning when
  that is the small system disk), its cameras, accounts for the family and Home
  Assistant, then opens the camera wall. Importing a configuration file from
  another Sentinel and the Home Assistant step are optional. Installations from
  before 2.0 are not sent through it.
- **Viewer accounts.** The administrator creates accounts, during setup or under
  **Settings → Viewer accounts**, for people who only watch: they sign in at
  Sentinel's address to see the cameras, recordings and saved clips, and cannot
  change anything. The camera wall asks them to sign in, and offers a sign-out.
- **The installer asks which disk holds the recordings.** It lists the disks
  the Proxmox host has mounted, with their free space, marks the one Proxmox
  runs from, and creates a `sentinel-recordings` folder on the one chosen.
  Nothing is formatted or mounted.
- **The address to open is easy to find.** The server log says which address
  to open, and the Proxmox container's console shows it at the login prompt.
- **Named motion zones.** On **Motion zones**, each area is now a detection
  zone or an ignored area. A detection zone has a name and can have its own
  sensitivity, and once a camera has one, only motion inside its detection zones
  counts. Detection still runs on the CPU with one background model per camera,
  however many zones it has. Motion events record the zones they touched.
- **A Home Assistant motion sensor per zone.** Each detection zone appears as
  `binary_sensor.<camera>_<zone>_motion`, pushed the moment it changes, and
  fires `sentinel_zone_motion_started` and `sentinel_zone_motion_ended`.
  Renaming a zone keeps its entity.
- **Move to another machine.** **Settings → Move to another machine** exports
  every camera, its zones and the recording settings to one YAML file and
  imports it on another server. Camera passwords are left out unless you set a
  passphrase; then they are encrypted with AES-256-GCM under a key derived from
  it with scrypt. Importing only adds, and skips a storage policy that could
  delete footage on the new machine.
- **Ready-to-run server images.** Each release now publishes
  `ghcr.io/jencarnacao19/sentinel-cctv` for `amd64` and `arm64`, and the quick
  start installs from that image instead of building from source.
- **A dev image for work in progress.** Every push to the `dev` branch publishes
  `ghcr.io/jencarnacao19/sentinel-cctv:dev` for `amd64`, for test servers only.

### Changed

- `GET /api/v1/auth/status` adds `setup_completed`, and signing in returns
  `is_admin`, so the interface can send a viewer account to the camera wall.
- **The motion-zones API has a new format (breaking).**
  `/api/v1/cameras/{id}/motion-zones` now exchanges `{"zones": [...]}` with zone
  objects (`id`, `name`, `kind`, `enabled`, `sensitivity`, `points`) instead of
  a bare list of polygons. Zones saved by earlier versions were areas to ignore
  and load as `ignore` zones.
- Motion events, the realtime event stream and the Home Assistant contract
  carry the zones involved.

### Removed

- **Tapo battery cameras (TC82/H200) are no longer in releases.** Their
  development continues on the `dev` branch and its `:dev` image. A release
  refuses to add one. A battery camera the dev build added stays in the
  database untouched and is listed as not supported, so it can be removed, or
  used again by going back to the dev build.
  `SENTINEL_ENABLE_TAPO_BATTERY_PROVIDER`, `SENTINEL_TAPO_BATTERY_LIVE_MAX_SECONDS`
  and `SENTINEL_TAPO_BATTERY_SYNC_MAX_CLIPS` are ignored.
- `SENTINEL_TIMEZONE`, which nothing read. `TZ` still sets the time in the log;
  recordings are always stored in UTC.

### Fixed

- **A camera's own retention period now applies when the global one is
  unlimited.** Its recordings and motion events stayed until a size or
  free-space limit removed them.
- **A saved clip can no longer use the space kept free for recording.** Saved
  clips are never deleted by retention, so a clip that would cross that reserve
  is refused with a message, instead of retention deleting ordinary footage to
  make room for it.

### Security

- **Viewer accounts cannot reach administration.** To every route outside the
  viewer a viewer account is signed out; a test sends its session to every
  route in the API and requires the answer an anonymous client gets.
- **Live-stream and event sockets refuse other websites.** A browser's
  `Origin` must now match the host the socket was opened on. Before, a page on
  another site could open the viewer's live-stream socket with a household
  member's viewer cookie (which is `SameSite=None` for the Home Assistant
  iframe) and read the video, given a camera's id.
- **ONVIF replies can no longer steer Sentinel's requests.** The Media service
  address a device reports is kept on that device's own host, and discovery
  never contacts this machine's loopback. A device on the network could
  previously point Sentinel at go2rtc's API, whose exit endpoint stopped every
  live view and recording.
- **go2rtc's API registers only the paths Sentinel uses.** Its exit, restart,
  config and log endpoints no longer exist, and it no longer sends a CORS
  header to every origin.
- **Stored stream URLs are checked where they are used.** A non-RTSP address in
  the database is ignored instead of being written into go2rtc's configuration,
  and a backup containing one is refused. A hand-edited backup with an
  `exec:` source could previously run a command inside the container once
  restored. Stored PTZ addresses are pinned to the camera on every use.
- **Dependencies:** PyJWT 2.15.0, urllib3 2.8.0 and multidict 6.9.1 replace
  versions with published advisories; Vitest 5 and refreshed development
  dependencies clear the remaining `npm audit` findings.

## [0.1.0] - 2026-10-17

### Added

- **The Home Assistant integration ships with the server.** It now lives in
  this repository under `custom_components/sentinel` and installs through HACS
  as a custom repository. It still needs a running Sentinel server; see the
  [Home Assistant guide](docs/home-assistant.md).

- **Administrators can manage large motion-event libraries explicitly.** The
  Events page groups history by camera and adds age/duration filters, exact
  row/file/byte preview, typed confirmation, background progress, cancellation
  and an erase-all action. This workflow removes event metadata and managed
  event images only; continuous recordings, saved exports, active events and
  detections created while it runs are preserved.

- **Cameras are identified before they are offered a vendor's controls.**
  Sentinel decides whether the Tapo LAN controls apply from the camera's own
  manufacturer and model — read over ONVIF with the new **Identify camera**
  action, or entered by an administrator — and from the vendor-specific shape of
  its stream address. A camera's IP address is never part of that decision,
  because several channels of one recorder share it. Cameras that report nothing
  are shown as unidentified and offered an explicit **Enable Tapo LAN controls**
  setting rather than a button that would fail. Previously every RTSP camera
  showed a *Tapo controls* button even on cameras from other vendors.

- **A recording switch on each camera row.** Administrators can stop and start
  one camera's recorder without opening Configure, and see whether it is
  actually recording rather than what was configured — a recorder that failed to
  start reads *Failed to start*, not *On*. Stopping asks for confirmation and
  says plainly that the footage cannot be recovered. It moves that one recorder
  and nothing else: live view, ONVIF, motion detection, the other cameras and
  global privacy mode are untouched.

- **Timed manual recording for Tapo battery cameras.** An administrator can
  record a TC82/H200 for a bounded period — 5, 10, 15, 30 or 60 minutes, or a
  custom duration up to an administrator-set ceiling — after being shown the
  current battery percentage and explicitly accepting that the camera stays
  awake and may end up flat. The session reports its countdown, bytes captured
  and connection state, and can be stopped at any time. Media is written as it
  arrives and remuxed into a playable MP4 that is indexed in the ordinary
  timeline and marked as a manual Tapo battery recording, so it plays in
  Sentinel and Home Assistant like any other clip.

  Partial results are kept and reported honestly. If the firmware closes the
  stream, Sentinel reopens it over the LAN with bounded backoff — never through
  the TP-Link cloud. If the camera runs out of battery the clip is saved and the
  result says *"Camera powered off before the requested duration."*; the full
  duration is never claimed. Privacy mode and server shutdown both end the
  session and keep what was captured, and a session interrupted by a crash is
  finalised and indexed at the next start-up. Only one live view or timed
  recording may be open per camera, and nothing ever starts one automatically.

### Fixed

- **Motion sensitivity and event aggregation now affect the real failure
  modes.** The sensitivity slider controls MOG2's variance threshold rather than
  only thresholding an already-binary mask. Three spatially-related detections
  are required to open an incident, and the default end grace is eight seconds,
  so compression artefacts on opposite sides of a frame do not combine and a
  short occlusion does not fragment one incident into several rows.

- **TC82 Direct Stream is prepared for real browsers instead of forwarded
  raw.** On-demand local video is normalised to H.264/yuv420p at no more than
  1080p, and the camera's separate PCMA/PCMU audio is joined as AAC. This avoids
  black playback when the battery camera sends HEVC and gives live/timed TC82
  media one consistent local format without adding cloud or continuous polling.

- **Retention now reclaims motion evidence, not only footage.** Recording
  segments were pruned; the full-resolution snapshot and thumbnail saved for
  every motion event never were. Because the sweep accounted only for indexed
  recording segments, these images could fill the disk that also held the
  database while retention still reported sufficient recording capacity.

  Motion events are now pruned in the same pass and on the same policy as
  footage, oldest first, honouring per-camera retention overrides, never
  touching a protected event, and deleting each file before its row so a crash
  cannot leave an orphan nothing owns. The sweep reports events separately from
  segments, so "retention deleted forty things" can no longer hide the fact
  that all forty were thumbnails and no footage was reclaimed.

- **A camera error is no longer reported as a proxy error.** Opening the live
  view of a battery camera whose credentials the camera rejected produced
  `200 OK` with a `video/mp2t` content type and an empty body, because the
  vendor session was opened inside the streaming response after the status line
  had already been sent. Any reverse proxy in front of the origin reports that
  as a broken upstream — through Cloudflare, as *"The origin web server returned
  an invalid or incomplete response."* The session is now opened before the
  response starts, so a rejected credential is a `502` carrying Sentinel's own
  message and an unreachable camera is a `503`. The interface never renders a
  proxy's error page as a camera fault.

- **One unresponsive camera no longer slows down the whole application.**
  Blocking vendor calls ran on the interpreter's shared thread pool, and a
  timed-out call cannot be cancelled — it keeps its worker until the operating
  system gives up on the socket. Repeated timeouts could therefore delay
  unrelated API requests beyond a reverse proxy's response window. Vendor calls
  now run on their own small pool.

- **The ONVIF patrol path was still on the shared thread pool.** The fix above
  moved the Tapo control and battery paths onto their own pool but missed
  `app/cameras/ptz.py`, whose patrol restoration constructs a vendor client —
  a blocking, uncancellable network call — and retries every thirty minutes.
  Repeated authentication failures could exhaust the shared pool. All six of
  its thread offloads now use the vendor pool, and the regression test covers
  `ptz` as well.

- **Camera control requests are bounded well inside a proxy's origin window.**
  A capability discovery could take 70 seconds against an unresponsive camera,
  against a 20-second client timeout. Every Tapo request now shares a 14-second
  deadline across all attempts and fallbacks, so a slow camera produces a
  Sentinel error rather than a proxy page or a silent client timeout.

- **One unsupported capability no longer blanks the whole panel.** Sentinel now
  builds its own vendor request from the twenty-four methods it can actually
  use, instead of the library helper's eighty-seven, and splits a batch the
  camera rejects until the offending method is isolated. Confirmed controls stay
  visible and usable, the panel says how many checks did not answer, and a
  capability is only dropped when the camera answered and said no.

- **Loading skeletons no longer sit above an error for ever.** The Tapo control
  panel gated its state request on a fully successful discovery; a disabled
  query reports as pending indefinitely, so a failed discovery left two
  skeletons permanently above the error message. Errors are now specific —
  authentication rejected, camera timeout, unsupported firmware — and each says
  what to do next.

- **Broken thumbnails no longer show their alt text.** A camera reporting itself
  online whose snapshot failed rendered the browser's broken-image glyph beside
  the words *"Latest frame from …"*. Thumbnails now fall back to a neutral
  camera icon that names the actual state. The snapshot URL also embedded
  `Date.now()` evaluated during render, so any unrelated state change refetched
  a JPEG from every camera on the page; the cache key is now owned by the caller
  and only changes when a fresh frame is wanted.

### Changed

- **The Cameras page was rebuilt around what is used daily.** Each camera keeps
  its recording switch, Configure and Test as buttons; everything occasional —
  Tapo controls, Identify, PTZ detection, positions and patrol, removal — moved
  into one labelled overflow menu with full keyboard support and focus return.
  Brand-specific actions appear only where they apply. The card is a grid, so on
  a phone the thumbnail stays a small identifier instead of a 16:9 hero image
  that pushed the next camera off the screen. States are named rather than
  implied: *Recording*, *Off*, *Paused by privacy mode*, *Failed to start*.

- **Correct and discoverable local Tapo controls.** The battery-camera flow now
  distinguishes a standalone TC82 from an H200. Standalone media uses fixed
  username `admin` and never asks for an email; only the hub topology can expose
  an optional local username for firmware that requires it. Neither path sends
  credentials to a cloud endpoint.
  A confirmed Tapo RTSP camera offers a controls panel that can store this
  separate encrypted password, discover only capabilities confirmed by that
  camera, and expose LED, physical privacy, day/night, tracking, floodlight,
  supported on-camera detections, SD status, patrol/preset status, siren and
  reboot. Physical siren and reboot actions require a second confirmation.

- **Experimental local Tapo battery-camera provider.** TC82-class cameras can be
  represented honestly as event sources instead of fabricated RTSP cameras.
  Behind a disabled-by-default feature flag, Sentinel can prove local
  authentication and battery state, open one bounded Direct Stream only on
  request, and import camera-owned event clips into the ordinary timeline.
  These cameras are excluded in code and in the database contract from go2rtc,
  continuous recorders, motion workers, motion zones and RTSP reliability
  scoring. Imported clips remain available to the protected Home Assistant
  playback interface. Privacy mode blocks every camera operation.
- **Persistent global privacy mode.** An administrator can pause every Sentinel
  camera with one control and choose a predefined explanation shown in place of
  live feeds. The server closes existing viewers, stops go2rtc, recorders and
  motion workers, and blocks snapshots, stream probes, discovery and PTZ until
  cameras are resumed. The state survives reboot, fails safe on an incomplete
  transition, keeps historical playback/clips available and never calls a cloud
  service.
- **Camera-owned ONVIF positions and patrol.** Administrators can move a camera,
  save and revisit named presets, select patrol stops and start/stop an ONVIF
  preset tour from the camera page. Capability discovery records the camera's
  real preset, home and tour support. Native ONVIF patrol is preferred and
  automatically resumes after manual PTZ; the existing Tapo LAN cruise command
  remains an optional fallback for cameras without standard tours. Neither path
  calls or waits for a cloud service.
- **Purpose-built recorder interface.** Administrator entry, navigation,
  dashboard metrics and the public system strip now use a compact operational
  hierarchy instead of generic floating cards, icon tiles and soft pills. The
  visual system uses native UI typography, square state markers, restrained
  borders and semantic colour only where it communicates recorder state.
- **Tapo patrol recovery after PTZ.** Each camera can resume horizontal or
  vertical Tapo patrol after an ONVIF movement ends. The normal stop and
  server-side dead-man stop remain authoritative; patrol recovery runs
  afterwards and a proprietary Tapo failure cannot turn a successful stop into
  an error. Newer firmware can use a separate, write-only encrypted Tapo
  control password, and authentication failures back off for 30 minutes rather
  than repeatedly triggering the camera's temporary lockout.
- **Browser request hardening.** Cookie-authenticated mutations now reject
  cross-site Origin/Fetch Metadata, auth responses are non-cacheable, and the
  CSP restricts scripts, connections, media, workers, forms and embeds to the
  resources Sentinel actually uses. The pre-paint theme bootstrap is a
  same-origin file, so inline scripts are no longer required.
- **Restricted footage operator.** A non-administrator viewer can now use live
  video, timeline playback, segment downloads and permanent protected exports,
  while camera/settings/token administration and destructive clip actions stay
  behind the normal administrator login.
- **Home Assistant split-DNS deployment.** The same HTTPS hostname can be
  embedded locally in Home Assistant and protected by Cloudflare Access on the
  public route. Sentinel emits a configurable `frame-ancestors` CSP and other
  browser hardening headers.
- **Complete camera onboarding UI.** Administrators can run ONVIF discovery,
  resolve media profiles, choose main/substreams and edit stream, credentials,
  recording, retention and motion settings from the browser.
- **Host-network Compose deployment.** WS-Discovery now uses the LXC's real LAN
  interface while go2rtc control and RTSP listeners remain loopback-only.
- **Automatic PWA updates.** Embedded Home Assistant and mobile clients now
  activate a new frontend build automatically instead of waiting for an update
  prompt that the application did not render. The HTML, web manifest and service
  worker are also served with revalidation/no-store headers so an iOS or HA
  Webpage cannot remain pinned to an old API contract.
- **Per-camera recorder fallback.** Recording still starts through go2rtc to
  conserve RTSP clients, but a recorder that cannot initialise an NVR/H.264
  relay automatically retries that camera directly. If the direct connection
  is also refused, the next attempt returns to the relay instead of getting
  stuck on one failing source. A successful per-camera fallback is persisted so
  a known-incompatible NVR channel does not lose another segment on every
  application or host restart. Recorders now wait for go2rtc's listener during
  application start and ignore instant relay exits, avoiding a restart storm
  that would otherwise make every camera open a direct client simultaneously.
- **NVR audio compatibility.** Recorder video remains a zero-copy H.264/H.265
  path, while optional camera audio is normalised to low-bitrate AAC. Direct
  fallbacks carrying G.711/PCM audio now mux into MP4 instead of failing the
  entire camera recorder.
- **Reliable Home Assistant embed.** The viewer cookie uses
  `SameSite=None; Secure` for cross-site HTTPS embeds without granting
  administration rights. A one-time site migration also unregisters the
  obsolete iOS/PWA service worker and evicts its cache while preserving
  Cloudflare and Sentinel cookies.

- **The timeline.** A scrubbable day of footage with motion shaded over it,
  click-to-seek, zoom, keyboard control and next/previous-event jumps. Gaps are
  returned by the API as first-class values carrying *why* they are there — not
  recording, storage unavailable, a damaged segment — because a gap you cannot
  explain is indistinguishable from a bug.
- **Playback that hides the segmentation.** Seeking to a wall-clock instant maps
  to a segment and an offset inside it; crossing a boundary neither stalls nor
  resets the player, and a gap is skipped over and drawn rather than played as a
  freeze.
- **Clip export** as background jobs: FFmpeg concat with `-c copy`, progress
  measured from output duration rather than a timer, range-request downloads,
  and jobs left `running` by a crash reconciled to `failed` so they are visible
  and retryable. Trimming is keyframe-accurate, and both the interface and the
  code say so rather than implying a precision stream copy cannot deliver.
- **Camera discovery.** ONVIF WS-Discovery from every suitable network
  interface, not just the default route — a camera on a second NIC or a VLAN is
  the normal case here. Real stream URLs from `GetProfiles`/`GetStreamUri` with
  WS-UsernameToken digest auth; vendor URL patterns for Tapo, Hikvision, Dahua
  and Reolink offered only once FFprobe confirms they answered; NVR channels
  swept and imported as separate cameras in one action.
- **Discovery is bounded to the local network.** Scanning is restricted to
  private, link-local and loopback space — a feature that takes a host from the
  caller and issues requests against it is an SSRF vector otherwise. Discovery
  responses come from untrusted devices, so parsing is bounded in size and
  depth.
- **A realtime event stream.** An authenticated WebSocket, resumable by sequence
  number, that says "resynchronise" when the gap is too large rather than
  handing a client a plausible but incomplete history. Subscriber queues are
  bounded and drop oldest, so a browser tab on a suspended laptop cannot slow a
  motion worker.
- **mDNS announcement** of `_sentinel._tcp.local.`, so Home Assistant can offer
  zero-configuration setup. It fails soft: a container where multicast does not
  work still starts and serves.
- **A written contract for the Home Assistant API**
  (`app/integrations/CONTRACT.md`), because it is consumed by a separate
  repository and an undocumented shape is what silently breaks.
- **The motion ignore-zone editor.** The backend accepted zones from the start
  and no human could draw one. Now they can, with a mouse, a keyboard or a
  touchscreen, over a live preview, with undo and an explicit save.
- **A Proxmox installer.** One command creates an unprivileged LXC, installs
  Docker, starts Sentinel and prints the URL. It never formats a disk, never
  reuses a container ID, never writes into a path another container has mounted,
  and rolls back exactly what it created if it fails half way.
- **A `sentinel` console command** for the case a local-first product has to
  handle without an email reset: `reset-password` when the only administrator is
  locked out. It reads the database directly, so it works when the service will
  not start.
- **Documentation** covering architecture, configuration, motion tuning,
  recording, security, the API, Proxmox and troubleshooting — each claim checked
  against the code that implements it.

### Fixed

- Home Assistant and iPhone embeds no longer depend on third-party cookie
  support. Viewer API requests use the restricted viewer header and live video
  uses a validated WebSocket subprotocol fallback, keeping the key out of URLs
  and every administrator dependency.
- Recorded H.264 High Profile level 5.0 streams are now declared to the browser
  playback buffer, fixing playback rejection on high-resolution cameras.
- Playback now falls back to the browser's native MP4 player when Safari or a
  Home Assistant WebView detaches its MSE buffer mid-append, and advances to the
  next recorded segment instead of presenting a terminal error.
- The packaged `sentinel` console script pointed at a module that did not exist,
  so it was broken in every install.
- A blocking `Path.exists()` ran on the event loop in the export worker while
  the line below it already offloaded `stat()` to a thread.

- **Motion detection without machine learning.** OpenCV background subtraction
  (MOG2) on the camera's substream at 640×360 and 5 fps — no model, no GPU, no
  accelerator. Analysing a 2K main stream would cost more CPU than recording
  every camera put together, for movement that is just as obvious small.
- **Global-change rejection.** When almost the whole frame changes at once it is
  a light switching on, auto-exposure stepping, or the infrared cut filter
  flipping — not something moving. Those are the largest single source of false
  events on an outdoor camera, and the scene is relearned immediately so the
  minute after a light comes on is not one long false alarm.
- **Events, not frames.** A person crossing the view produces one event, not
  forty: motion must be confirmed over consecutive frames to start, brief gaps
  are tolerated so someone passing behind a car does not split an incident, and
  the event closes only after a grace period. Endless movement, such as rain on
  a lens, is split at a ceiling rather than becoming one event that lasts all
  night.
- **Pre-roll and post-roll**, so an event's clip starts before the movement
  rather than when it was already underway.
- **Snapshots at the peak.** The best frame from the opening seconds becomes the
  event image, pulled in colour from go2rtc, with a thumbnail for the events
  list and the timeline.
- **Ignore zones** as normalised polygons, so a mask drawn on a 2K preview keeps
  working if the analysis resolution or the camera changes.
- **Segments are annotated** with the motion they contain, which is what lets
  the timeline shade the periods with movement.
- **Crash recovery**: events left open by an unclean shutdown are closed at
  start-up instead of being drawn as still running for ever.

- **Retention.** Three independent limits — age, total size and minimum free
  space — each triggering an oldest-first sweep. A per-camera retention period
  overrides the global one.
- **Protection is absolute.** A protected recording is never removed
  automatically, even when a size limit cannot otherwise be met; retention stops
  and says so rather than deleting it.
- **Safe deletion.** Footage newer than two minutes is never touched, so a
  retention period of zero cannot delete the segment still being written. The
  file is removed before its row, so a crash mid-delete leaves a recoverable
  state rather than an orphan file nothing owns. Nothing is deleted at all while
  storage is unavailable, since footage that is merely unreachable would
  otherwise be erased from the index.
- **Storage page** showing what recordings, snapshots, the database and
  everything else occupy — the last of those matters on a shared volume, where
  most used space may have nothing to do with Sentinel.

- **Continuous recording.** FFmpeg records each camera with `-c copy`, so a 2K
  camera costs a few percent of one core instead of most of one, and ten
  cameras fit comfortably on a small server.
- **Segmented, crash-tolerant files.** Recordings are cut into short segments
  aligned to clock boundaries, written as fragmented MP4. A conventional MP4 is
  unplayable without the `moov` atom written at the end, so a power cut would
  lose the whole file; fragmented output means an interrupted segment still
  plays up to the moment it stopped, and a power cut costs seconds rather than
  hours.
- **Indexing decoupled from recording.** FFmpeg writes files and knows nothing
  about the database; a sweep reconciles the directory against it. Crash
  recovery is therefore free — whatever was written before a power cut is picked
  up on the next pass. Every new file is probed, so a corrupt segment becomes a
  recorded fact and a visible gap instead of a playback failure discovered weeks
  later. Files that disappear are marked missing rather than forgotten.
- **Storage protection.** A marker file distinguishes "the storage is mounted"
  from "the mountpoint directory exists but the filesystem is gone". If the
  mount disappears, recording stops instead of quietly filling the system disk,
  the interface says so plainly, and recording resumes by itself when the
  storage returns.
- **Recordings API**: list with time-range, camera, motion and protection
  filters; protect a segment against retention; download with range requests so
  the browser can seek. The on-disk path is never exposed, and every download is
  re-resolved against the storage root.

- **Live view.** Cameras are connected to exactly once: go2rtc holds the RTSP
  connection and fans it out, so a camera never sees the recorder, the motion
  worker, a browser tab and Home Assistant as four simultaneous clients.
- **Managed go2rtc.** Sentinel generates `go2rtc.yaml` from the camera list,
  supervises the process and keeps its stream list in step over go2rtc's own
  API — adding a camera does not restart it, so no other live view or recording
  is interrupted. The config file holds camera passwords by necessity and is
  written `0600` through an atomic replace.
- **Nothing exposed.** go2rtc binds to loopback and its unauthenticated admin
  interface is never reachable. The browser talks to Sentinel, which
  authenticates first and only ever forwards the one stream name belonging to
  the requested camera.
- **Process supervisor** with a 2s → 5s → 10s → 30s → 60s restart ladder that
  plateaus rather than growing, never gives up, and resets after a process stays
  healthy. `SIGTERM` before `SIGKILL`, so FFmpeg can finalise the file it is
  writing.
- **MSE player** speaking go2rtc's WebSocket protocol, with buffer trimming,
  automatic catch-up to the live edge after a backgrounded tab, and a grid that
  switches to substreams so a dozen cameras do not mean a dozen 2K decodes.
- **Snapshots now prefer go2rtc**, which already holds the connection, falling
  back to opening the camera directly.

- **Cameras.** Full CRUD over `/api/v1/cameras`, with the stream URL stored
  separately from its credentials. A URL pasted as
  `rtsp://user:pass@host/stream` has its credentials lifted out and encrypted,
  and the clean URL is what gets stored, displayed and logged. The password is
  never returned by the API, at any endpoint.
- **Stream URL validation.** Only `rtsp://` and `rtsps://` are accepted for a
  stream. `file://`, `http://`, `concat:` and everything else are refused, so
  adding a camera cannot be turned into reading a local file. Passwords are
  percent-encoded when a URL is rebuilt, so a password containing `@` cannot
  redirect the connection to another host.
- **Connection testing.** `POST /cameras/test-stream` probes a URL before it is
  saved and `POST /cameras/{id}/test` re-probes a stored camera, both reporting
  codec, resolution, frame rate, audio and bitrate, and whether the stream can be
  recorded with `-c copy` rather than re-encoded. A successful probe doubles as
  the camera's liveness signal; a broken substream is reported but is not fatal.
- **Snapshots.** `GET /cameras/{id}/snapshot` returns a single JPEG frame,
  preferring the substream so a still image costs as little as possible.
- **Camera management interface** with an add-camera dialog that tests the
  connection before saving and explains what it found.

- **Backend foundation.** FastAPI application with an explicit start-up order,
  async SQLAlchemy 2 over SQLite in WAL mode, and Alembic migrations applied
  automatically at start-up. An existing database is migrated in place and never
  recreated.
- **UTC-only timestamps.** A custom column type stores every datetime as UTC and
  rejects naive values, so aware and naive timestamps cannot mix.
- **Local authentication.** Argon2id password hashing, first-run administrator
  creation, `HttpOnly; SameSite=Lax` session cookies backed by HMAC-signed
  short-lived tokens, and revocable API tokens stored only as hashes and shown
  in plaintext exactly once.
- **Credential encryption.** Camera passwords are encrypted with a key derived
  via HKDF from a 0600 install secret generated on first run.
- **Secret redaction.** RTSP userinfo, `password=`/`token:` pairs,
  `Authorization: Bearer …` values and Sentinel API tokens are stripped at the
  logging root, so no subsystem can leak them to disk.
- **Rate limiting** on sign-in and API-token creation.
- **Health endpoint** at `/api/v1/system/health` with the documented flat keys
  plus per-component detail, returning 503 when a critical component fails.
- **Web interface.** React 19, TypeScript, Vite 6 and Tailwind 4, installable as
  a PWA, with light/dark/system theming tuned for CCTV work, an error boundary,
  skeleton loaders and honest empty states.
- **Packaging.** Multi-stage Dockerfile running unprivileged under tini with a
  real healthcheck, a `compose.yml` that needs no edits, and CI covering ruff,
  mypy, pytest, eslint, tsc, vitest, the production build and an image build
  gated on the container actually reporting healthy.

[Unreleased]: https://github.com/jencarnacao19/sentinel-cctv/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/jencarnacao19/sentinel-cctv/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/jencarnacao19/sentinel-cctv/compare/v0.1.0...v2.0.0
[0.1.0]: https://github.com/jencarnacao19/sentinel-cctv/releases/tag/v0.1.0
