# Build from source on Proxmox

This layout builds Sentinel locally and keeps application data separate from
recordings:

- unprivileged Debian LXC;
- root disk on `local-lvm`;
- Docker builds from a local Git checkout;
- recordings use a separate Proxmox-managed mount point at `/recordings`;
- the root and recording volumes can be moved independently in Proxmox.

## Recommended layout

| Item | Initial value | Notes |
| --- | --- | --- |
| LXC | next unused ID | Never reuse an existing CT ID |
| Hostname | `sentinel` | DHCP with a router reservation recommended |
| CPU | 4 cores | Increase if motion workers fall behind |
| RAM | 4096 MB | The container limit in Compose is 3 GiB |
| Root disk | 16 GiB on `local-lvm` | OS, Docker, source and database |
| Recordings | separate managed volume | Mounted at `/recordings`; size for bitrate and retention |
| HTTPS | TCP 443 | Local reverse proxy and embedded Home Assistant page |
| HTTP | TCP 8090 | Sentinel origin; keep it on the LAN/tunnel only |
| WebRTC | TCP/UDP 8555 | Low-latency browser playback |

Approximate continuous recording consumption is `10.8 GB/day` for every
`1 Mbit/s` of aggregate camera bitrate. Ten 4 Mbit/s streams are therefore
about 432 GB/day before filesystem overhead.

## Build and start inside the LXC

Clone the repository at `/opt/sentinel-cctv`. If your checkout is private, use a
read-only deploy key scoped to this repository; the server does not need write
access to GitHub.

```bash
git clone https://github.com/jencarnacao19/sentinel-cctv.git /opt/sentinel-cctv
cd /opt/sentinel-cctv
docker compose -f compose.yml -f compose.source.yml up -d --build
install -m 0755 tools/sentinel-ctl /usr/local/bin/sentinel-ctl
sentinel-ctl health
```

Open `http://<LXC-IP>:8090`, create the first administrator, then configure one
camera before onboarding the rest.

## Move storage later

In the Proxmox interface, stop the LXC and use **Resources → Root Disk/MP0 →
Volume Action → Move Storage**. Move the root disk and recording mount point
separately. Confirm that `/recordings` is mounted and writable before starting
continuous recording again.

Never replace `/recordings` with an empty directory while Sentinel is running.
The application deliberately stops recording when its expected storage marker
is absent.

## Routine commands

```bash
sentinel-ctl status
sentinel-ctl health
sentinel-ctl logs
sentinel-ctl restart
sentinel-ctl update
```

From the Proxmox host:

```bash
pct status <CTID>
pct enter <CTID>
pct config <CTID>
```

## Reviewed builds and rollback

Runtime, build-tool and development dependencies are committed as exact versions
with artifact hashes in `backend/requirements*.lock`. `npm ci` retains the
frontend lockfile. Base-image digests and the three go2rtc download SHA-256 values
are pinned in Dockerfile. App-only edits no longer resolve new Python packages.
Debian package installation still uses its signed live repositories; a deliberate
cache-free OS rebuild is not bit-for-bit reproducible.
Review and refresh these pins for security updates; pinning is not patching.

To review a dependency update with `uv`:

```sh
cd backend
uv pip compile pyproject.toml --python-version 3.12 --universal --generate-hashes --no-header --no-annotate -o requirements.lock --upgrade-package PACKAGE
uv pip compile requirements-build.in --python-version 3.12 --universal --generate-hashes --no-header --no-annotate -o requirements-build.lock
uv pip compile pyproject.toml requirements-build.in --extra dev -c requirements.lock -c requirements-build.lock --python-version 3.12 --universal --generate-hashes --no-header --no-annotate -o requirements-dev.lock
```

Review the diff, run tests and build before publishing. CI installs locked
dependencies and runs `pip check` before the tests and Docker smoke check.

`sentinel-ctl update` / `rebuild` refuse dirty checkouts, retain the running
image as `sentinel-cctv:before-<UTC timestamp>`, build while it keeps running,
then replace it and check health. A build failure does not stop the service.
The helper prints `sentinel-ctl rollback <retained-image>` for manual recovery.
Rollback restores an image only: check migration compatibility first; source,
database and recordings are never reset. Retained images consume space and are
not automatically pruned. Reinstall the helper after updating this repository:
`install -m 0755 tools/sentinel-ctl /usr/local/bin/sentinel-ctl`.

## Home Assistant integration

Create a long-lived API token under **Sentinel → Settings → API tokens**, then
install the integration from this repository
([Home Assistant guide](home-assistant.md)). Use
`http://<LXC-IP>:8090` when automatic mDNS discovery does not cross the network
boundary.

The native integration and embedded footage console are independent. Configure
the integration with the LXC's LAN URL. For an HTTPS Home Assistant Webpage,
use `https://cctv.example.net/embed`: browsers reject embedding a plain HTTP LAN
address as mixed content. A more-specific Cloudflare Access application may
bypass only `/embed*`, `/assets/*`, `/sw.js`, `/api/v1/viewer/*` and
`/api/v1/streams/*`; Sentinel's viewer key still protects footage. Keep the
main hostname and administration under the normal Access policy.

The tunnel-based embed does not require port forwarding or split DNS.

Enable the viewer settings from the [remote access guide](remote-access.md), add the Home Assistant HTTPS
origin to `SENTINEL_FRAME_ANCESTORS`, and put the random `#viewer_key=...`
fragment only in the Home Assistant Webpage dashboard. The key belongs in the
private deployment `.env` and dashboard configuration; never place it in Git,
a Proxmox note or a screenshot. `/admin` continues to require the administrator
password. The operator can watch, play back, save and download footage, but
cannot configure cameras or delete/unprotect saved clips.

ONVIF discovery needs Docker host networking. The supplied Compose file uses it
while go2rtc's unauthenticated API and RTSP listeners stay on `127.0.0.1`.

## Current product boundaries

- Use recording mode `continuous` or `off`; `motion` and `scheduled` recording
  modes are not implemented yet.
- The Home Assistant camera entity serves snapshots. The current contract does
  not expose a live stream URL to Home Assistant.
- Test one real camera for at least 24 hours before sizing final retention.
- Permanent clips are stored on the separate `/recordings` volume under
  `.exports`; Sentinel retention will not remove them, so monitor their growth
  and back up that volume when a saved clip is irreplaceable.
- Do not delete a camera with footage until orphaned-file cleanup semantics are
  implemented.
