<div align="center">

<img src="frontend/public/favicon.svg" width="72" height="72" alt="">

# Sentinel CCTV

**Local-first video surveillance for RTSP cameras.**
Continuous recording, motion detection, a timeline and a Home Assistant
integration. Runs on a small home server without a GPU.

</div>

---

> **Early development (`2.0.0`).** Test it with your own cameras and storage
> before relying on it. [`PROJECT_STATUS.md`](PROJECT_STATUS.md) lists what
> works today and what does not yet.

## Quick start

You need a Linux computer or server with Docker (a Proxmox VM or LXC works),
on the same network as your cameras. Images are published for `amd64` and
`arm64` (for example a Raspberry Pi 4 or 5).

**1. Install Sentinel**

```bash
mkdir sentinel && cd sentinel
curl -fsSLO https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/compose.yml
mkdir recordings && sudo chown 1000:1000 recordings
docker compose up -d
```

Recordings are saved in the `recordings` folder. To keep them on another disk,
change `./recordings` in `compose.yml` before starting.

To build the image yourself instead, clone this repository and run
`docker compose -f compose.yml -f compose.source.yml up -d --build`; recordings
then go to `/recordings`, which must belong to uid 1000.

**2. Open Sentinel.** `docker compose logs sentinel` prints the address to open,
for example `http://192.168.50.50:8090`. Sentinel then walks you through the
setup: the administrator account, where recordings go and for how long, your
cameras (found on the network or added by RTSP address), accounts for the
family, and Home Assistant.

**3. Watch.** After the setup the same address opens the camera wall. Family
members sign in there with the accounts you made for them; administration is
under **Admin**.

**4. Optional: add it to Home Assistant**

> The Home Assistant integration needs the Sentinel server from step 1. On its
> own it does not record or connect to cameras.

1. In Sentinel, create a token under **Settings → API tokens** (the setup offers
   this as its last step).
2. In Home Assistant, open **HACS**, search for **Sentinel CCTV**, download it
   and restart Home Assistant.
3. Sentinel usually appears on its own under **Settings → Devices & services**
   (otherwise use **Add integration → Sentinel CCTV**). Add it and paste the
   token.

Each camera then appears in Home Assistant with snapshots, a motion sensor and
a recording switch. More in the [Home Assistant guide](docs/home-assistant.md).

## What it does

- **Records continuously** without re-encoding the video, so ten cameras run
  comfortably on a 4-core home server. No GPU needed.
- **Detects motion** on the CPU and marks it on a searchable timeline with
  playback. Named zones each get their own sensitivity and Home Assistant
  sensor, and areas you mark as ignored never trigger.
- **Moves to a new machine** with one YAML file holding every camera, zone
  and setting. Camera passwords go in only if you set a passphrase.
- **Manages storage** with retention rules and permanent saved clips.
- **Controls cameras**: ONVIF pan/tilt/zoom and presets, plus a privacy mode
  that pauses every camera.
- **Stays local**: no cloud and no telemetry. Recording keeps working without
  Home Assistant or the Internet.

## Requirements

| | |
| --- | --- |
| Host | Proxmox VE, or any machine that runs Docker |
| CPU | 4 cores is comfortable for ~10 cameras with stream copy |
| RAM | 4 GB |
| OS disk | 16 GB |
| Recordings | Separate storage, sized for your retention |
| GPU | **Not required.** Not used at all. |

## Keep it safe

- Back up the Docker volume `sentinel-data` (settings, database and encryption
  keys) and the recordings folder.
- Never expose port `8090` directly to the Internet. For remote access use a
  VPN or a reverse proxy; see [Remote access](docs/remote-access.md).
- Found a security problem? See the [security policy](SECURITY.md).

## Documentation

| Guide | What is in it |
| --- | --- |
| [Using Sentinel](docs/features.md) | PTZ, recording switch, motion zones, moving machines, privacy mode, timeline, saved clips, battery cameras |
| [Home Assistant](docs/home-assistant.md) | Integration, dashboard and troubleshooting |
| [Proxmox](docs/private-proxmox.md) | Building and running Sentinel in a Proxmox LXC |
| [Remote access](docs/remote-access.md) | Footage console, Cloudflare Access and the embedded viewer |
| [Configuration](docs/configuration.md) | Settings and sizing |
| [Recording](docs/recording.md) | Segments, retention and getting footage out |
| [Troubleshooting](docs/troubleshooting.md) | Common problems |
| [Architecture](docs/architecture.md) | How it is built |
| [Security](docs/security.md) | Threat model and protections |
| [Development](docs/development.md) | Running from source and the checks CI runs |

## Licence

[MIT](LICENSE) © João Encarnação
