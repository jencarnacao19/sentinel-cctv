# Installing on Proxmox VE

`install-proxmox.sh` creates an unprivileged Debian LXC on a Proxmox node,
installs Docker inside it, starts Sentinel, and prints the URL. This document
covers what it does, what it refuses to do, and everything that comes after.

Every claim below is checked against `install-proxmox.sh` itself. Where a
number or a default is mentioned, it is the one in the script.

## The one command

Run this on the **Proxmox host**, as root, over SSH or in the node's shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)"
```

It asks for anything it needs and takes sensible defaults for the rest. To run
it without prompts — from Ansible, cron, or a pipe:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)" -- --yes --ctid 120 --storage local-lvm
```

The bare `--` is not optional in that form: it separates `bash`'s own arguments
from the script's.

Prefer to read it first? That is the right instinct for anything piped from the
internet into a root shell:

```bash
curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh -o install-proxmox.sh
less install-proxmox.sh
bash install-proxmox.sh
```

When it finishes it prints Sentinel's address, for example
`http://192.168.50.60:8090`. The same address is shown on the container's
console in the Proxmox web interface. Open it to set Sentinel up: the
administrator account, recordings, cameras, family accounts and Home Assistant.

## What it will not do

The script runs as root on a machine that is probably holding other people's
virtual machines. It is built around not being trusted:

- **It never formats a disk or creates a filesystem.** Storage is mounted, never
  made.
- **It never reuses an existing container ID.** If the ID you chose is taken, it
  stops and says so, having changed nothing.
- **It never writes into a directory another container already has mounted.**
  `path_already_mounted()` scans `/etc/pve/lxc/*.conf` and names the container
  that owns the path.
- **It refuses obvious mistakes.** `--recordings-path /etc` is rejected, as are
  `/`, `/usr`, `/boot/...` and anything else that looks like a system directory
  rather than storage.
- **It cleans up after itself.** A run that dies half way offers to remove the
  container it created — and only that one, verified by a marker in the
  container's description before anything is destroyed. A recordings directory
  is removed only if this run created it *and* it is still empty; the moment
  Sentinel writes a single file there, it stops being the script's to delete.

To see exactly what a run would do without doing any of it:

```bash
bash install-proxmox.sh --dry-run
```

To check your flags parse the way you expect:

```bash
bash install-proxmox.sh --print-config --cores 8 --recordings-path /tank/cctv
```

## Defaults

| Setting | Default | Flag |
| --- | --- | --- |
| Container ID | next free | `--ctid` |
| Hostname | `sentinel` | `--hostname` |
| Cores | 4 | `--cores` |
| Memory | 4096 MB | `--memory` |
| Root disk | 16 GB | `--disk` |
| Swap | 512 MB | `--swap` |
| Bridge | `vmbr0` | `--bridge` |
| Debian series | 13, falling back to 12 | `--debian` |
| Unprivileged | yes | `--unprivileged` |
| Timezone | the host's | `--timezone` |
| Web port | 8090 | — |

The root disk holds the OS, Docker and the image — **not** your recordings. 16
GB is right for that job and wrong for footage; see the next section.

Every flag also has a `SENTINEL_INSTALL_*` environment variable (`_CTID`,
`_CORES`, `_RECORDINGS_PATH`, and so on). A flag always wins over the variable.

## Storage for recordings

This is the decision that matters, and the one most likely to be got wrong.

### At install time

The installer lists, numbered, the disks this host has mounted: Proxmox
directory storages (local folders and NFS, SMB and CephFS shares), disks
mounted under `/mnt`, `/media` or `/srv`, and ZFS datasets with a mountpoint of
their own. Each shows its free space; the disk Proxmox runs from is listed last
and marked, because continuous recording fills it within days. The roomiest
other disk is the default. Recordings go into a new `sentinel-recordings`
folder on the disk you pick, created with the ownership Sentinel needs inside
the container.

Nothing is formatted or mounted: a disk that is not mounted on the host does
not appear. Mount it first (or add it as a Proxmox directory storage), then run
the installer.

To choose without the menu, or for a folder the menu does not list:

```bash
bash install-proxmox.sh --recordings-path /tank/cctv
```

