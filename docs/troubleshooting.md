# Troubleshooting

Start here: **Settings → Download diagnostics**. That file contains everything
below in one place, with credentials already removed, so it is safe to attach to
an issue.

---

## Nothing is recording

Check `System → Health` first. `recording` reports one of:

| State | Meaning |
| --- | --- |
| `skipped` | No camera is set to record. Check each camera's recording mode. |
| `failed` | Recorders should be running and none are. Read the detail line. |
| `degraded` | Some recorders are down — usually one unreachable camera. |

For a relayed H.264/NVR channel that exits before FFmpeg can determine its
dimensions (often preceded by `non-existing PPS`), Sentinel automatically tries
that recorder's direct camera URL on the next supervised start. If the camera
refuses the extra RTSP client, the following attempt returns to go2rtc. The
choice is per camera and never moves healthy recorders or motion workers off the
shared relay. A successful direct fallback is remembered across container and
host restarts, so that known channel does not repeat the same gap at every boot.

Then work through these in order.

**Is FFmpeg installed?** The diagnostics bundle has a `tooling` section. If
`ffmpeg` is `null`, nothing can record. Inside the official container it is
always present; on a manual install, `apt install ffmpeg`.

**Is the camera reachable?** Open the camera and press **Test**. A failure
message here is the camera's own answer, translated:

- *"The camera rejected the username or password"* — the credentials are wrong.
  On Tapo cameras this is almost always because the TP-Link **cloud** login was
  used instead of a **camera account** created in the Tapo app under
  *Advanced Settings → Camera Account*.
- *"That stream path does not exist"* — the address is right but the path is
  not. Tapo uses `/stream1` (main) and `/stream2` (sub); most other brands
  publish theirs in the manual or over ONVIF.
- *"The camera did not respond in time"* — a firewall, a VLAN, or a camera that
  accepts the connection and then stalls.

**Tapo patrol does not resume after PTZ.** The live `Stop` is ONVIF, but patrol
is a separate proprietary Tapo command. Confirm the camera has **Resume Tapo
patrol after PTZ** configured and enable **Third-Party Compatibility** in the
Tapo app (under *Tapo Lab* or *Settings*, depending on the app version). The
container log records an authentication failure without delaying or undoing the
safety-critical ONVIF stop, then waits 30 minutes before another proprietary
login attempt. Some newer firmware requires `admin` plus the TP-Link cloud
password instead of the local Camera Account password. Save that value in the
camera's separate **Tapo control password** field; it is write-only and encrypted
locally, and does not replace the RTSP/ONVIF password.

**Is the storage available?** If the dashboard shows *"Storage unavailable —
recording is suspended"*, Sentinel has deliberately stopped rather than fill the
system disk. See [Storage disappeared](#storage-disappeared) below.

---

## The live view does not play

Live view goes through go2rtc. The API and health page both report it.

- **`go2rtc` is `failed` in health** — the process is not answering. Check the
  container logs for lines in the `GO2RTC` category. On a manual install,
  confirm the binary exists (`go2rtc --version`).
- **"This browser cannot decode …"** — the camera is H.265 and the browser
  cannot play it in Media Source Extensions. Safari can; most others cannot.
  Configure the camera for H.264 if you need live view in that browser.
  Recording is unaffected, because it never re-encodes.
- **Live works on one device and not another** — almost always the WebRTC port.
  Sentinel serves live over MSE by default, which needs nothing extra, but if a
  reverse proxy is in front of it, make sure it forwards WebSocket upgrades.

Recording does not depend on the browser at all. Closing the tab, or the whole
machine you were watching from, changes nothing.

---

## Too many motion events

Motion detection is deliberately simple, so it is tuned rather than trained.

1. **Draw ignore zones** over whatever is moving and should not count — a road,
   a tree, a flag, a television through a window. This is the single most
   effective change.
2. **Lower the sensitivity** for that camera. Lower means a larger, clearer
   change is needed before anything registers.
3. **Check the camera has a substream.** Without one, motion is detected on a
   downscale of the main stream, which is both slower and noisier.

Lights switching on, infrared cut filters flipping and auto-exposure steps are
already rejected: when almost the whole frame changes at once, Sentinel treats it
as a lighting change rather than movement and relearns the scene immediately.

Rain and heavy foliage are the hard cases. Sensitivity plus zones usually gets
there; if a camera is genuinely hopeless, motion can be disabled for it alone
while it keeps recording continuously.

---

## No motion events at all

- Is `motion_enabled` on for the camera, and is its recording mode not `off`?
  Detecting motion on a camera that records nothing produces events with no
  footage behind them, so Sentinel does not do it.
- Check `System → Health` for `motion`.
- The detector needs about fifteen frames — three seconds at the default rate —
  to learn the scene before it reports anything. That is expected after a
  restart or a reconnection.

---

## Storage disappeared

The dashboard shows a red banner and recording stops. This is intentional.

The usual cause is a mountpoint whose filesystem is no longer mounted: the
directory still exists and is still writable, so writes would silently fill the
**system** disk instead of the storage. Sentinel detects this with a marker file
written when the storage was first accepted, and refuses to record when it is
missing.

To fix it, remount the storage. Recording resumes on its own within about half a
minute — no restart needed.

If you deliberately moved the storage, set the new path under
**Storage → path**. Sentinel validates it before saving.

---

## The disk is filling up

Retention runs every ten minutes and enforces three independent limits: age,
total size and minimum free space. If usage is still climbing:

- **Check for protected recordings.** Protection is absolute — retention stops
  rather than deleting something you chose to keep. The Storage page shows the
  protected count.
- **Nothing older than two minutes is ever deleted**, so a very small retention
  window still leaves the last few minutes on disk.
- Press **Apply now** on the Storage page to run a sweep immediately and see
  exactly what it removed.

---

## A gap in the timeline

Gaps are shown honestly rather than glossed over. A gap means one of:

- The camera was unreachable for that period. Its status and reconnection count
  are on the camera page.
- A segment was interrupted — usually a power cut. It is indexed as *damaged*
  rather than silently skipped. Because recordings are written as fragmented
  MP4, an interrupted file usually still plays up to the moment it stopped.
- The footage was deleted by retention.

---

## After restoring a backup

1. **Restart Sentinel.** The recorders, motion workers and go2rtc configuration
   were all built from the previous camera list.
2. **Re-enter every camera password.** Backups deliberately exclude them: they
   are encrypted with a secret that stays on the original machine, so an archive
   containing them would be either useless or unsafe.

The database that was replaced is kept alongside the new one as
`sentinel-replaced-<timestamp>.db`, so restoring the wrong archive is
recoverable.

---

## Home Assistant does not find Sentinel

- Discovery uses mDNS. It frequently does not cross VLANs or subnets, and some
  container network modes block it entirely. Adding the integration manually
  with the server address and an API token always works.
- Create the token under **Settings → API tokens**. It is shown once.
- If entities go `unavailable` and stay that way, check the token has not been
  revoked and that Home Assistant can reach the Sentinel port.

Entities going `unavailable` while Sentinel restarts is normal; they recover on
their own.

---

## Reading the logs

Every line carries a category: `SYSTEM`, `CAMERA`, `RECORDING`, `MOTION`,
`STORAGE`, `API`, `HOME_ASSISTANT`, `GO2RTC`, `DISCOVERY`.

```bash
docker logs -f sentinel
```

Credentials never appear: RTSP userinfo, `password=` and `token:` pairs,
`Authorization` headers and Sentinel API tokens are stripped before anything is
written. If you ever see one, that is a bug worth reporting.
