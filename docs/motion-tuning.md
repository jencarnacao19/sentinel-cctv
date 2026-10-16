# Motion detection: a practical guide

Sentinel's motion detector is OpenCV's `MOG2` background subtractor running on
a 640×360 greyscale downscale at 5 frames per second — no model, no GPU (see
`docs/architecture.md` for why). This document is about the numbers: what the
sensitivity slider actually changes, what the timing parameters mean in
practice, and how to stop rain, foliage and headlights from filling the
events list. Every figure below is read from
`backend/app/motion/detector.py` and `backend/app/motion/aggregator.py`, not
estimated.

## What "sensitivity" actually changes

Each camera has one number, 0–100 (`Camera.motion_sensitivity`, default 50).
`MotionSettings.from_sensitivity` maps it onto two real thresholds:

- **MOG2 variance threshold** — how far a pixel has to move from the learned
  background to count as foreground. Linear from 28 at sensitivity 0, through
  16 at the default 50, down to 4 at sensitivity 100. Older revisions changed
  only a later threshold over an already-binary mask, so that half of the
  sensitivity control had little practical effect.
- **Minimum area** — how large a blob of changed pixels has to be, as a
  fraction of the analysed frame, before it counts as motion rather than
  noise. Linear from 0.5% of the frame at sensitivity 0 down to 0.02% at
  sensitivity 100.

At the 640×360 analysis size (230,400 pixels), that minimum area works out to
roughly:

| Sensitivity | Minimum blob size | Rough equivalent |
| --- | --- | --- |
| 0 | ~1,150 px | A large, close subject only |
| 50 (default) | ~600 px | A person at moderate distance |
| 100 | ~46 px | Anything the size of a hand, or a raindrop on the lens |

**Raising sensitivity does two things at once**: it lowers the MOG2 variance
threshold (so fainter changes register) *and* shrinks the minimum blob size
(so smaller ones count). That combination is exactly why a camera turned all
the way up starts reporting insects, raindrops and sensor noise — both knobs
that would normally filter them out have been loosened together. If a camera
is missing genuine distant movement, prefer moving sensitivity up in small
steps and re-checking, over jumping to 100.

There is currently no slider for this in the interface — motion sensitivity
is a per-camera field, changed only through the API:

```bash
curl -X PATCH https://sentinel.example.lan/api/v1/cameras/<camera-id> \
  -H "Authorization: Bearer sctv_..." \
  -H "Content-Type: application/json" \
  -d '{"motion_sensitivity": 65}'
```

Changing it takes effect on the worker's next restart, which
`PATCH /cameras/{id}` triggers automatically — you do not need to restart
Sentinel itself. (`backend/app/motion/manager.py`'s `restart_camera` rebuilds
the worker because it holds compiled detector thresholds and cannot pick up
new ones without a fresh instance.)

## Zones: excluding part of the frame entirely