With `--yes` and no `--recordings-path`, recordings stay on the container's own
16 GB root disk, as before.

### Afterwards

```bash
pct stop <ctid>
pct set <ctid> -mp0 /your/existing/path,mp=/recordings
chown -R 101000:101000 /your/existing/path
pct start <ctid>
```

Nothing there is destructive: `pct set` records a mount, it does not format.

### Why 101000

An unprivileged container shifts its user IDs by 100000. Sentinel runs as uid
1000 inside the container, which is uid **101000** on the host. Get this wrong
and the container cannot write to the directory — the single most common way an
install like this fails, and the reason the script checks it up front and
explains rather than letting FFmpeg fail later with a permission error nobody
connects back to the mount.

For a privileged container (`--privileged`, not recommended) there is no shift:
`chown -R 1000:1000` instead.

### What Sentinel does if the mount disappears

It stops recording and says so, rather than quietly filling the container's root
disk with footage it was told to write elsewhere. A marker file distinguishes
"the storage is mounted" from "the mountpoint exists but the filesystem is
gone". Recording resumes by itself when the storage comes back. See
[`recording.md`](recording.md).

## Why the container needs `nesting` and `keyctl`

Docker inside an unprivileged LXC needs two features enabled, and the script
sets both:

- **`nesting=1`** lets the container mount the filesystems Docker needs for its
  own layers and for `/proc`-like mounts inside its containers.
- **`keyctl=1`** gives the container its own kernel keyring. Without it,
  containerd's keyring calls fail and Docker will not start at all.

Neither is a hole in the unprivileged model — they are both scoped to the
container's own ID mapping.

## Updating

Re-running the script is safe. It looks for a container it built before, by a
marker in the container description, and offers to pull the latest image and
restart Sentinel there rather than building a second one:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)" -- --yes
```

An update never rewrites `compose.yml` inside the container, so anything you
changed there by hand survives.

- `--new` forces a second, independent instance.
- `--update` refuses to create anything: it updates an existing install or
  fails.
- `--ctid` targets one specific install when you have several.

Your database, configuration and recordings are untouched by an update. The
schema is migrated in place on the next start — never recreated.

## Uninstalling

```bash
pct stop <ctid>
pct destroy <ctid>
```

That removes the container and its root disk. **If you bind-mounted recordings
from the host, they are not in the container and are not removed** — which is
the point of putting them there. Delete that directory yourself if you actually
want the footage gone.

## When it goes wrong

**"This does not look like a Proxmox VE host."**
The script only runs on a PVE node, not inside a container and not on a plain
Debian box. Run it on the hypervisor.

**"Container ID N is already in use."**
Nothing was changed. Pick a free ID with `--ctid`, or remove that container
yourself first — the script will not do it for you.

**"Not enough space."**
The message names how much is needed and how much exists. Either free space on
that storage or point `--storage` at another one.

**No template available.**
The script downloads a Debian template if the node has none, which needs the
node to reach `download.proxmox.com`. On an air-gapped node, fetch the template
yourself first with `pveam download <storage> <template>`.

**Docker will not start inside the container.**
Almost always `nesting` or `keyctl` missing on a container that was not created
by this script. Check with `pct config <ctid> | grep features`.

**The container has no network.**
`--bridge` defaulted to `vmbr0`, which may not be your bridge. `ip link` on the
host lists them; re-run with the right one.

**Sentinel is running but the page will not load.**
`pct exec <ctid> -- docker compose -f /opt/sentinel/compose.yml logs` shows what
the application is saying. [`troubleshooting.md`](troubleshooting.md) covers
Sentinel's own failure modes; this document only covers the container around it.

## What has and has not been tested

The installer's pure logic — argument parsing, validation, version comparison,
uid mapping, memory arithmetic, the config-directory scans, and the recordings
disk menu against stand-in disks — is covered by
`tools/test-install-proxmox.sh`, which runs anywhere:

```bash
bash tools/test-install-proxmox.sh
```

The script also passes `shellcheck` at `--severity=style`.

**None of that proves it works on a Proxmox host.** No test here calls `pct`,
`pvesm`, `pveam` or Docker; no container is created, no image pulled, no bind
mount made. Those paths need a real PVE node, and have not been exercised on
one.
