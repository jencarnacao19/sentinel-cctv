# TC82 local provider

The Tapo TC82 is a battery/event camera, not an RTSP camera. Sentinel therefore
integrates it through a separate provider whose failure cannot affect existing
RTSP cameras, go2rtc, recorders or motion workers.

## What is local

- Control and media target only the configured private LAN address, normally
  the camera or its H200 hub, on the vendor's local control/media ports.
- Battery and firmware state are read only when the interface requests them.
- **Live view** opens one Direct Stream on demand and closes it when the dialog
  closes or after the configured time limit (five minutes by default).
- **Sync last 24 hours** asks the camera/hub for completed event clips, downloads
  at most the configured limit and stores them on Sentinel's recording volume.
- Imported files use the normal timeline, playback, range-download and retention
  implementation. The provider's stable external clip key makes repeated syncs
  idempotent.

A standalone TC82 does not expose an operator-created local username. Its
Direct Stream uses fixed username `admin`, and Sentinel never presents an
account email as a workaround. An H200 is a different topology: Sentinel uses
the hub control path and may accept an optional local username for firmware
that requires it. Passwords are encrypted at rest. Sentinel does not call a
TP-Link cloud endpoint at runtime, ask for an API token, or open/forward a
router port.

## What deliberately does not happen

- no invented RTSP or ONVIF address;
- no continuous FFmpeg recorder;
- no Sentinel motion worker or motion-zone editor;
- no go2rtc registration or extra permanent camera client;
- no reliability alarm for the absence of continuous segments;
- no automatic live stream on either camera wall;
- no cloud fallback when the local provider fails.

Privacy mode is authoritative. It refuses state, clip, sync and live operations,
and an active Direct Stream checks the privacy state while yielding media.
Historical clips remain playable, matching the existing privacy-mode contract.

## Enable and configure

1. Enable **Third-Party Compatibility** for the camera/account in the Tapo app.
2. Set `SENTINEL_ENABLE_TAPO_BATTERY_PROVIDER=true` in the deployment `.env` and
   restart Sentinel.
3. Open **Cameras → Add manually → Tapo battery**.
4. Choose **Standalone TC82** and enter the camera's private address, or choose
   **H200 hub**, enter the hub address and select/enter the exact child ID.
5. Enter the local third-party-service password, then run **Test connection**.
   Standalone uses fixed username `admin`; the optional username field exists
   only for H200. Do not save until the exact topology and credentials pass.
6. Save, open **Battery camera**, test a short live view, stop it, then import a
   small 24-hour clip set and confirm playback in both admin and Home Assistant.

## Timed manual recording

A TC82 is not a continuous recorder and Sentinel does not turn it into one. It
sleeps between events, and that sleep is the entire reason its battery lasts
months instead of hours. What the **Timed recording** panel adds is a deliberate,
time-boxed exception.

Choosing a duration shows the current battery percentage and a warning that the
camera stays awake for the whole period and may end up completely flat, after
which it stops recording and stays offline until charged. Starting requires
explicitly accepting that cost; the API refuses a request without
`accept_battery_drain: true`.

Durations are 5, 10, 15, 30 or 60 minutes, or a custom value up to a ceiling an
administrator sets with
`PUT /api/v1/cameras/tapo-battery/timed-recording/max-minutes`. Sentinel's own
hard limit is 120 minutes and cannot be raised: two hours of continuous radio
and encoder use is already far beyond what this hardware is built for.

While it runs, the panel shows the countdown, bytes captured, connection state
and reconnect count, with a **Stop and save** button. Progress is read from
memory and never contacts the camera, so polling it costs no battery.

**How the media is kept.** Packets are appended to a `.part.ts` file as they
arrive, then prepared as MP4. Video is stream-copied; the Tapo G.711 audio track
is converted locally to AAC so recordings have sound in browsers and Home
Assistant. A partial capture still contains exactly the video frames the camera
sent. The clip is indexed as an `origin=tapo_manual` segment, which the ordinary timeline,
playback manifest and Home Assistant playback all serve like any other footage.
The part file is deleted only after the segment row is committed, so a crash in
between leaves it on disk and the next start-up finalises and indexes it.

**Every way it can end early keeps the footage.**

| What happened | Result | Reported as |
| --- | --- | --- |
| The full duration elapsed | complete clip | `completed` |
| An administrator pressed Stop | partial clip | `partial`, *Stopped by an administrator* |
| Privacy mode was switched on | partial clip | `partial`, *Stopped because privacy mode was switched on* |
| Sentinel shut down | partial clip | `partial`, *Stopped because Sentinel shut down* |
| The camera stopped answering | partial clip | `partial`, *Camera powered off before the requested duration* |
| Nothing playable arrived | no clip | `failed` |

The requested duration is never reported as captured. A session that ran eleven
minutes of a requested sixty says eleven.

If the firmware closes the Direct Stream before the timer ends, Sentinel reopens
it over the LAN with backoff (2 s, 4 s, 8 s, 15 s, 30 s), giving up after six
consecutive failures or when the timer expires — whichever comes first. The
TP-Link cloud is never contacted, on this path or any other. Only one live view
*or* timed recording may be open per camera, and nothing starts a session
automatically: there is no schedule, no motion trigger and no retry tomorrow.

## Real-device acceptance checklist

This code has automated boundary, migration, API, idempotency and frontend
coverage, but firmware-specific behaviour must be proved with the physical
TC82/H200 before calling it production-ready:

- authentication succeeds with Internet access to the camera network blocked;
- the returned model, firmware and battery percentage are plausible;
- opening live view wakes the camera and closing the dialog releases it;
- a second live request is refused instead of opening another battery session;
- event listing returns the expected UTC time range;
- the same sync twice creates one local segment, not duplicates;
- the imported MP4 seeks and plays in admin and Home Assistant playback;
- privacy mode blocks a new live/sync request and terminates an active stream;
- all RTSP recorder and motion-worker counts remain unchanged throughout;
- rebooting Sentinel with the provider enabled but the TC82 unreachable leaves
  every RTSP camera recording normally.

Timed recording adds three more, and these are the ones that cannot be proved
without operating the camera. Do not run them during a smoke test, and do not
start one on a camera below roughly 40% charge:

- a **5-minute** timed recording completes and its clip plays in the Timeline and
  in Home Assistant playback;
- pressing **Stop and save** part-way through produces a shorter clip that also
  plays, and the reported duration matches what was captured rather than what was
  requested;
- if the firmware closes the stream mid-session, the reconnect counter rises and
  the session continues rather than ending.

If any item fails, disable the feature flag. Existing RTSP cameras and already
imported local clips remain unaffected.
