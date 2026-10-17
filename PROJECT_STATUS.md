# Sentinel CCTV — project status

Sentinel is an early-development (`0.1.0`) local video recorder for RTSP
cameras. Deployments should be validated with the cameras, storage and browsers
that will actually be used. Automated tests do not establish compatibility with
every device or firmware version.

## Available capabilities

- Continuous RTSP recording with video stream copy, supervised recorders,
  automatic segment indexing and per-camera recording controls.
- A shared go2rtc relay for live viewing and motion analysis, with a per-camera
  recorder fallback for streams that cannot initialise through the relay.
- OpenCV motion detection, ignore zones, event snapshots and a searchable
  timeline with playback, downloads and permanent saved clips.
- Storage monitoring, retention policies and previews, protected footage and
  safeguards against recording onto an unavailable storage mount.
- Local administrator accounts, scoped integration tokens, encrypted camera
  credentials and a separate restricted footage-operator interface.
- A Home Assistant API and an embedded dashboard for live video and playback.
- ONVIF camera discovery and supported PTZ controls, presets and native patrols.
  Optional local Tapo controls depend on camera capabilities.
- Persistent privacy mode that pauses camera access and recording while keeping
  historical footage available.
- Source-built Docker deployment, a Proxmox installation path, pinned application
  dependencies and a deployment helper that retains an image for rollback.

## Limitations

- Use recording modes `continuous` or `off`. Motion-triggered and scheduled
  recording are not complete modes. A camera's recording switch is independent
  of its configured mode.
- Tapo battery cameras (TC82/H200) are not in releases. They are being
  developed on the `dev` branch and its `:dev` image, for test servers only.
- The native Home Assistant camera entity provides snapshots. Use the embedded
  Sentinel dashboard for live viewing and timeline playback.
- ONVIF discovery requires enabled, reachable camera services and a network that
  permits multicast. Manual RTSP configuration remains available. Devices with
  insufficient identification data remain unidentified until configured.
- Local Tapo control requests have a 14-second deadline. A camera that cannot
  respond within it is reported as unavailable.
- Saved clips are protected from automatic Sentinel retention, but not from
  storage loss. Back up both application data and irreplaceable footage.
- Camera deletion and orphaned recording-file cleanup need further work. Avoid
  deleting camera entries with footage you need to retain.
- Remote access requires a trusted VPN or a correctly configured HTTPS proxy.
  Keep the Sentinel origin private. Cloudflare Access viewer grants require
  explicit issuer and audience configuration and signed assertion verification.
- Image rollback does not downgrade the database schema. Check migration
  compatibility and retain an application-data backup before an update.
- Dependency pins make application builds more predictable; Debian packages
  still come from signed live repositories, so OS rebuilds are not bit-for-bit
  reproducible.

Optional object detection, notification delivery, evidence manifests and
protected-clip off-site backup are proposals in the
[camera roadmap](docs/CAMERA_ROADMAP.md), not implemented capabilities.

## Deployment acceptance

Before relying on a new installation or an update:

1. Confirm system health, recorder counts, motion-worker health and free space
   on both the application-data and recording volumes.
2. Record from each intended camera for at least 24 hours and check playback,
   gaps, audio where enabled and the projected retention period.
3. Test live view, timeline seeking and a short export in the actual desktop,
   phone and Home Assistant clients that will be used.
4. Confirm administrator, integration and footage-operator access boundaries,
   including the reverse-proxy path if remote access is enabled.
5. Verify that a restart resumes recording and that backups include the
   database, encryption material and any footage that must be recoverable.

For installation and operations, see the [README](README.md),
[source-build guide](docs/private-proxmox.md) and
[troubleshooting guide](docs/troubleshooting.md).
