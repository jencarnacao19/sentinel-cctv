# Recording

## What a segment is

A recording is not one file per camera — it is a stream of short segments,
each an independent fragmented MP4 file. FFmpeg copies H.264/H.265 video with
`-c:v copy`; optional camera audio is encoded to 64 kbit/s AAC so the G.711/PCM
tracks common on NVRs remain compatible with MP4
(`backend/app/recording/recorder.py`). Two things follow directly from that:

- **A power cut costs at most one segment**, not the whole recording. Because
  the output is fragmented (an index written per fragment rather than one
  `moov` atom at the end), a segment cut off mid-write is usually still
  playable up to the point it stopped.
- **Retention can delete the oldest footage without rewriting anything** —
  there is nothing to splice, only whole files to remove.

Segments are cut on clock boundaries (`-segment_atclocktime 1`), not on
elapsed time since the recorder started, so a segment's filename and its
content agree and the timeline has no drifting offset to account for. They
are laid out on disk as:

```
<storage root>/<camera-id>/2026/08/15/14/segment-14-00-00.mp4
```

All of it in UTC — the recorder subprocess is always started with `TZ=UTC` in
its own environment, regardless of the container's timezone, specifically so
FFmpeg's `strftime` expansion in the output path cannot drift across a
daylight-saving change and produce an hour of overwritten filenames twice a
year (`backend/app/recording/paths.py`). The directory layout exists so a
human can find footage by browsing the disk directly, but **the database is
the actual index**; nothing except the indexer reads meaning out of a
filename, and even the indexer cross-checks what it parses against the file
itself.

Segment length defaults to 60 seconds and, in this build, cannot actually be
changed — see `docs/configuration.md` for why the environment variable and
the database setting that appear to control it do not currently connect to
anything.

## What `recording_mode` actually does

Only `continuous` starts a recorder at all (`CONTINUOUS_MODES` in
`backend/app/recording/manager.py`). Selecting `motion` or `scheduled` is
honest about doing nothing rather than silently behaving like `continuous` —
no segments are written for a camera in either of those modes in this build.
One nuance worth knowing: the motion detector's own decision to run is a
separate check (`MotionManager._should_detect` in
`backend/app/motion/manager.py`) that only excludes `recording_mode: off`,
not `motion` or `scheduled`. In practice this means a camera set to `motion`
mode will still have its motion detector running and producing events with a
snapshot image — captured directly from go2rtc at the moment of the event —
even though there is no recorder writing segments underneath it, so those
events have no video to open. If you want motion detection without
continuous recording, this is not yet a supported combination; use `off` for
"detect nothing" and `continuous` for everything else until `motion`
recording mode is actually implemented.

## Indexing, and what a gap in the timeline means

The recorder and the database index are deliberately decoupled
(`backend/app/recording/indexer.py`): FFmpeg writes files and knows nothing
about SQLite, and a separate sweep — every 30 seconds by default
(`index_interval_seconds`) — walks the directory and reconciles it. This is
what makes crash recovery free: whatever FFmpeg managed to write before a
power cut is picked up on the very next sweep, with no special recovery
procedure and no need to restart anything by hand.

Every newly seen file is probed with FFprobe before it is indexed, which is
what turns a bad file into a recorded fact rather than a silent playback
failure discovered weeks later. A segment lands in one of three states:

- **complete** — probed successfully, playable.
- **damaged** — either far too small to be real footage (under 1 KiB, almost
  always a failed start) or a file FFprobe could not read, typically a
  segment cut short mid-write by a power cut. Recorded as a known, visible
  gap rather than skipped.
- **missing** — the database still has the row, but the file is no longer on
  disk: someone deleted it by hand, or the storage mount disappeared and came
  back with different contents. Marked, not dropped, so the timeline can
  still say honestly that something used to be there.

The newest file for a camera is given a 5-second grace period before it is
indexed at all (`IN_PROGRESS_GRACE_SECONDS`), because it may still be open
under FFmpeg — the next sweep, 30 seconds later, catches it once it is
genuinely finished.

**A gap in the timeline is therefore always one of:** the camera was
unreachable for that period (nothing was ever written); a segment exists but
is `damaged`; or footage that did exist was removed by retention. There is no
fourth kind of gap — an indexed, complete segment is never silently absent
from what the API reports.

## Protection

Protection is absolute: the retention sweep excludes any segment with
`protected: true` unconditionally, and if a size or free-space limit could
only be satisfied by deleting a protected segment, the sweep stops and
reports why rather than removing it (`backend/app/storage/retention.py`).

