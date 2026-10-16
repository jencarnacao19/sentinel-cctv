# Camera roadmap

Planned work that builds on the Camera Reliability Center. **Nothing described
here is implemented.** This document exists so the next phases are designed
against the same constraints as the current system rather than discovered
halfway through.

## The rule every phase inherits

**No future integration may sit in the critical recording path.**

Sentinel's job is that footage exists. go2rtc relays, FFmpeg records, the
supervisors restart what dies, and the segment index describes what was
written. Nothing added below may be able to stop, delay or corrupt that chain.
Concretely, for every phase:

- if the new component is stopped, crashed, misconfigured or removed, recorders
  keep writing and playback keeps working;
- it consumes what already exists — the relay, the segment index, finished
  files — and never inserts itself between a camera and its recorder;
- it never becomes an additional consumer of a camera's RTSP stream, because
  consumer cameras cap concurrent clients at two or three;
- it fails closed and quietly: a broken integration is a degraded feature, not
  an outage.

The same local-first boundary applies throughout. Recording must continue with
no internet, no cloud service, no Cloudflare, no Home Assistant and no
notification integration reachable.

---

## Phase 1 — Optional local Frigate object detection

Classify **person, vehicle, animal and package** events locally, alongside the
existing CPU motion detection rather than in place of it.

**Required first: a Proxmox CPU/GPU/Coral resource audit.** This phase must not
start until that audit exists and is recorded. It has to answer:

- how much CPU headroom remains on the target host while all configured
  channels record and their motion workers run;
- whether a Coral TPU can be passed into an *unprivileged* LXC, and what device
  mapping and udev rules that needs;
- whether any GPU is available for passthrough, and what that costs the host;
- what happens to recording latency and segment freshness when detection is
  saturated;
- whether detection should run in the same container at all, or in a separate
  one with its own resource limits.

Without that audit the honest default is that detection is unaffordable, and
shipping it anyway would trade guaranteed recording for optional labels.

Design constraints:

- Frigate consumes the **go2rtc substream**, exactly as motion workers already
  do. It never opens its own connection to a camera.
- Detection is **per camera, opt-in, and off by default**.
- Detection results are metadata attached to footage Sentinel already recorded.
  Frigate never becomes the recorder, and its own recording features stay off.
- Under load, detection is what degrades. If the host cannot keep up, frames are
  dropped and the fact is reported through the Reliability Center — recording is
  never throttled to make room.
- Existing independent recording or backup systems must remain unaffected.

Open questions: model choice and its licence; whether zones are shared with the
existing motion zones or configured separately; how detection events relate to
the current `motion_events` rows.

---

## Phase 2 — Local-first alerting through a persistent outbox

Deliver notifications about camera and detection events without ever depending
on a remote service being reachable.

Shape:

- A durable, bounded **outbox table**. An event is written locally as part of
  the transaction that produced it, and delivery is a separate concern.
- A delivery worker drains the outbox with retry and backoff, in the style of
  the existing process supervisor: it does not give up, and it cannot block the
  producer.
- **Home Assistant and webhooks are optional consumers.** Both are unreachable
  by design in the local-first failure case, and neither may hold up recording
  or the API.
- Retention and size limits on the outbox, same reasoning as camera incidents:
  a week of failed deliveries must not grow the database without bound.
- Payloads are sanitised on the way in, not on the way out — no hosts, RTSP
  URLs, filesystem paths, credentials or raw process output, matching the rules
  already applied to incidents.

Open questions: whether the outbox is per-consumer or shared with per-consumer
cursors; deduplication for a camera that flaps; whether alerting reuses the
incident debounce or needs its own.

---

## Phase 3 — Evidence integrity

Make it demonstrable that an exported clip is the footage that was recorded.

Shape:

- A **manifest** per export listing the source segments, their time bounds and
  their sizes.
- **Hashes** computed over segment content, recorded when the segment is
  indexed rather than when it is exported, so the hash predates any request to
  produce evidence.
- An **audit history** of the actions that matter — export created, clip
  protected, clip unprotected, clip deleted — with who did it and when.
- Verification is offline: given the recordings volume and the database,
  integrity can be checked with no network at all.

Constraints: hashing happens in the indexer's existing pass, off the event loop,
and a hashing failure marks a segment rather than stopping the recorder. The
audit history is append-only and bounded, and is subject to the same redaction
rules as everything else.

Open questions: hash algorithm and cost per segment at current bitrates;
whether manifests are signed with the install secret, and what that implies for
backup and restore.

---

## Phase 4 — Optional encrypted off-site backup for protected clips

Copy **explicitly protected clips only** to an off-site destination.

Shape:

- Strictly opt-in, and strictly limited to clips an administrator has protected.
  Continuous recordings are never uploaded.
- **Encrypted locally before leaving the machine.** The destination is treated
  as untrusted storage, and holds no key.
- The uploader is a consumer of finished export files. It cannot touch the
  recording volume in a way that affects recorders, and it is bandwidth-limited
  so it cannot starve the LAN or the relay.
- Failure is visible and inert: a destination that is unreachable produces a
  reported backlog, never a stalled export queue or a paused recorder.
- Privacy mode's meaning is preserved. Enabling privacy stops camera use; it
  does not retroactively delete or upload anything.

Open questions: key custody and recovery — an encrypted backup nobody can
decrypt is not a backup; which destinations are worth supporting; whether
deletion of a protected clip should propagate off-site, and who is allowed to
trigger that.

---

## Sequencing

Phases 2 and 3 are independent of the Frigate audit and can proceed first.
Phase 4 should follow Phase 3, so that what is backed up off-site is already
something whose integrity can be demonstrated.
