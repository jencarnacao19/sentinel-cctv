#!/usr/bin/env bash
#
# Exercises the pure logic inside install-proxmox.sh.
#
# What this proves: argument parsing, validation, version comparison, uid
# mapping, memory arithmetic, byte conversion, the two config-directory scans
# (path_already_mounted, list_marked_containers) and the parsing behind the
# recordings-disk menu behave correctly,
# including on the inputs that would silently do the wrong thing -- an octal-
# looking "016", a --recordings-path of /etc, a pveversion string from a future
# Proxmox.
#
# What this does NOT prove: that the installer works on a Proxmox host. Nothing
# here calls pct, pvesm, pveam or Docker; the container is never created, the
# image is never pulled, the bind mount is never made. Those paths need a real
# PVE node and cannot be honestly claimed from here.
#
# The installer is *sourced*, not run: its final line only calls main() when it
# is being executed directly, precisely so this file can reach the helpers.
#
# Usage: bash tools/test-install-proxmox.sh

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/../install-proxmox.sh"

PASSED=0
FAILED=0

pass() {
	PASSED=$((PASSED + 1))
	printf '  ok   %s\n' "$1"
}

fail() {
	FAILED=$((FAILED + 1))
	printf '  FAIL %s\n' "$1"
	printf '       expected: %s\n' "$2"
	printf '       actual:   %s\n' "$3"
}

check() {
	local label="$1" expected="$2" actual="$3"
	if [ "$expected" = "$actual" ]; then
		pass "$label"
	else
		fail "$label" "$expected" "$actual"
	fi
}

# Runs the installer in a subshell with the given arguments and prints its
# resolved configuration, so parsing and validation can be asserted on without
# any of it touching the host.
config_with() {
	bash "$INSTALLER" --print-config "$@" 2>&1
}

# The value of one `key=value` line from --print-config, e.g.
# setting_of cores -> "4".
setting_of() {
	local key="$1"
	shift
	config_with "$@" | sed -n "s/^$key=//p" | head -1
}

# True when the installer refuses these arguments. Failure has to be the exit
# status, not merely a message: a validator that complains and then carries on
# is worse than one that does not exist, because the operator stops reading.
rejects() {
	! bash "$INSTALLER" --print-config "$@" >/dev/null 2>&1
}

printf '\n== install-proxmox.sh: pure logic ==\n\n'

if [ ! -f "$INSTALLER" ]; then
	printf 'error: %s not found\n' "$INSTALLER" >&2
	exit 1
fi

# Sourcing must not install anything: if the guard at the bottom of the
# installer ever breaks, this file would start creating containers instead of
# testing them, so it is the very first thing checked.
printf -- '-- sourcing is side-effect free --\n'
# shellcheck source=/dev/null
if source "$INSTALLER" 2>/dev/null; then
	pass "the installer can be sourced without running main()"
else
	fail "the installer can be sourced without running main()" "exit 0" "non-zero"
fi

printf -- '\n-- pve_major_version --\n'
check "reads the major version" "8" "$(pve_major_version 'pve-manager/8.2.3/abcdef')"
check "handles a two-digit major" "10" "$(pve_major_version 'pve-manager/10.0.1/abcdef')"
check "empty on an unexpected shape" "" "$(pve_major_version 'not a version string')"
check "empty on an empty string" "" "$(pve_major_version '')"

printf -- '\n-- mapped_host_uid --\n'
# shellcheck disable=SC2034 # read by mapped_host_uid(), which comes from the
# sourced installer -- shellcheck cannot follow the assignment across that.
UNPRIVILEGED=yes
check "unprivileged shifts by the subuid base" "101000" "$(mapped_host_uid 1000)"
check "unprivileged shifts root too" "100000" "$(mapped_host_uid 0)"
# shellcheck disable=SC2034 # same reason as above
UNPRIVILEGED=no
check "privileged is unshifted" "1000" "$(mapped_host_uid 1000)"
# Not restored afterwards on purpose: nothing below reads it (every remaining
# check either is pure or runs the installer in its own subshell), and a
# restore that no longer protects anything is just a line to keep in step.