**Protection is a property of the recording segment, not of the motion event
that happens to overlap it.** `RecordingSegment.protected` and
`MotionEvent.protected` are two independent columns, set by two independent
endpoints (`PATCH /api/v1/recordings/{id}` and `PATCH /api/v1/events/{id}`
respectively — `backend/app/recording/router.py`,
`backend/app/motion/router.py`), and nothing in the codebase makes one follow
the other. Concretely: protecting a motion event in the **Events** page marks
that event's own row as protected, which stops the event from being filtered
out by a `protected=true` query — but the recording segments underneath it
are untouched and remain eligible for deletion by retention on their normal
schedule. To actually keep the footage a given event points to, protect the
overlapping recording segments directly (`PATCH /api/v1/recordings/{id}` with
`{"protected": true}`); the interface does not do this for you yet. Treat
"protect this event" in the current interface as bookmarking the event
record, not as a guarantee the video will still exist next month.

## When the disk fills up, or the mount disappears

Three independent limits can trigger a retention sweep, checked every ten
minutes (`RETENTION_TICK_SECONDS`): age (global, with a per-camera override on
`Camera.retention_days`), total size (`storage.max_bytes`), and minimum free
space (`storage.min_free_bytes`, 5 GiB by default). Any of them deletes
oldest-first, skips protected segments entirely, and never touches anything
recorded in the last two minutes (`MINIMUM_AGE_SECONDS`) — a misconfigured
retention of a single day still cannot delete footage that is still being
written. A single sweep is capped at 5,000 deletions
(`MAX_DELETIONS_PER_SWEEP`), so a badly wrong setting cannot erase an entire
archive in one pass with no chance to notice.

Deletion order matters: the file on disk is removed *before* its database
row (`backend/app/storage/retention.py`). If the process is killed in
between, the row survives pointing at a file that is now gone, the next
indexing sweep marks it `missing`, and the next retention sweep simply skips
a row with no bytes behind it. The reverse order — deleting the row first —
would leave an orphaned file on disk that nothing owns and retention would
never find again to reclaim the space.

Separately from retention, a storage check runs every 30 seconds
(`STORAGE_TICK_SECONDS`, `backend/app/recording/manager.py`) watching for the
recordings mount disappearing underneath Sentinel. A marker file
(`.sentinel-storage`) is written into the storage root the first time it is
accepted; if the directory still exists and is still writable but the marker
is gone, that means a mountpoint was unmounted and Sentinel is now looking at
the empty directory underneath it — writing there would silently fill the
**system** disk instead. When that happens every recorder is stopped and the
dashboard shows the reason in plain language; recording resumes by itself,
with no restart needed, the next time the check finds the storage available
again.

## Getting footage out

The Recordings API supports filtering by camera, time range, protection and
whether a segment contains motion (`GET /api/v1/recordings`), and downloading
a specific segment with HTTP range-request support so a browser can seek
inside it (`GET /api/v1/recordings/{id}/download`).

The web interface has a **Timeline** page with continuous playback, scrubbing,
motion bands, visible gaps and clip saving. A motion event's legacy
`playback_url` still points at `/event/{id}`, which is not a dedicated frontend
route; use the Timeline page for interactive playback. The Recordings API is
still useful for scripts or for downloading one exact segment:

```bash
curl -H "Authorization: Bearer sctv_..." \
  "https://sentinel.example.lan/api/v1/recordings?camera_id=<id>&start=2026-08-15T10:00:00Z&end=2026-08-15T11:00:00Z" \
  | jq '.segments[].id'

curl -H "Authorization: Bearer sctv_..." \
  -o clip.mp4 \
  "https://sentinel.example.lan/api/v1/recordings/<segment-id>/download"
```

The **Events** page lists motion incidents with thumbnails and the **Storage**
page exposes disk usage and retention settings. **Timeline** can save the
previous 30 seconds, 1 minute or 5 minutes, or any dragged range. Each request
is processed in the background as a stream-copied MP4 and appears on **Saved
clips** with its progress and download action.

A newly saved clip has `protected=true` and no expiry. It lives under the
recording root's `.exports` directory and is deliberately outside the automatic
recording-retention and temporary-export cleanup queries. Its original segments
may therefore be reclaimed while the stand-alone MP4 stays downloadable. An
administrator may mark it temporary, giving it a fresh 24-hour expiry, or
delete it explicitly. Storage loss and deletion of the complete recording
volume are infrastructure operations and are not prevented by this protection.
