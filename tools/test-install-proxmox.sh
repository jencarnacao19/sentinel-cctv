#!/usr/bin/env bash
#
# Exercises the pure logic inside install-proxmox.sh.
#
# What this proves: argument parsing, validation, version comparison, uid
# mapping, memory arithmetic, byte conversion, and the two config-directory
# scans (path_already_mounted, list_marked_containers) behave correctly,
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
