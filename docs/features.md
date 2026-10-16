# Using Sentinel

How the main features work once Sentinel is installed. For installation, see
the [README](../README.md#quick-start).

## ONVIF PTZ controls

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

## Turning one camera's recording on or off

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

## Tapo battery cameras (experimental)

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
[`docs/TC82_LOCAL_PROVIDER.md`](TC82_LOCAL_PROVIDER.md) for the exact
boundaries and validation checklist.

## Privacy mode

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

## Timeline and storage intelligence

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

## Phones and tablets

The web interface is responsive but deliberately does not cache itself for
offline use: stale CCTV state is less safe than an explicit connection error.
On narrow screens it uses a
drawer instead of the desktop sidebar, keeps navigation visible while scrolling,
honours notches and home-indicator safe areas, and provides at least 44 px touch
targets for controls. Forms, camera cards and actions collapse to the available
width without horizontal page scrolling. Live video and the timeline use native
inline playback and touch-aware controls.

## Permanent saved clips

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