printf -- '\n-- compose_memory_limit_mb --\n'
check "leaves a gigabyte for the rest of the container" "3072" "$(compose_memory_limit_mb 4096)"
check "never drops below a usable floor" "1024" "$(compose_memory_limit_mb 1024)"
check "floor applies to tiny allocations too" "1024" "$(compose_memory_limit_mb 512)"
check "scales with a large allocation" "15360" "$(compose_memory_limit_mb 16384)"

printf -- '\n-- bytes_to_gib --\n'
check "exact gibibyte" "1" "$(bytes_to_gib 1073741824)"
check "truncates rather than rounding up" "1" "$(bytes_to_gib 2147483647)"
check "zero for a small count" "0" "$(bytes_to_gib 1048576)"

printf -- '\n-- path_already_mounted --\n'
CONF_DIR="$(mktemp -d)"
trap 'rm -rf "$CONF_DIR"' EXIT

cat >"$CONF_DIR/100.conf" <<'EOF'
arch: amd64
hostname: other
mp0: /tank/media,mp=/media
EOF
cat >"$CONF_DIR/101.conf" <<'EOF'
arch: amd64
hostname: sentinel-old
mp0: /tank/recordings,mp=/recordings
EOF

check "finds the container using a path" "101" "$(path_already_mounted /tank/recordings "$CONF_DIR")"
check "finds a different container's path" "100" "$(path_already_mounted /tank/media "$CONF_DIR")"
if path_already_mounted /tank/unused "$CONF_DIR" >/dev/null; then
	fail "an unused path is free" "non-zero exit" "exit 0"
else
	pass "an unused path is free"
fi
# A prefix must not count as a match: bind-mounting /tank/rec when /tank/recordings
# exists is legitimate, and refusing it would block a valid layout.
if path_already_mounted /tank/rec "$CONF_DIR" >/dev/null; then
	fail "a prefix of another path is not a match" "non-zero exit" "exit 0"
else
	pass "a prefix of another path is not a match"
fi

printf -- '\n-- storage_cfg_directories --\n'
cat >"$CONF_DIR/storage.cfg" <<'EOF'
dir: local
	path /var/lib/vz
	content iso,vztmpl,backup

lvmthin: local-lvm
	thinpool data
	vgname pve
	content rootdir,images

dir: disk2
	path /mnt/disk2
	content backup

nfs: nas
	export /volume1/proxmox
	server 192.168.50.10
	content backup

cifs: old-share
	path /mnt/pve/old-share
	server 192.168.50.11
	disable

zfspool: tank
	pool tank
	content images,rootdir
EOF
check "lists folder storages with their paths, block storage left out" \
	"$(printf 'local\t/var/lib/vz\ndisk2\t/mnt/disk2\nnas\t/mnt/pve/nas')" \
	"$(storage_cfg_directories "$CONF_DIR/storage.cfg")"
check "nothing when there is no storage.cfg" "" "$(storage_cfg_directories "$CONF_DIR/missing.cfg")"

printf -- '\n-- data_mount_targets --\n'
MOUNTS="$(printf '%s\n' \
	'/ ext4' \
	'/boot/efi vfat' \
	'/mnt/usb ext4' \
	'/mnt/pve/nas nfs4' \
	'/media/backup ntfs3' \
	'/srv/footage xfs' \
	'/mnt/iso iso9660' \
	'/tank zfs' \
	'/rpool/ROOT/pve-1 zfs' \
	'/var/lib/docker ext4' \
	'/run/lock tmpfs')"
check "offers data disks and shares, never the system's own mounts" \
	"$(printf '/mnt/usb\n/mnt/pve/nas\n/media/backup\n/srv/footage\n/tank')" \
	"$(printf '%s\n' "$MOUNTS" | data_mount_targets)"

printf -- '\n-- filesystem_key --\n'
check "an ordinary filesystem is its device" "/dev/sdb1" "$(filesystem_key /dev/sdb1 ext4)"
check "a ZFS dataset is its pool" "rpool" "$(filesystem_key rpool/data zfs)"
check "the system dataset shares that pool" "rpool" "$(filesystem_key rpool/ROOT/pve-1 zfs)"

