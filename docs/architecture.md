# Architecture

This describes the shape of Sentinel and, more importantly, why it is shaped
that way. Every claim below is checked against the code that implements it; the
file is named after each decision so you can go and read the real thing.

## The shape of it

Sentinel is one process. The API, the compiled web interface, every FFmpeg
recorder, every motion worker and the supervised go2rtc process all live in a
single Python process tree (`backend/app/main.py`). There is no message queue,
no second container, no service mesh. For a box that watches a handful of
cameras in someone's house, "one thing to restart, one log stream" is a
feature, not a shortcut — see the multi-stage `Dockerfile`'s own comment: *"One
container means one thing to restart, one log stream and no inter-service
networking for a home user to debug."*

## The data path

```
CAMERA (RTSP main stream + sub stream)
   │  one TCP connection, opened once
   ▼
GO2RTC                          loopback-only API + RTSP (127.0.0.1:1984 / :8554)
   │
   ├──────────────┬──────────────────────┐
   ▼              ▼                      ▼
RECORDER      MOTION WORKER        SENTINEL WEBSOCKET PROXY
ffmpeg         ffmpeg -> raw        authenticates first, then forwards
-c:v copy      frames -> MOG2       to exactly one stream name
segment muxer  (640x360 @ 5fps)          │
   │              │                      ▼
   ▼              ▼                    BROWSER (MSE / WebRTC)
RECORDINGS DIR   MOTION EVENTS
(fragmented MP4)  + snapshots
   │              │
   └──────┬───────┘
          ▼ indexed by a decoupled sweep, not written by the recorder
   SQLITE (WAL) -- segments, motion events, cameras, settings, users, tokens
          │
          ▼
   SENTINEL REST API  (/api/v1/...)
          │
          ▼
   BROWSER  /  HOME ASSISTANT (bearer API token)
```

Walking it end to end: a camera is opened exactly once, by go2rtc. Everything
downstream — the recorder, the motion worker, the browser, and eventually Home
Assistant — is a consumer of go2rtc's fan-out, not a second connection to the
camera. The recorder writes segments to disk; a separate, decoupled process
reads the directory back and reconciles it into SQLite. The API only ever
reads from SQLite and from the filesystem through validated paths — it never
opens a camera itself.

## Why one RTSP connection per camera, through go2rtc

Consumer IP cameras commonly cap concurrent RTSP clients at two or three.
Between continuous recording, motion detection, one or more browser tabs and a
future Home Assistant stream, that limit is reached almost immediately if each
of them dialled the camera directly. go2rtc sits in front of the camera and
fans one connection out to every consumer, so adding a live-view tab or
enabling motion detection never costs the camera another client
(`backend/app/recording/recorder.py`, `backend/app/motion/frames.py`, both of
which prefer go2rtc's own local RTSP endpoint over the camera when
`record_via_go2rtc` is on). Motion remains on that shared relay. A recorder
whose FFmpeg process cannot initialise the relay alternates to the direct camera
URL on its next supervised attempt; this handles NVR channels that do not repeat
H.264 SPS/PPS data for a late-joining relay consumer. If the camera refuses that
extra client, the following attempt returns to go2rtc. A healthy recorder never
opens both inputs. A direct fallback that recovers recording is stored by camera
identifier in the application settings, so known-incompatible NVR channels do
not lose another segment after every restart. No stream URL or credential is
stored in that preference; a later direct-input failure clears it by returning
the recorder to the relay.

go2rtc itself is configured, not operated, by hand: `backend/app/streaming/config.py`
generates `go2rtc.yaml` from the camera list, and `backend/app/streaming/manager.py`
keeps two paths deliberately separate — a **cold start** that reads that file
(so a reboot with no help from the API still has every camera configured), and
**hot changes** that go through go2rtc's REST API (so adding a camera does not
restart go2rtc and interrupt every other stream already flowing through it).

## Why `-c:v copy`, not video transcoding

`backend/app/recording/recorder.py` writes the camera's own H.264/H.265
bitstream to disk untouched (`-c:v copy`). Decoding, scaling and re-encoding a
2K stream is the difference between a few percent of one CPU core and most of
one — multiplied by every camera on the box. Video stream copy is what makes it
realistic to run ten cameras on a small, GPU-less server, and it is why the
continuous recording path does not require hardware video transcoding. The one condition
this depends on is that the camera actually emits H.264, H.265 or MJPEG;
`backend/app/cameras/probe.py`'s `can_stream_copy` check is what the add-camera
dialog uses to warn the operator before they rely on a codec that would force
re-encoding. Audio is the small exception: FFmpeg encodes an optional track to
64 kbit/s AAC because the G.711/PCM codecs common on NVRs cannot be muxed into
MP4. Video remains untouched and dominates neither CPU nor storage overhead.

## Why MOG2 on a downscaled substream, not a model