Ignore zones are normalised polygons (0.0–1.0 in both axes, so they survive a
change of analysis resolution or camera). Up to 32 zones, 3–128 points each
(`backend/app/motion/router.py`'s `MotionZonesUpdate`). Anything inside a zone
is masked out of the frame *before* thresholding — pixels there simply never
contribute to a detection, at any sensitivity
(`backend/app/motion/detector.py`'s `build_mask`). This is the single most
effective tool for a scene with one specific offender: a road at the edge of
frame, a tree that sways, a flag, a television visible through a window.

The **Motion zones** page provides the editing surface: choose a camera, draw
polygons over its preview frame, enable or disable them, undo/redo changes and
save the complete list. The same operation is available through the API with
`GET`/`PUT /api/v1/cameras/{id}/motion-zones`:

```bash
curl -X PUT https://sentinel.example.lan/api/v1/cameras/<camera-id>/motion-zones \
  -H "Authorization: Bearer sctv_..." \
  -H "Content-Type: application/json" \
  -d '{"zones": [[[0.0, 0.7], [1.0, 0.7], [1.0, 1.0], [0.0, 1.0]]]}'
```

That example ignores the bottom 30% of the frame — a common shape for a
camera that watches a driveway but also sees a public road at the very
bottom edge.

## What the timing parameters actually do

An event is not one frame — it is a whole incident, aggregated by a small
state machine (`backend/app/motion/aggregator.py`) so that a person walking
across a driveway for ten seconds produces one row, not forty.

- **Confirmation (fixed at 3 spatially-related frames).** A single motion frame
  never starts an event. Three detections must plausibly follow one another in
  the image; unrelated blobs on opposite sides no longer combine into one
  incident. At the default 5 fps, first-to-third confirmation takes about 0.4
  seconds. Not currently configurable.
- **End grace (default 8 seconds, `motion.end_grace_seconds`).** Motion has
  to be absent for this long before an event is considered over. Someone
  walking behind a parked car for a second and re-emerging is one event, not
  two, because the gap is shorter than this window.
- **Pre-roll (default 5 seconds, `motion.pre_roll_seconds`)** and **post-roll
  (default 10 seconds, `motion.post_roll_seconds`)** extend the event's
  *playback* window before the first confirmed frame and after the last one.
  The interesting part of most events is the approach, which is why pre-roll
  exists at all.
- **Event ceiling (fixed at 15 minutes, `max_event_seconds`).** Continuous
  movement — rain that never stops, a branch that will not stop swaying —
  is cut into a fresh event every 15 minutes rather than becoming one event
  that appears to run all night. Not currently configurable.

The three DB-backed values above are read by the aggregator on every worker
(re)start but have no control in the interface or the API yet — changing them
means editing the `app_settings` table directly. In practice the defaults are
reasonable starting points; per-camera sensitivity (currently API-only) and
the Motion zones editor are the levers meant to be used day to day.

## Warm-up: why nothing is detected right after a (re)start

MOG2 needs to see the scene before it knows what "normal" looks like. For the
first 15 frames after the detector starts or resets — 3 seconds at the
default 5 fps — every result is suppressed (`WARMUP_FRAMES` in
`backend/app/motion/detector.py`). This happens after every Sentinel restart
and after every camera reconnect, because a reconnect may show a genuinely
different scene (the camera could have been repositioned, or hours could
have passed) and carrying the old learned background over would produce a
burst of false motion the moment the stream resumes
(`backend/app/motion/worker.py`). Seeing no events for the first few seconds
after a restart or a network blip is expected, not a bug.

## Stopping lights, exposure steps and IR-cut flips

These are rejected structurally, not by tuning: when 55% or more of the
analysed frame changes in one step (`GLOBAL_CHANGE_FRACTION`), the detector
treats it as a lighting change — a light switching on, auto-exposure
stepping, an infrared cut filter flipping at dusk — rather than motion, and
immediately re-learns the background at a much higher learning rate so the
following frames are judged against the new scene instead of producing a
minute of false motion. This applies at every sensitivity level and needs no
configuration.

## Rain, foliage and headlights: the hard cases

These are the false-positive sources that survive both of the above, because
they genuinely are movement, just movement nobody wants an event for.
`PROJECT_STATUS.md` is honest that their behaviour on real scenes has been
*reasoned about, not measured* — there is no camera-in-the-loop benchmark
behind the advice below, only the mechanics of the detector:

- **Rain.** Individual drops are usually well under the minimum blob size and
  filtered out on their own. Heavy rain lit at an angle (headlight, porch
  light, streetlight) can cover enough of the frame with visible streaks to
  clear the minimum-area threshold as one connected blob — the morphological
  *opening* step in the detector removes small isolated speckle for exactly
  this reason, but a large sheet of falling water is not speckle. Lowering
  sensitivity a little, or zoning out the area nearest a light source where
  droplets are most visible, is usually enough.
- **Foliage.** A tree or bush that sways constantly is the case zones exist
  for. Sensitivity alone cannot fix a subject that is *always* moving,
  because the whole point of MOG2 is to learn what is normal and this
  particular motion never becomes normal enough to absorb — the mask has to
  exclude it outright.
- **Headlights sweeping across a scene at night.** These are large,
  fast-moving, high-contrast changes that clear both the pixel and area
  thresholds easily and are not global enough (rarely 55% of the frame) to
  be caught by the lighting-change rejection. A zone over the stretch of
  road or driveway where headlights sweep is the reliable fix; lowering
  sensitivity alone tends to also start missing genuine pedestrians in the
  same shot.
- **A camera with no substream.** Analysing a downscale of the *main* stream
  costs more CPU and, more importantly, tends to be noisier — compression
  artefacts in a full-resolution H.264/H.265 stream survive the downscale
  differently than a substream encoded at low resolution to begin with.
  Configuring a substream on the camera, if it has one, is worth doing before
  reaching for sensitivity.

If a single camera is simply not worth the tuning effort — a busy street
scene, for instance — motion detection can be turned off for that camera
alone (`motion_enabled: false`) while it keeps recording continuously; the
two settings are independent (`backend/app/motion/manager.py`'s
`_should_detect` checks both `camera.motion_enabled` and `camera.enabled`,
independently of whether the camera is being recorded).