printf -- '\n-- probe_recordings_candidate --\n'
# timeout(1) runs programs, not shell functions, so the stand-ins for df and
# findmnt are small scripts put first on PATH.
PROBE_DIR="$(mktemp -d)"
mkdir -p "$PROBE_DIR/bin"
cat >"$PROBE_DIR/bin/findmnt" <<'EOF'
#!/usr/bin/env bash
printf '/dev/sdb1 ext4\n'
EOF
cat >"$PROBE_DIR/bin/df" <<'EOF'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/sdb1 100 50 2048 50%% /\n'
EOF
chmod +x "$PROBE_DIR/bin/findmnt" "$PROBE_DIR/bin/df"
check "reports free bytes and the disk it lives on" \
	"2097152|/dev/sdb1" \
	"$(PATH="$PROBE_DIR/bin:$PATH" probe_recordings_candidate "$PROBE_DIR" | tr '\t' '|')"
cat >"$PROBE_DIR/bin/df" <<'EOF'
#!/usr/bin/env bash
sleep 30
EOF
began=$SECONDS
if PATH="$PROBE_DIR/bin:$PATH" probe_recordings_candidate "$PROBE_DIR" >/dev/null; then
	fail "a folder that never answers is given up on" "non-zero exit" "exit 0"
else
	pass "a folder that never answers is given up on"
fi
check "and quickly, not after the share times out" "yes" "$([ $((SECONDS - began)) -lt 10 ] && echo yes || echo no)"
rm -rf "$PROBE_DIR"

printf -- '\n-- choose_recordings_storage --\n'
# The whole menu, against three folders standing in for disks. findmnt,
# df and the storage list are replaced; ask() answers with $MENU_ANSWER, or
# with the default it was offered when that is empty.
DISKS="$(mktemp -d)"
mkdir -p "$DISKS/vz" "$DISKS/usb" "$DISKS/big"
storage_cfg_directories() {
	# "nas" stands for a share whose server is off: its probe gives up.
	printf 'local\t%s\nusb\t%s\nbig\t%s\nnas\t%s\n' "$DISKS/vz" "$DISKS/usb" "$DISKS/big" "$DISKS/nas"
}
findmnt() {
	case "$*" in
	*TARGET,FSTYPE*) return 0 ;;
	esac
	printf '/dev/mapper/pve-root ext4\n'
}
probe_recordings_candidate() {
	case "$1" in
	"$DISKS/vz") printf '%s\t%s' $((80 * 1073741824)) /dev/mapper/pve-root ;;
	"$DISKS/usb") printf '%s\t%s' $((500 * 1073741824)) /dev/sdb1 ;;
	"$DISKS/big") printf '%s\t%s' $((3000 * 1073741824)) /dev/sdc1 ;;
	*) return 1 ;;
	esac
}
ask() {
	printf -v "$1" '%s' "${MENU_ANSWER:-$3}"
}
MENU_FILE="$DISKS/menu.txt"
# shellcheck disable=SC2034 # read by the sourced installer, which shellcheck cannot follow
pick_disk() {
	RECORDINGS_PATH=""
	ASSUME_YES=0
	DISK=16
	TTY_DEVICE="$MENU_FILE"
	MENU_ANSWER="$1"
	: >"$MENU_FILE"
	choose_recordings_storage >/dev/null
}
pick_disk ""
check "the roomiest disk that is not the system disk is the default" \
	"$DISKS/big/sentinel-recordings" "$RECORDINGS_PATH"
check "the system disk is listed last among the disks, and says so" \
	"1" "$(grep -c '^ *3) .*the disk Proxmox runs from' "$MENU_FILE")"
check "the container itself is always the last choice" \
	"1" "$(grep -c '^ *4) Inside the container (16 GB)' "$MENU_FILE")"
check "a folder that cannot be read in time is left out" \
	"0" "$(grep -c '/nas' "$MENU_FILE")"
pick_disk 3
check "the system disk can still be chosen on purpose" \
	"$DISKS/vz/sentinel-recordings" "$RECORDINGS_PATH"
pick_disk 4
check "the last choice keeps recordings inside the container" "" "$RECORDINGS_PATH"
pick_disk ""
# shellcheck disable=SC2034 # same reason as above
ASSUME_YES=1
RECORDINGS_PATH=""
choose_recordings_storage >/dev/null
check "--yes asks nothing and keeps the old default" "" "$RECORDINGS_PATH"
RECORDINGS_PATH="/tank/recordings"
ASSUME_YES=0
choose_recordings_storage >/dev/null
check "--recordings-path is never second-guessed" "/tank/recordings" "$RECORDINGS_PATH"
rm -rf "$DISKS"
unset -f storage_cfg_directories findmnt probe_recordings_candidate ask pick_disk