Motion detection is intentionally simple: OpenCV's `MOG2` background
subtractor running on a 640×360 greyscale downscale at 5 frames per second
(`backend/app/motion/detector.py`, `backend/app/motion/frames.py`). This is a
product constraint, not a missing feature — Sentinel does not ship any
object-detection or model-based inference anywhere. MOG2 learns what a scene
normally looks like and reports what does not fit, which is all a timeline
needs to mark "something moved here." Two things separate it from a naive
frame difference, per the detector's own docstring: it absorbs lighting
flicker and compression noise into its learned background rather than firing
on every one of them, and it explicitly rejects a change covering most of the
frame at once (a light switching on, an IR-cut filter flipping) as a global
change rather than motion, immediately relearning the new scene afterwards.
Analysing at 640×360/5fps rather than the full recorded resolution costs
roughly a millisecond of analysis per frame — about 1% of one core per
camera, per the comment in `backend/app/motion/worker.py` — because movement
worth noticing is just as obvious downscaled as it is at full resolution.

## Why SQLite

`backend/app/database/session.py` is explicit about this: *"SQLite is the
default because it needs no administration — a home user should never be
asked to install PostgreSQL."* WAL journalling is what lets the recorder index
segments while the API serves the timeline concurrently, which is the actual
concurrency pattern Sentinel has — one writer process, several readers, never
a cluster. Every connection gets `PRAGMA busy_timeout=10000` so a sweep
holding a write lock surfaces as a brief wait rather than "database is
locked," and `PRAGMA foreign_keys=ON` so a deleted camera cascades to its
segments and events instead of leaving orphans. The engine URL is the only
thing that would need to change to move to another database later; nothing
else in the codebase assumes SQLite.

Every timestamp column uses a custom `UTCDateTime` type
(`backend/app/database/base.py`) that raises rather than silently coercing a
naive `datetime` on the way in. SQLite has no native timezone type, so without
this guard a stray naive timestamp would round-trip into the wrong hour
depending on the host's local time — which is exactly the class of bug that
produces a timeline drawn an hour off.

## Why indexing is decoupled from recording

FFmpeg (the recorder) knows nothing about the database — it writes files and
nothing else. A separate sweep (`backend/app/recording/indexer.py`), run every
`index_interval_seconds` (30s by default) from `RecordingManager._index_loop`,
walks the recordings directory and reconciles it against what SQLite already
knows. This is what makes crash recovery free: whatever FFmpeg managed to
write before a power cut is picked up on the very next sweep, and a database
restored from an old backup re-discovers every segment still sitting on disk.
The indexer probes every newly seen file with FFprobe, so a segment cut short
by a power cut is recorded as `damaged` — a known, visible gap — rather than a
silent playback failure discovered weeks later. Rows whose file has vanished
(deleted by hand, or a storage mount that disappeared) are marked `missing`,
not dropped, so the timeline can still say honestly that something used to be
there.

## Start-up and shutdown order

`backend/app/main.py`'s `lifespan` function fixes the order deliberately:
settings and directories first, then the install secret, then Alembic
migrations against the existing database (never a fresh one), then the async
engine. go2rtc starts next because the recorders read from it; recorders start
after go2rtc because they need something to read from; motion workers start
last because they read the same go2rtc substreams the recorders are already
consuming and only annotate footage that is already being written. Shutdown
reverses this: motion stops first, then recording (so FFmpeg gets a `SIGTERM`
and can finalise the fragment it is writing before its go2rtc input goes
away), then go2rtc, then the database engine.

## Process supervision

Every external process Sentinel owns — go2rtc, each recorder, each motion
reader — is wrapped in a `SupervisedProcess`
(`backend/app/supervisor/process.py`). Restart delays step 2s → 5s → 10s → 30s
→ 60s and then plateau, and there is no maximum retry count: a camera
unplugged for a week must still reconnect by itself when it comes back, and a
transient go2rtc crash must never require a human to notice and restart it. A
process that stays up past `HEALTHY_UPTIME_SECONDS` (30s) resets the ladder, so
one bad night does not leave a camera stuck on a 60-second retry forever.
`SIGTERM` is always sent before `SIGKILL`, specifically so FFmpeg's fragmented
MP4 writer gets a chance to close out the fragment it is on rather than
leaving an unplayable tail.

## Where configuration lives

Two layers, on purpose (see `docs/configuration.md` for the full list):
process-level settings that must be known before the database exists — data
directory, port, the go2rtc ports, which FFmpeg binary to use — live in
environment variables read once at start-up (`backend/app/config.py`).
Everything a user is meant to change after installing — storage path,
retention, per-camera recording mode and sensitivity, ignore zones — lives in
the database, either as typed columns on `Camera` or as JSON-encoded rows in
the generic `app_settings` key/value table
(`backend/app/system/settings_store.py`). Changing the second kind never needs
a restart or a redeploy; changing the first kind does.

## Where the browser fits

The compiled React SPA is served from the same FastAPI process, from the same
origin as the API (`backend/app/main.py`'s `_mount_frontend`), which is why
CORS is closed by default — there is no cross-origin call to make in a normal
install. The browser never talks to go2rtc directly: go2rtc's own admin
interface has no authentication of its own and can be told to open arbitrary
sources, so every live-view request is proxied through an authenticated
Sentinel endpoint that only ever forwards to the one stream name that belongs
to the camera being requested (`backend/app/streaming/router.py`). A client
that has not authenticated at the WebSocket handshake never receives a single
frame.