printf -- '\n-- list_marked_containers --\n'
cat >"$CONF_DIR/102.conf" <<EOF
arch: amd64
hostname: sentinel
description: $MARKER
EOF
check "lists only containers carrying the marker" "102" "$(list_marked_containers "$CONF_DIR")"

EMPTY_DIR="$(mktemp -d)"
check "empty when nothing is marked" "" "$(list_marked_containers "$EMPTY_DIR")"
rmdir "$EMPTY_DIR"

printf -- '\n-- argument parsing --\n'
check "defaults to four cores" "4" "$(setting_of cores)"
check "a flag overrides the default" "8" "$(setting_of cores --cores 8)"
check "hostname is overridable" "nvr" "$(setting_of hostname --hostname nvr)"
# 016 must be read as sixteen, not as octal fourteen -- shell arithmetic would
# quietly do the latter.
check "a leading zero is decimal, not octal" "16" "$(setting_of cores --cores 016)"

printf -- '\n-- validation refuses bad input --\n'
for bad in "--cores abc" "--cores 0" "--cores 999" "--memory 64" "--hostname -bad" "--hostname bad-" "--hostname has_underscore"; do
	# shellcheck disable=SC2086 # deliberate word splitting: each entry is a flag plus its value
	if rejects $bad; then
		pass "refuses $bad"
	else
		fail "refuses $bad" "non-zero exit" "exit 0"
	fi
done

for bad_path in "/" "/etc" "/usr" "/boot/efi" "relative/path"; do
	if rejects --recordings-path "$bad_path"; then
		pass "refuses --recordings-path $bad_path"
	else
		fail "refuses --recordings-path $bad_path" "non-zero exit" "exit 0"
	fi
done

for good_path in "/tank/recordings" "/mnt/cctv" "/srv/sentinel/recordings"; do
	if rejects --recordings-path "$good_path"; then
		fail "accepts --recordings-path $good_path" "exit 0" "non-zero exit"
	else
		pass "accepts --recordings-path $good_path"
	fi
done

printf -- '\n-- build switch helper --\n'
helper_dir="$(mktemp -d)"
write_channel_helper "$helper_dir"
compose="$helper_dir/compose.yml"
printf 'services:\n  sentinel:\n    image: ghcr.io/jencarnacao19/sentinel-cctv:latest\n  web:\n    image: nginx:latest\n' >"$compose"
sh "$helper_dir/sentinel-channel" --retag "$compose" dev
check "moves the Sentinel image to :dev" "    image: ghcr.io/jencarnacao19/sentinel-cctv:dev" "$(grep sentinel-cctv "$compose")"
check "leaves every other image alone" "    image: nginx:latest" "$(grep nginx "$compose")"
sh "$helper_dir/sentinel-channel" --retag "$compose" stable
check "moves it back to :latest" "    image: ghcr.io/jencarnacao19/sentinel-cctv:latest" "$(grep sentinel-cctv "$compose")"
printf 'services:\n  sentinel:\n    image: sentinel-cctv:local\n' >"$compose"
if sh "$helper_dir/sentinel-channel" --retag "$compose" dev; then
	fail "refuses a compose file built from source" "non-zero exit" "exit 0"
else
	pass "refuses a compose file built from source"
fi
check "the source-built compose file is untouched" "    image: sentinel-cctv:local" "$(grep image "$compose")"
check "the timer looks every 30 seconds" "OnUnitActiveSec=30s" "$(grep OnUnitActiveSec "$helper_dir/sentinel-channel.timer")"
if sh -n "$helper_dir/sentinel-channel"; then
	pass "the helper is valid POSIX sh"
else
	fail "the helper is valid POSIX sh" "exit 0" "syntax error"
fi
rm -rf "$helper_dir"

printf -- '\n-- help and usage --\n'
if bash "$INSTALLER" --help >/dev/null 2>&1; then
	pass "--help exits successfully"
else
	fail "--help exits successfully" "exit 0" "non-zero exit"
fi

if bash "$INSTALLER" --not-a-real-flag >/dev/null 2>&1; then
	fail "an unknown flag is refused" "non-zero exit" "exit 0"
else
	pass "an unknown flag is refused"
fi

printf '\n== %d passed, %d failed ==\n\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]
