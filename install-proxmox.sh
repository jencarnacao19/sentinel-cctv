#!/usr/bin/env bash
#
# Sentinel CCTV -- Proxmox VE installer.
#
# Creates an unprivileged Debian LXC, installs Docker inside it, starts Sentinel
# and prints the URL. One command on the Proxmox host, then a browser:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)"
#
# In that form bash consumes the first arguments itself, so flags need a `--`
# separator:
#
#   bash -c "$(curl -fsSL .../install-proxmox.sh)" -- --yes --ctid 120
#
# What this script will never do: format a disk, create a filesystem, delete
# existing data, or reuse a container ID that already exists. Every command that
# changes anything on the host is printed before it runs.
#
# Re-running it is safe: it looks for a container it created before (a marker
# in the description, see MARKER below) and offers to update that instead of
# building a second one. See --new / --update if you want to force one path.

# -E (errtrace) is the one easy to forget: without it, a trap on ERR is not
# inherited by shell functions, and nearly everything below lives in a
# function. Skip it and a real failure three functions deep would just exit
# silently instead of reporting the line that failed.
set -Eeuo pipefail

# --- Constants ---------------------------------------------------------------

readonly SENTINEL_IMAGE="ghcr.io/jencarnacao19/sentinel-cctv:latest"
readonly SENTINEL_PORT=8090
readonly WEBRTC_PORT=8555
readonly INSTALL_DIR="/opt/sentinel"
readonly RECORDINGS_DIR="/recordings"

#: The image runs as this uid; bind-mounted recording storage must be writable
#: by it. See docs/proxmox.md, "Storage: attaching real disks".
readonly APP_UID=1000

#: Written into the container description. Rollback refuses to remove a
#: container that does not carry it, so a pre-existing container can never be
#: destroyed by a failure in here.
readonly MARKER="sentinel-cctv-installer"

readonly DOCS_PROXMOX="https://github.com/jencarnacao19/sentinel-cctv/blob/main/docs/proxmox.md"
readonly DOCS_RECORDING="https://github.com/jencarnacao19/sentinel-cctv/blob/main/docs/recording.md"

# Defaults. 4 cores and 4 GB comfortably run ~9 cameras with stream copy; the
# 16 GB root disk holds the OS, Docker and the image, not the footage.
readonly DEFAULT_HOSTNAME="sentinel"
readonly DEFAULT_CORES=4
readonly DEFAULT_MEMORY=4096
readonly DEFAULT_DISK=16
readonly DEFAULT_SWAP=512
readonly DEFAULT_BRIDGE="vmbr0"
readonly DEFAULT_DEBIAN=13
readonly FALLBACK_DEBIAN=12

# --- Environment variable overrides -------------------------------------------
#
# Every flag has a SENTINEL_INSTALL_* equivalent so the whole thing can be
# driven non-interactively without fighting `bash -c "$(curl ...)" --`
# argument-passing. Precedence is flag > env var > built-in default. Deliberately
# namespaced away from the app's own SENTINEL_* settings (SENTINEL_LOG_LEVEL and
# friends, consumed by config.py inside the container) -- those configure
# Sentinel once it is running; these configure this installer, once, on the host.

# Accepts the same words a human would type at a y/n prompt.
truthy() {
	case "${1:-}" in
	1 | y | Y | yes | Yes | YES | true | True | TRUE | on | On | ON) return 0 ;;
	*) return 1 ;;
	esac
}

# --- Settings, all overridable by flag, env var, or prompt --------------------

CTID="${SENTINEL_INSTALL_CTID:-}"
CT_HOSTNAME="${SENTINEL_INSTALL_HOSTNAME:-}"
CORES="${SENTINEL_INSTALL_CORES:-}"
MEMORY="${SENTINEL_INSTALL_MEMORY:-}"
DISK="${SENTINEL_INSTALL_DISK:-}"
SWAP="${SENTINEL_INSTALL_SWAP:-$DEFAULT_SWAP}"
STORAGE="${SENTINEL_INSTALL_STORAGE:-}"
TEMPLATE_STORAGE="${SENTINEL_INSTALL_TEMPLATE_STORAGE:-}"
BRIDGE="${SENTINEL_INSTALL_BRIDGE:-}"
UNPRIVILEGED="${SENTINEL_INSTALL_UNPRIVILEGED:-yes}"
DEBIAN_SERIES="${SENTINEL_INSTALL_DEBIAN:-}"
TIMEZONE="${SENTINEL_INSTALL_TIMEZONE:-}"

#: Absolute host path to bind-mount at $RECORDINGS_DIR. Empty (the default)
#: means recordings stay on the container's own root disk, to be pointed at
#: real storage by hand afterwards -- see resolve_recordings_storage() and
#: the "Afterwards" section of --help.
RECORDINGS_PATH="${SENTINEL_INSTALL_RECORDINGS_PATH:-}"

ASSUME_YES=0
DRY_RUN=0
KEEP_ON_FAILURE=0
PRINT_CONFIG=0
NO_COLOR_FLAG=0
FORCE_NEW=0
FORCE_UPDATE=0

truthy "${SENTINEL_INSTALL_YES:-}" && ASSUME_YES=1
truthy "${SENTINEL_INSTALL_DRY_RUN:-}" && DRY_RUN=1
truthy "${SENTINEL_INSTALL_KEEP_ON_FAILURE:-}" && KEEP_ON_FAILURE=1
truthy "${SENTINEL_INSTALL_NO_COLOR:-}" && NO_COLOR_FLAG=1
truthy "${SENTINEL_INSTALL_NEW:-}" && FORCE_NEW=1
truthy "${SENTINEL_INSTALL_UPDATE:-}" && FORCE_UPDATE=1

# --- Runtime state ------------------------------------------------------------

CREATED_CTID=""
TEMPLATE_VOLID=""
TEMPLATE_NAME=""
CT_IP=""
WORK_DIR=""
PROVISION_FILE=""
COMPOSE_FILE=""

#: Set by detect_existing_install() when a previous run of this script is found;
#: non-empty routes main() straight to update_existing_container() instead of
#: creating a second container.
UPDATE_TARGET=""

#: 1 when resolve_recordings_storage() created $RECORDINGS_PATH from nothing,
#: so rollback() knows it is safe to remove again on failure -- a directory
#: that already existed is the operator's, not this run's, to delete.
RECORDINGS_PATH_CREATED=0

C_RESET=""
C_BOLD=""
C_DIM=""
C_RED=""
C_YELLOW=""
C_GREEN=""
C_BLUE=""

# --- Output -------------------------------------------------------------------

init_colors() {
	if [ "$NO_COLOR_FLAG" -eq 1 ] || [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
		C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_YELLOW="" C_GREEN="" C_BLUE=""
		return
	fi
	C_RESET=$'\033[0m'
	C_BOLD=$'\033[1m'
	C_DIM=$'\033[2m'
	C_RED=$'\033[31m'
	C_YELLOW=$'\033[33m'
	C_GREEN=$'\033[32m'
	C_BLUE=$'\033[34m'
}

step() { printf '\n%s==> %s%s\n' "${C_BOLD}${C_BLUE}" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
note() { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_RESET"; }
warn() { printf '%swarning:%s %s\n' "${C_BOLD}${C_YELLOW}" "$C_RESET" "$*" >&2; }
error() { printf '%serror:%s %s\n' "${C_BOLD}${C_RED}" "$C_RESET" "$*" >&2; }

die() {
	error "$*"
	exit 1
}

# Prints a command, then runs it. Everything that changes the host goes through
# here, so the terminal is a complete record of what was done.
run() {
	local rendered="" quoted arg
	for arg in "$@"; do
		printf -v quoted '%q' "$arg"
		rendered+=" $quoted"
	done
	printf '%s   +%s%s\n' "$C_DIM" "$rendered" "$C_RESET"
	if [ "$DRY_RUN" -eq 1 ]; then
		printf '%s     (dry run: not executed)%s\n' "$C_DIM" "$C_RESET"
		return 0
	fi
	"$@"
}

# Reads one line from the terminal rather than stdin: the script itself may have
# arrived on stdin (`curl ... | bash`), and reading that would swallow the rest
# of the script.
read_tty() {
	local prompt_text="$1" target="$2"
	if [ ! -r /dev/tty ]; then
		die "No terminal is available for prompts. Re-run with --yes and the flags you want, or use: bash -c \"\$(curl -fsSL ...)\""
	fi
	printf '%s' "$prompt_text" >/dev/tty
	# "$target" is a variable *name* (e.g. "answer"), not the value -- read
	# assigns into whatever it expands to, which is exactly the indirection
	# ask() relies on below. shellcheck cannot see through that, hence the
	# disable: this is the one place it would otherwise flag real code as dead.
	# shellcheck disable=SC2229
	IFS= read -r "$target" </dev/tty || die "Input ended before the question was answered."
}

# ask VARNAME "Question" "default"
ask() {
	local target="$1" question="$2" default="$3" answer=""
	if [ "$ASSUME_YES" -eq 1 ]; then
		printf -v "$target" '%s' "$default"
		return 0
	fi
	read_tty "$(printf '%s%s%s [%s]: ' "$C_BOLD" "$question" "$C_RESET" "$default")" answer
	[ -n "$answer" ] || answer="$default"
	printf -v "$target" '%s' "$answer"
}

confirm() {
	local answer=""
	[ "$ASSUME_YES" -eq 1 ] && return 0
	while true; do
		read_tty "$(printf '%s%s%s [y/N]: ' "$C_BOLD" "$1" "$C_RESET")" answer
		case "$answer" in
		[yY] | [yY][eE][sS]) return 0 ;;
		"" | [nN] | [nN][oO]) return 1 ;;
		*) printf 'Please answer y or n.\n' >/dev/tty ;;
		esac
	done
}

# --- Help ---------------------------------------------------------------------

usage() {
	# The closing marker is deliberately not the bare word "USAGE": that word
	# also appears as a section header inside the text below, and a heredoc
	# terminator matches the first line that is exactly the marker -- so using
	# "USAGE" for both silently truncated this whole block after that header
	# and left the rest of the file to be parsed as shell commands.
	cat <<'USAGE_TEXT'
Sentinel CCTV installer for Proxmox VE.

Creates an unprivileged Debian LXC, installs Docker inside it, starts Sentinel
and prints the URL. It never formats a disk, never creates a filesystem and
never touches a container ID that already exists.

USAGE
  install-proxmox.sh [options]

  Straight from GitHub, prompting for anything not given:

    bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)"

  Unattended. The bare -- separates bash's own arguments from this script's:

    bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)" -- --yes --ctid 120 --storage local-lvm

OPTIONS
  --ctid <id>              Container ID. Default: the next free ID, or an
                           existing Sentinel install's ID -- see UPDATING.
  --hostname <name>        Container hostname. Default: sentinel
  --cores <n>              CPU cores, 1-128. Default: 4
  --memory <mb>            RAM in MB, 512-262144. Default: 4096
  --disk <gb>              Root disk in GB, 8-8192. Default: 16
                           This holds the OS, Docker and the image. Recordings
                           belong on separate storage; see --recordings-path.
  --storage <name>         Proxmox storage for the root disk. Default: the first
                           active storage that accepts container volumes.
  --template-storage <n>   Storage holding the LXC template. Default: the first
                           active storage that accepts templates.
  --bridge <name>          Network bridge. Default: vmbr0
  --recordings-path <dir>  Bind-mount this host directory at /recordings instead
                           of leaving recordings on the container's root disk.
                           Created if missing, with the ownership Sentinel needs
                           already applied. Default: none -- attach storage by
                           hand afterwards (see AFTERWARDS).
  --unprivileged[=yes|no]  Run the container unprivileged. Default: yes.
  --privileged             Same as --unprivileged=no. Not recommended.
  --debian <13|12>         Template series. Default: 13, falling back to 12 when
                           no Debian 13 template is available.
  --timezone <tz>          Container timezone, e.g. Europe/Lisbon. Default: the
                           Proxmox host's.
  -y, --yes                Never prompt. Every unset value takes its default.
  --dry-run                Print every command that would run, change nothing.
  --new                    Always create a fresh container, even if an existing
                           Sentinel install is found on this node.
  --update                 Only update an existing install; fail rather than
                           create one if none is found (or --ctid is not it).
  --keep-on-failure        Leave a half-built container in place instead of
                           offering to remove it.
  --print-config           Print the parsed settings and exit. Runs anywhere,
                           touches nothing; useful for checking flags.
  --no-color               Plain output.
  -h, --help               This text.

ENVIRONMENT
  Every option above has a SENTINEL_INSTALL_* variable, for driving this from
  cron, Ansible, or a pipe without juggling `--` and quoting: SENTINEL_INSTALL_
  CTID, _HOSTNAME, _CORES, _MEMORY, _DISK, _STORAGE, _TEMPLATE_STORAGE, _BRIDGE,
  _RECORDINGS_PATH, _UNPRIVILEGED, _DEBIAN, _TIMEZONE, _SWAP, and the boolean
  switches _YES, _DRY_RUN, _NEW, _UPDATE, _KEEP_ON_FAILURE, _NO_COLOR (any of
  1/yes/true/on). A flag always wins over the matching variable.

UPDATING
  Re-running this script is safe. It looks for a container it built before (by
  a marker in the container description) and, if it finds one, offers to pull
  the latest image and restart Sentinel there instead of building a second
  container:

    bash -c "$(curl -fsSL https://raw.githubusercontent.com/jencarnacao19/sentinel-cctv/main/install-proxmox.sh)" -- --yes

  This never rewrites compose.yml inside the container, so any changes made
  there by hand survive an update. Pass --new for a second, independent
  instance, or --ctid to target one of several existing installs by hand.

AFTERWARDS
  Without --recordings-path, recordings sit on the container's own root disk.
  Point Sentinel at storage you already have -- this only mounts a path, it is
  not destructive, nothing is formatted or erased:

    pct stop <ctid>
    pct set <ctid> -mp0 /your/existing/path,mp=/recordings
    chown -R 101000:101000 /your/existing/path   # unprivileged uid mapping
    pct start <ctid>

  docs/proxmox.md covers this installer in depth: attaching real storage,
  updating, uninstalling and troubleshooting the container itself.
  docs/recording.md covers what Sentinel does once it is running -- retention,
  and the marker file that tells it a recordings mount has disappeared.
USAGE_TEXT
}

# --- Argument parsing ---------------------------------------------------------

#: Sets OPT_VALUE from either --flag=value or the following argv entry. Not a
#: command substitution so that a bad flag can die in the main shell.
OPT_VALUE=""
take_value() {
	local flag="$1" inline="$2" inline_value="$3" next="${4-}" example="${5-VALUE}"
	if [ "$inline" -eq 1 ]; then
		[ -n "$inline_value" ] || die "$flag needs a value, for example ${flag}=${example}."
		OPT_VALUE="$inline_value"
		return 0
	fi
	case "$next" in
	"" | -*) die "$flag needs a value, for example $flag $example." ;;
	esac
	OPT_VALUE="$next"
}

parse_bool() {
	case "$1" in
	yes | y | true | 1 | on) OPT_VALUE="yes" ;;
	no | n | false | 0 | off) OPT_VALUE="no" ;;
	*) die "Expected yes or no, got '$1'." ;;
	esac
}

parse_args() {
	local opt val inline
	while [ "$#" -gt 0 ]; do
		opt="$1"
		val=""
		inline=0
		case "$opt" in
		--*=*)
			val="${opt#*=}"
			opt="${opt%%=*}"
			inline=1
			;;
		esac

		case "$opt" in
		-h | --help)
			usage
			exit 0
			;;
		--ctid)
			take_value "$opt" "$inline" "$val" "${2-}" 120
			CTID="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--hostname)
			take_value "$opt" "$inline" "$val" "${2-}" sentinel
			CT_HOSTNAME="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--cores)
			take_value "$opt" "$inline" "$val" "${2-}" 4
			CORES="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--memory | --ram)
			take_value "$opt" "$inline" "$val" "${2-}" 4096
			MEMORY="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--disk)
			take_value "$opt" "$inline" "$val" "${2-}" 16
			DISK="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--swap)
			take_value "$opt" "$inline" "$val" "${2-}" 512
			SWAP="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--storage)
			take_value "$opt" "$inline" "$val" "${2-}" local-lvm
			STORAGE="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--template-storage)
			take_value "$opt" "$inline" "$val" "${2-}" local
			TEMPLATE_STORAGE="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--bridge)
			take_value "$opt" "$inline" "$val" "${2-}" vmbr0
			BRIDGE="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--recordings-path)
			take_value "$opt" "$inline" "$val" "${2-}" /mnt/recordings
			RECORDINGS_PATH="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--debian)
			take_value "$opt" "$inline" "$val" "${2-}" 13
			DEBIAN_SERIES="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--timezone | --tz)
			take_value "$opt" "$inline" "$val" "${2-}" Europe/Lisbon
			TIMEZONE="$OPT_VALUE"
			[ "$inline" -eq 1 ] || shift
			;;
		--unprivileged)
			# Bare --unprivileged means yes; --unprivileged=no is the long form.
			# A following yes/no is accepted too, and can never be confused with
			# another flag because every flag starts with a dash.
			if [ "$inline" -eq 1 ]; then
				parse_bool "$val"
			else
				case "${2-}" in
				yes | y | true | 1 | on | no | n | false | 0 | off)
					parse_bool "$2"
					shift
					;;
				*) OPT_VALUE="yes" ;;
				esac
			fi
			UNPRIVILEGED="$OPT_VALUE"
			;;
		--privileged) UNPRIVILEGED="no" ;;
		-y | --yes) ASSUME_YES=1 ;;
		--dry-run) DRY_RUN=1 ;;
		--new) FORCE_NEW=1 ;;
		--update) FORCE_UPDATE=1 ;;
		--keep-on-failure) KEEP_ON_FAILURE=1 ;;
		--print-config) PRINT_CONFIG=1 ;;
		--no-color) NO_COLOR_FLAG=1 ;;
		--)
			# `bash -c "$(curl ...)" -- --yes` leaves a bare -- as $1.
			;;
		-*) die "Unknown option '$1'. Run with --help for the list." ;;
		*) die "Unexpected argument '$1'. Values go with their flag, for example --cores 4." ;;
		esac
		shift
	done

	validate_explicit_settings
}

# Checks whatever the operator actually supplied, by flag or environment
# variable, as soon as it has been read.
#
# resolve_settings() validates these again later, and has to: values that come
# from a prompt or from a default have not been through here. But leaving it
# *only* there means a typo is not caught until after the preflight has run and
# the operator has answered several questions, and it means --print-config --
# documented as "useful for checking flags" -- would happily print
# `cores=abc` and exit 0, telling them their flags were fine when they were
# not. Refusing here is what makes that claim true.
#
# Empty means "not supplied"; those are settled interactively later and are not
# this function's business.
validate_explicit_settings() {
	[ -z "$CTID" ] || { validate_int ctid "$CTID" 100 999999999 && CTID="$OPT_VALUE"; }
	[ -z "$CT_HOSTNAME" ] || validate_hostname "$CT_HOSTNAME"
	[ -z "$CORES" ] || { validate_int cores "$CORES" 1 128 && CORES="$OPT_VALUE"; }
	[ -z "$MEMORY" ] || { validate_int memory "$MEMORY" 512 262144 && MEMORY="$OPT_VALUE"; }
	[ -z "$DISK" ] || { validate_int disk "$DISK" 8 8192 && DISK="$OPT_VALUE"; }
	[ -z "$SWAP" ] || { validate_int swap "$SWAP" 0 65536 && SWAP="$OPT_VALUE"; }
	[ -z "$STORAGE" ] || validate_name storage "$STORAGE"
	[ -z "$TEMPLATE_STORAGE" ] || validate_name template-storage "$TEMPLATE_STORAGE"
	[ -z "$BRIDGE" ] || validate_name bridge "$BRIDGE"
	[ -z "$TIMEZONE" ] || validate_timezone "$TIMEZONE"
	[ -z "$RECORDINGS_PATH" ] || validate_recordings_path "$RECORDINGS_PATH"
}

# --- Validation ---------------------------------------------------------------

validate_int() {
	local name="$1" value="$2" min="$3" max="$4"
	case "$value" in
	'' | *[!0-9]*) die "--$name must be a whole number, got '$value'." ;;
	esac
	# Strip leading zeros before comparing: test treats 016 as octal.
	value=$((10#$value))
	if [ "$value" -lt "$min" ] || [ "$value" -gt "$max" ]; then
		die "--$name must be between $min and $max, got '$value'."
	fi
	OPT_VALUE="$value"
}

validate_name() {
	local name="$1" value="$2"
	case "$value" in
	'' | *[!A-Za-z0-9._-]*) die "--$name may only contain letters, digits, dot, underscore and dash, got '$value'." ;;
	esac
}

validate_hostname() {
	local value="$1"
	case "$value" in
	'' | -* | *-) die "--hostname must start and end with a letter or digit, got '$value'." ;;
	*[!A-Za-z0-9-]*) die "--hostname may only contain letters, digits and dashes, got '$value'." ;;
	esac
	[ "${#value}" -le 63 ] || die "--hostname must be 63 characters or fewer."
}

validate_timezone() {
	local value="$1"
	case "$value" in
	*[!A-Za-z0-9+_/-]*) die "--timezone contains characters that are not valid in a timezone name: '$value'." ;;
	esac
}

validate_recordings_path() {
	local value="$1"
	case "$value" in
	/*) ;;
	*) die "--recordings-path must be an absolute path, got '$value'." ;;
	esac
	# A narrow blocklist, not an allowlist: the goal is to stop an obvious typo
	# like --recordings-path /etc from bind-mounting over something that
	# matters, not to second-guess every legitimate mount point.
	case "$value" in
	/ | /bin | /boot | /dev | /etc | /lib | /lib64 | /proc | /root | /run | /sbin | /sys | /usr | /var | \
		/etc/* | /proc/* | /sys/* | /dev/* | /boot/*)
		die "--recordings-path '$value' looks like a system directory, not storage for recordings." ;;
	esac
}

# --- Small pure helpers ---------------------------------------------------------
#
# Kept free of side effects and of calls to pct/pvesm/pveam so tools/test-
# install-proxmox.sh can exercise them directly.

# Extracts the major version from pveversion's first line, e.g.
# "pve-manager/8.2.3/xxxxxxxx" -> "8". Empty if the string is not in that shape,
# which the caller treats as "could not tell" rather than as a failure.
pve_major_version() {
	printf '%s' "$1" | sed -n 's#^pve-manager/\([0-9]\{1,\}\)\..*#\1#p'
}

# Where container uid $1 lands on the host. An unprivileged container's uids
# are shifted by the range Proxmox reserves for root in /etc/subuid/subgid
# (100000-165535 by default); a privileged container uses the same uid on
# both sides because there is no shift to begin with.
mapped_host_uid() {
	if [ "$UNPRIVILEGED" = "yes" ]; then
		printf '%s' $(($1 + 100000))
	else
		printf '%s' "$1"
	fi
}

# The memory cap passed to Docker for the Sentinel container. Left with at
# least 1 GB below the LXC's own RAM so the container's other processes (systemd,
# dockerd, containerd) are never the thing that gets OOM-killed to make room.
compose_memory_limit_mb() {
	local mem="$1" limit
	limit=$((mem - 1024))
	[ "$limit" -ge 1024 ] || limit=1024
	printf '%s' "$limit"
}

# Non-empty (the owning CTID) if $1 is already an mpN target in some other
# container's config. Reads the raw config text rather than calling `pct
# config` for every container on the node, so it works before this run's own
# container exists and needs no stub beyond a directory of .conf files in
# tests. $2 overrides the config directory for tests; production always uses
# the real one.
path_already_mounted() {
	local path="$1" conf_dir="${2:-/etc/pve/lxc}" conf owner
	for conf in "$conf_dir"/*.conf; do
		[ -e "$conf" ] || continue
		if awk -F'[:,]' -v want="$path" '
			$1 ~ /^mp[0-9]+$/ {
				val = $2
				sub(/^[ \t]+/, "", val)
				if (val == want) { found = 1 }
			}
			END { exit !found }
		' "$conf"; then
			owner="$(basename "$conf" .conf)"
			printf '%s' "$owner"
			return 0
		fi
	done
	return 1
}

# CTIDs on this node whose description carries our MARKER, one per line. Reads
# the config text directly rather than `pct config` in a loop, for the same
# reason and with the same test seam as path_already_mounted() above -- $1
# overrides the config directory.
list_marked_containers() {
	local conf_dir="${1:-/etc/pve/lxc}" conf
	for conf in "$conf_dir"/*.conf; do
		[ -e "$conf" ] || continue
		if grep -q -F -- "$MARKER" "$conf" 2>/dev/null; then
			basename "$conf" .conf
		fi
	done
}

# Bytes free on Proxmox storage $1, per `pvesm status`. Columns are Name Type
# Status Total Used Available %, and -- unlike `df` -- with no --human-readable
# flag they are already raw bytes rather than 1K blocks, which is why this does
# not need a unit conversion the way df_avail_bytes() below does. Empty if the
# output could not be parsed, which the caller treats as "unknown" rather than
# failing the whole preflight over a formatting change in a future pvesm.
storage_avail_bytes() {
	pvesm status --storage "$1" 2>/dev/null | awk 'NR==2 { print $6 }'
}

# Bytes free on the filesystem that would hold path $1, walking up to the
# nearest existing ancestor first since $1 (a --recordings-path) may not exist
# yet. `df -Pk` is the portable form (POSIX output columns, kibibyte blocks);
# ordinary `df` defaults differ across distributions and are not worth trusting.
df_avail_bytes() {
	local path="$1" kib
	while [ ! -e "$path" ]; do
		path="$(dirname "$path")"
	done
	kib="$(df -Pk "$path" 2>/dev/null | awk 'NR==2 { print $4 }')"
	case "$kib" in
	'' | *[!0-9]*) return 1 ;;
	esac
	printf '%s' $((kib * 1024))
}

# Integer GiB for a byte count, for messages -- avoids depending on `bc` or on
# awk printf's rounding behaviour, which is not guaranteed identical everywhere
# this script runs.
bytes_to_gib() {
	printf '%s' $(($1 / 1073741824))
}

# --- Configuration dump -------------------------------------------------------

print_config() {
	printf 'ctid=%s\n' "${CTID:-auto}"
	printf 'hostname=%s\n' "${CT_HOSTNAME:-$DEFAULT_HOSTNAME}"
	printf 'cores=%s\n' "${CORES:-$DEFAULT_CORES}"
	printf 'memory=%s\n' "${MEMORY:-$DEFAULT_MEMORY}"
	printf 'disk=%s\n' "${DISK:-$DEFAULT_DISK}"
	printf 'swap=%s\n' "$SWAP"
	printf 'storage=%s\n' "${STORAGE:-auto}"
	printf 'template_storage=%s\n' "${TEMPLATE_STORAGE:-auto}"
	printf 'bridge=%s\n' "${BRIDGE:-$DEFAULT_BRIDGE}"
	printf 'recordings_path=%s\n' "${RECORDINGS_PATH:-none (manual attach afterwards)}"
	printf 'unprivileged=%s\n' "$UNPRIVILEGED"
	printf 'debian=%s\n' "${DEBIAN_SERIES:-$DEFAULT_DEBIAN}"
	printf 'timezone=%s\n' "${TIMEZONE:-auto}"
	printf 'assume_yes=%s\n' "$ASSUME_YES"
	printf 'dry_run=%s\n' "$DRY_RUN"
	printf 'force_new=%s\n' "$FORCE_NEW"
	printf 'force_update=%s\n' "$FORCE_UPDATE"
	printf 'keep_on_failure=%s\n' "$KEEP_ON_FAILURE"
	printf 'image=%s\n' "$SENTINEL_IMAGE"
	printf 'port=%s\n' "$SENTINEL_PORT"
}

# --- Preflight ----------------------------------------------------------------

preflight() {
	step "Checking the host"

	[ "$(id -u)" -eq 0 ] || die "This must run as root on the Proxmox host: sudo bash install-proxmox.sh"

	if ! command -v pveversion >/dev/null 2>&1; then
		error "This does not look like a Proxmox VE host -- pveversion was not found."
		printf '\n' >&2
		printf 'Sentinel runs anywhere Docker does. On a plain Linux box:\n' >&2
		printf '\n' >&2
		printf '    git clone https://github.com/jencarnacao19/sentinel-cctv.git\n' >&2
		printf '    cd sentinel-cctv && docker compose up -d\n' >&2
		printf '\n' >&2
		printf 'This installer is only for creating an LXC on Proxmox VE.\n' >&2
		exit 1
	fi

	local missing=() cmd
	for cmd in pct pveam pvesm awk sed grep curl; do
		command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
	done
	if [ "${#missing[@]}" -gt 0 ]; then
		die "Missing required commands: ${missing[*]}"
	fi

	local version major
	version="$(pveversion 2>/dev/null | head -n1)"
	info "Host: $version"
	major="$(pve_major_version "$version")"
	if [ -n "$major" ] && [ "$major" -lt 8 ]; then
		warn "This installer uses Proxmox VE 8/9 syntax; you are on major version $major. It may not work."
		confirm "Continue anyway?" || exit 0
	fi
}

# --- Resolution and prompts ---------------------------------------------------

# Every active storage that accepts the given content type.
list_storages() {
	pvesm status --content "$1" 2>/dev/null | awk 'NR > 1 && $3 == "active" { print $1 }'
}

storage_accepts() {
	list_storages "$1" | grep -qx -- "$2"
}

pick_storage() {
	local content="$1" preferred="$2" candidates candidate
	candidates="$(list_storages "$content")"
	[ -n "$candidates" ] || return 1
	for candidate in $preferred; do
		if printf '%s\n' "$candidates" | grep -qx -- "$candidate"; then
			printf '%s' "$candidate"
			return 0
		fi
	done
	printf '%s\n' "$candidates" | head -n1
}

# Refuses to proceed if $1 does not have room for a $2 GB root disk. This is
# the one preflight check that talks in the concrete numbers the operator
# needs to fix the problem, rather than leaving `pct create` to fail deep
# inside creation with whatever raw error the storage layer gives it.
check_storage_space() {
	local storage="$1" disk_gb="$2" avail needed
	needed=$((disk_gb * 1073741824))
	avail="$(storage_avail_bytes "$storage")"
	case "$avail" in
	'' | *[!0-9]*)
		warn "Could not read free space on storage '$storage'; continuing without that check."
		return 0
		;;
	esac
	if [ "$avail" -lt "$needed" ]; then
		die "Not enough space on storage '$storage': --disk $disk_gb needs ${disk_gb} GiB, only $(bytes_to_gib "$avail") GiB free."
	fi
	info "Free on $storage: $(bytes_to_gib "$avail") GiB (root disk needs ${disk_gb} GiB)."
}

ctid_in_use() {
	local id="$1"
	# Config files first: they are authoritative for this node and cost nothing.
	[ -e "/etc/pve/lxc/${id}.conf" ] && return 0
	[ -e "/etc/pve/qemu-server/${id}.conf" ] && return 0
	pct list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -qx -- "$id" && return 0
	if command -v qm >/dev/null 2>&1; then
		qm list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -qx -- "$id" && return 0
	fi
	# Cluster-wide check. Only an explicit "already exists" counts, so a broken
	# or unavailable API does not make every ID look taken.
	if command -v pvesh >/dev/null 2>&1; then
		local out
		out="$(pvesh get /cluster/nextid --vmid "$id" 2>&1 || true)"
		case "$out" in
		*"already exists"*) return 0 ;;
		esac
	fi
	return 1
}

next_free_ctid() {
	local candidate
	if command -v pvesh >/dev/null 2>&1; then
		candidate="$(pvesh get /cluster/nextid 2>/dev/null | tr -dc '0-9' || true)"
		if [ -n "$candidate" ]; then
			printf '%s' "$candidate"
			return 0
		fi
	fi
	for candidate in $(seq 100 999); do
		if ! ctid_in_use "$candidate"; then
			printf '%s' "$candidate"
			return 0
		fi
	done
	return 1
}

host_timezone() {
	local tz=""
	if command -v timedatectl >/dev/null 2>&1; then
		tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
	fi
	if [ -z "$tz" ] && [ -r /etc/timezone ]; then
		tz="$(head -n1 /etc/timezone 2>/dev/null || true)"
	fi
	printf '%s' "${tz:-UTC}"
}

resolve_settings() {
	step "Container settings"

	local default_ctid
	if [ -z "$CTID" ]; then
		default_ctid="$(next_free_ctid)" || die "Could not find a free container ID. Pass one with --ctid."
		ask CTID "Container ID" "$default_ctid"
	fi
	validate_int ctid "$CTID" 100 999999999
	CTID="$OPT_VALUE"
	# The one refusal that matters most: an existing container is somebody's
	# working system, and this script has no business writing over it.
	if ctid_in_use "$CTID"; then
		error "Container ID $CTID is already in use."
		info "Nothing has been changed. Pick a free ID with --ctid, or remove that container yourself first."
		exit 1
	fi

	[ -n "$CT_HOSTNAME" ] || ask CT_HOSTNAME "Hostname" "$DEFAULT_HOSTNAME"
	validate_hostname "$CT_HOSTNAME"

	[ -n "$CORES" ] || ask CORES "CPU cores" "$DEFAULT_CORES"
	validate_int cores "$CORES" 1 128
	CORES="$OPT_VALUE"

	[ -n "$MEMORY" ] || ask MEMORY "RAM in MB" "$DEFAULT_MEMORY"
	validate_int memory "$MEMORY" 512 262144
	MEMORY="$OPT_VALUE"

	[ -n "$DISK" ] || ask DISK "Root disk in GB (recordings go elsewhere)" "$DEFAULT_DISK"
	validate_int disk "$DISK" 8 8192
	DISK="$OPT_VALUE"

	validate_int swap "$SWAP" 0 65536
	SWAP="$OPT_VALUE"

	local default_storage
	if [ -z "$STORAGE" ]; then
		default_storage="$(pick_storage rootdir "local-lvm local-zfs local-btrfs local")" ||
			die "No active storage accepts container volumes. Enable 'Container' content on a storage in Datacenter > Storage."
		info "Storages that can hold the container: $(list_storages rootdir | tr '\n' ' ')"
		ask STORAGE "Storage for the container disk" "$default_storage"
	fi
	validate_name storage "$STORAGE"
	storage_accepts rootdir "$STORAGE" ||
		die "Storage '$STORAGE' is not active or does not accept container volumes. Available: $(list_storages rootdir | tr '\n' ' ')"
	check_storage_space "$STORAGE" "$DISK"

	local default_template_storage
	if [ -z "$TEMPLATE_STORAGE" ]; then
		default_template_storage="$(pick_storage vztmpl "local")" ||
			die "No active storage accepts container templates. Enable 'Container template' content on a storage."
		TEMPLATE_STORAGE="$default_template_storage"
	fi
	validate_name template-storage "$TEMPLATE_STORAGE"
	storage_accepts vztmpl "$TEMPLATE_STORAGE" ||
		die "Storage '$TEMPLATE_STORAGE' is not active or does not accept templates. Available: $(list_storages vztmpl | tr '\n' ' ')"

	[ -n "$BRIDGE" ] || ask BRIDGE "Network bridge" "$DEFAULT_BRIDGE"
	validate_name bridge "$BRIDGE"
	if ! ip link show "$BRIDGE" >/dev/null 2>&1; then
		warn "Bridge '$BRIDGE' does not exist on this host. The container would have no network."
		confirm "Use it anyway?" || die "Pick an existing bridge with --bridge. Available: $(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | tr '\n' ' ')"
	fi

	[ -n "$DEBIAN_SERIES" ] || DEBIAN_SERIES="$DEFAULT_DEBIAN"
	case "$DEBIAN_SERIES" in
	13 | 12) ;;
	*) die "--debian must be 13 or 12, got '$DEBIAN_SERIES'." ;;
	esac

	[ -n "$TIMEZONE" ] || TIMEZONE="$(host_timezone)"
	validate_timezone "$TIMEZONE"
}

# --- Recordings storage --------------------------------------------------------

# Prepares --recordings-path on the *host* side, before the container exists to
# bind-mount it into. A no-op when the flag was not given at all -- recordings
# then simply live on the container's own root disk until the operator attaches
# something, exactly as --help's AFTERWARDS section describes.
#
# This is the single check the product owner singled out as the most common
# way this kind of script fails: an unprivileged container's uid 0 is not the
# host's uid 0, so a bind-mounted directory owned by host root is invisible to
# the app running as container uid 1000 unless the *host-side* directory is
# owned by the mapped uid (1000 + the subuid offset) before the container ever
# starts. Getting the shift backwards, or skipping it, produces a container
# that starts cleanly and simply cannot write a single recording -- one of the
# quietest failure modes there is, which is why it gets its own step and its
# own explicit chown rather than being folded into create_container().
#
# Deliberately read-only: it validates and reports, but creates and chowns
# nothing yet. That side effect is prepare_recordings_directory(), run only
# after summary_and_confirm() -- so declining at the confirmation prompt
# really does leave the host exactly as it was, which "Nothing was changed."
# would otherwise be lying about if this step had already written to disk.
resolve_recordings_storage() {
	[ -n "$RECORDINGS_PATH" ] || return 0
	step "Recordings storage"

	validate_recordings_path "$RECORDINGS_PATH"

	local owner
	if owner="$(path_already_mounted "$RECORDINGS_PATH")"; then
		die "$RECORDINGS_PATH is already bind-mounted into container $owner. Two containers writing into the same directory will corrupt each other's recordings -- pick a different --recordings-path, or remove that mountpoint from container $owner first (pct config $owner)."
	fi

	if [ -e "$RECORDINGS_PATH" ] && [ ! -d "$RECORDINGS_PATH" ]; then
		die "$RECORDINGS_PATH exists and is not a directory."
	fi

	local host_uid
	host_uid="$(mapped_host_uid "$APP_UID")"
	if [ ! -e "$RECORDINGS_PATH" ]; then
		info "Will create $RECORDINGS_PATH, owned by uid $host_uid (Sentinel's uid $APP_UID, mapped for an unprivileged container)."
	elif [ -z "$(ls -A "$RECORDINGS_PATH" 2>/dev/null)" ]; then
		info "$RECORDINGS_PATH exists and is empty; will set ownership to uid $host_uid."
	else
		# Whatever put files here already decided who owns them; overwriting
		# that in bulk is exactly the kind of surprise this script must not
		# spring on an operator. Say what to do instead of guessing.
		warn "$RECORDINGS_PATH already has content; its ownership will be left untouched."
		info "If Sentinel cannot write to it, the container's mapped uid is $host_uid:"
		info "  chown -R ${host_uid}:${host_uid} $RECORDINGS_PATH"
	fi

	local avail
	if avail="$(df_avail_bytes "$RECORDINGS_PATH")"; then
		info "Free space at $RECORDINGS_PATH: $(bytes_to_gib "$avail") GiB."
		if [ "$avail" -lt $((5 * 1073741824)) ]; then
			warn "Under 5 GiB free at $RECORDINGS_PATH. Continuous recording will fill that fast."
		fi
	fi
}

# The side-effecting half of the above: actually creates and/or chowns
# $RECORDINGS_PATH on the host. Called from create_container(), after the
# container itself exists, so a failure here rolls back through the same
# CREATED_CTID guard as everything else create_container() does -- and, if
# this step is what created the directory, rollback() also removes it again
# provided it is still empty (see RECORDINGS_PATH_CREATED).
prepare_recordings_directory() {
	[ -n "$RECORDINGS_PATH" ] || return 0

	local host_uid
	host_uid="$(mapped_host_uid "$APP_UID")"

	if [ ! -e "$RECORDINGS_PATH" ]; then
		run install -d -m 0755 -o "$host_uid" -g "$host_uid" "$RECORDINGS_PATH" ||
			die "Could not create $RECORDINGS_PATH. Check that its parent directory exists and this host has permission to create it."
		RECORDINGS_PATH_CREATED=1
	elif [ -z "$(ls -A "$RECORDINGS_PATH" 2>/dev/null)" ]; then
		run chown "${host_uid}:${host_uid}" "$RECORDINGS_PATH" ||
			die "Could not chown $RECORDINGS_PATH to ${host_uid}:${host_uid}."
	fi
}

# --- Template -----------------------------------------------------------------

# Newest matching template name from `pveam available`, empty if there is none.
latest_available_template() {
	local series="$1" arch="$2"
	pveam available --section system 2>/dev/null |
		awk '{ print $2 }' |
		grep -E "^debian-${series}-standard_.*_${arch}\.tar\." |
		sort -V |
		tail -n1
}

template_present() {
	pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '{ print $1 }' | grep -qx -- "${TEMPLATE_STORAGE}:vztmpl/$1"
}

resolve_template() {
	step "Debian template"

	local arch series
	arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

	info "Refreshing the template catalogue"
	if ! run pveam update >/dev/null 2>&1; then
		warn "pveam update failed. Working from the catalogue already on disk."
	fi

	for series in "$DEBIAN_SERIES" "$FALLBACK_DEBIAN"; do
		TEMPLATE_NAME="$(latest_available_template "$series" "$arch")"
		if [ -n "$TEMPLATE_NAME" ]; then
			if [ "$series" != "$DEBIAN_SERIES" ]; then
				warn "No Debian $DEBIAN_SERIES template is available for $arch; falling back to Debian $series."
				DEBIAN_SERIES="$series"
			fi
			break
		fi
	done

	if [ -z "$TEMPLATE_NAME" ]; then
		# Already-downloaded templates still work even when the catalogue does not.
		TEMPLATE_NAME="$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null |
			awk '{ print $1 }' |
			sed -n "s#^${TEMPLATE_STORAGE}:vztmpl/\(debian-1[23]-standard_.*\)#\1#p" |
			sort -V | tail -n1)"
		[ -n "$TEMPLATE_NAME" ] ||
			die "No Debian 13 or 12 template found for $arch. Check the host's internet access, or download one in Datacenter > Storage > CT Templates."
		warn "Using the template already on $TEMPLATE_STORAGE: $TEMPLATE_NAME"
	fi

	TEMPLATE_VOLID="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE_NAME}"

	if template_present "$TEMPLATE_NAME"; then
		info "Already downloaded: $TEMPLATE_NAME"
	else
		info "Downloading $TEMPLATE_NAME to $TEMPLATE_STORAGE (a few hundred MB)"
		run pveam download "$TEMPLATE_STORAGE" "$TEMPLATE_NAME" ||
			die "Template download failed. Check the host's internet access and free space on $TEMPLATE_STORAGE."
	fi
}

# --- Summary ------------------------------------------------------------------

summary_and_confirm() {
	local privileged_label="unprivileged"
	[ "$UNPRIVILEGED" = "yes" ] || privileged_label="privileged"

	step "About to create container $CTID"
	printf '\n'
	printf '      Container ID    %s\n' "$CTID"
	printf '      Hostname        %s\n' "$CT_HOSTNAME"
	printf '      Mode            %s, nesting enabled (Docker runs inside)\n' "$privileged_label"
	printf '      Template        %s\n' "$TEMPLATE_NAME"
	printf '      Cores / RAM     %s cores, %s MB (+%s MB swap)\n' "$CORES" "$MEMORY" "$SWAP"
	printf '      Root disk       %s GB on %s\n' "$DISK" "$STORAGE"
	printf '      Network         %s, DHCP\n' "$BRIDGE"
	printf '      Timezone        %s\n' "$TIMEZONE"
	printf '      Sentinel        %s on port %s\n' "$SENTINEL_IMAGE" "$SENTINEL_PORT"
	printf '\n'
	note "A new disk is allocated for the container. No existing disk, filesystem,"
	note "container or file is written to, formatted or deleted."
	printf '\n'

	if ! confirm "Create it?"; then
		info "Nothing was changed."
		exit 0
	fi
}

# --- Update path ----------------------------------------------------------------
#
# What makes re-running this script safe rather than merely harmless: a second
# run finds the container the first run built and offers to update it, instead
# of either refusing outright or (the actually dangerous failure mode) walking
# straight into "container ID already in use" and stopping one step short of
# ever telling the operator that container was this installer's own.

# Decides whether this run updates a container built by an earlier run of this
# script or creates a fresh one, and sets UPDATE_TARGET (empty means fresh).
#
# An explicit --ctid is trusted first and exclusively: if it names a container
# that already carries our marker, that is what gets updated. Only when --ctid
# is not given does this fall back to scanning the node for a marked
# container, because guessing which of several existing installs to touch
# would be worse than asking.
detect_existing_install() {
	UPDATE_TARGET=""
	[ "$FORCE_NEW" -eq 1 ] && return 0

	if [ -n "$CTID" ]; then
		if [ -e "/etc/pve/lxc/${CTID}.conf" ] && grep -q -F -- "$MARKER" "/etc/pve/lxc/${CTID}.conf" 2>/dev/null; then
			UPDATE_TARGET="$CTID"
		elif [ "$FORCE_UPDATE" -eq 1 ]; then
			die "Container $CTID does not carry this installer's marker, so --update refuses to touch it. Use --new to create a fresh container there instead, or drop --ctid to let the installer find its own install."
		fi
		return 0
	fi

	local found count
	found="$(list_marked_containers)"
	if [ -z "$found" ]; then
		[ "$FORCE_UPDATE" -eq 0 ] ||
			die "No existing Sentinel install found on this node (looked for the '$MARKER' marker in /etc/pve/lxc). Drop --update to create one, or pass --ctid if it lives on a different node."
		return 0
	fi

	count="$(printf '%s\n' "$found" | wc -l)"
	if [ "$count" -gt 1 ]; then
		die "Found more than one Sentinel install on this node: $(printf '%s' "$found" | tr '\n' ' '). Pick one with --ctid."
	fi

	if [ "$FORCE_UPDATE" -eq 1 ]; then
		UPDATE_TARGET="$found"
		return 0
	fi

	step "Existing install found"
	info "Container $found already runs Sentinel; it was created by this installer."
	if confirm "Update it instead of creating a new container?"; then
		UPDATE_TARGET="$found"
	else
		info "Continuing as a fresh install. Pick a different --ctid if $found should stay untouched."
	fi
}

# Pulls the latest image and recreates the Sentinel service inside a container
# this script (or an earlier run of it) already built. Deliberately never
# rewrites compose.yml -- anything edited there by hand survives an update,
# exactly as --help's UPDATING section promises -- so this only ever drives
# docker compose already running inside the container, never the container's
# own configuration or its disks.
update_existing_container() {
	CTID="$UPDATE_TARGET"
	step "Updating Sentinel in container $CTID"

	CT_HOSTNAME="$(pct config "$CTID" 2>/dev/null | sed -n 's/^hostname: //p')"
	[ -n "$CT_HOSTNAME" ] || CT_HOSTNAME="(unknown)"

	local status
	status="$(pct status "$CTID" 2>/dev/null | sed -n 's/^status: //p')"
	if [ "$status" != "running" ]; then
		info "Container is ${status:-unreachable}; starting it."
		run pct start "$CTID" || die "Could not start container $CTID."
	fi

	wait_for_network

	info "Pulling the latest image and recreating the Sentinel service"
	note "compose.yml on disk is left exactly as it is; only the image is refreshed."
	run pct exec "$CTID" -- sh -c "cd '$INSTALL_DIR' && docker compose pull && docker compose up -d" ||
		die "Update failed inside container $CTID. Inspect it with: pct enter $CTID"

	wait_for_sentinel
	final_message_updated
}

final_message_updated() {
	printf '\n%s%sSentinel was updated.%s\n\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
	printf '      Open        %shttp://%s:%s%s\n' "$C_BOLD" "$CT_IP" "$SENTINEL_PORT" "$C_RESET"
	printf '      Container   %s (%s)\n' "$CTID" "$CT_HOSTNAME"
	printf '      Compose     %s/compose.yml (inside the container, left untouched)\n' "$INSTALL_DIR"

	printf '\n%sHandy%s\n' "$C_BOLD" "$C_RESET"
	printf '  Shell in the container    pct enter %s\n' "$CTID"
	printf '  Logs                      pct exec %s -- docker compose -f %s/compose.yml logs -f\n' "$CTID" "$INSTALL_DIR"
	printf '  Run again to update       bash install-proxmox.sh --yes --ctid %s\n' "$CTID"

	printf '\n%sDocs%s\n' "$C_BOLD" "$C_RESET"
	printf '  Proxmox   %s\n\n' "$DOCS_PROXMOX"
}

# --- Creation -----------------------------------------------------------------

create_container() {
	step "Creating the container"

	local features="nesting=1"
	local unpriv_flag=1
	if [ "$UNPRIVILEGED" != "yes" ]; then
		unpriv_flag=0
	else
		# keyctl is what lets Docker's containerd start inside an unprivileged
		# container; without it the daemon fails on kernel keyring access.
		features="nesting=1,keyctl=1"
	fi

	local args=(
		"$CTID" "$TEMPLATE_VOLID"
		--hostname "$CT_HOSTNAME"
		--cores "$CORES"
		--memory "$MEMORY"
		--swap "$SWAP"
		--rootfs "${STORAGE}:${DISK}"
		--unprivileged "$unpriv_flag"
		--features "$features"
		--net0 "name=eth0,bridge=${BRIDGE},ip=dhcp"
		--ostype debian
		--onboot 1
		--timezone "$TIMEZONE"
		--description "Sentinel CCTV -- created by ${MARKER}. Web interface on port ${SENTINEL_PORT}."
	)

	run pct create "${args[@]}" || die "pct create failed. Nothing was left behind: no container with ID $CTID exists."
	if [ "$DRY_RUN" -eq 0 ]; then
		# From here on a failure has something to roll back, and only this.
		CREATED_CTID="$CTID"
	fi
	info "Container $CTID created."

	# Attached with the container stopped, the same way --help's AFTERWARDS
	# section tells an operator to attach storage by hand -- there is no
	# special create-time form of this that behaves any differently, and doing
	# it as a separate step means a failure here still rolls back cleanly
	# through the CREATED_CTID marker set just above.
	if [ -n "$RECORDINGS_PATH" ]; then
		prepare_recordings_directory
		info "Bind-mounting $RECORDINGS_PATH at $RECORDINGS_DIR"
		run pct set "$CTID" -mp0 "${RECORDINGS_PATH},mp=${RECORDINGS_DIR}" ||
			die "Could not attach $RECORDINGS_PATH to the container. The container itself was created; see rollback above."
	fi
}

start_container() {
	step "Starting the container"
	run pct start "$CTID" || die "The container was created but would not start."
	wait_for_network
}

# Blocks until the container has a DHCP lease and can resolve names, or dies
# with something actionable. Shared by a fresh create (start_container) and an
# update of an existing container (update_existing_container) -- an update
# needs working DNS just as much, to pull the image again.
wait_for_network() {
	if [ "$DRY_RUN" -eq 1 ]; then
		note "(dry run: skipping the wait for network)"
		CT_IP="<container-ip>"
		return 0
	fi

	local attempt
	info "Waiting for an IP address from DHCP"
	for attempt in $(seq 1 60); do
		CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | tr ' ' '\n' | grep -m1 -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
		[ -n "$CT_IP" ] && break
		sleep 2
	done
	[ -n "$CT_IP" ] || die "The container never got an IPv4 address on $BRIDGE. Check that the bridge reaches a DHCP server."
	info "Address: $CT_IP"

	info "Waiting for DNS"
	for attempt in $(seq 1 30); do
		if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
			return 0
		fi
		sleep 2
	done
	die "The container has an address but cannot resolve names. Check DNS on $BRIDGE."
}

# --- Provisioning -------------------------------------------------------------

write_compose_file() {
	local path="$1"
	# Leave the container room to breathe: the cap is for Sentinel, not for the
	# whole container.
	local mem_limit
	mem_limit="$(compose_memory_limit_mb "$MEMORY")"

	cat >"$path" <<COMPOSE
# Sentinel CCTV.
#
# Written by install-proxmox.sh. Safe to edit; re-apply with:
#
#   cd ${INSTALL_DIR} && docker compose up -d

services:
  sentinel:
    image: ${SENTINEL_IMAGE}
    container_name: sentinel
    restart: unless-stopped

    # Required for ONVIF WS-Discovery multicast to use the LXC's real LAN
    # interface rather than Docker's private bridge.
    network_mode: host

    volumes:
      # Database, thumbnails, snapshots and the local encryption secret.
      - sentinel-data:/data
      # Recordings. ${RECORDINGS_DIR} is on the container's root disk until you
      # attach real storage there with 'pct set ${CTID} -mp0 ...'; see docs/proxmox.md.
      - ${RECORDINGS_DIR}:${RECORDINGS_DIR}

    environment:
      TZ: ${TIMEZONE}
      SENTINEL_TIMEZONE: ${TIMEZONE}
      SENTINEL_LOG_LEVEL: INFO
      SENTINEL_VIEWER_ENABLED: \${SENTINEL_VIEWER_ENABLED:-false}
      SENTINEL_VIEWER_TRUSTED_HOSTS: \${SENTINEL_VIEWER_TRUSTED_HOSTS:-}
      SENTINEL_VIEWER_KEY: \${SENTINEL_VIEWER_KEY:-}
      SENTINEL_FRAME_ANCESTORS: \${SENTINEL_FRAME_ANCESTORS:-}
      SENTINEL_COOKIE_SECURE: \${SENTINEL_COOKIE_SECURE:-false}

    # FFmpeg opens a handful of file descriptors per camera; the default soft
    # limit of 1024 is tight once you pass roughly twenty streams.
    ulimits:
      nofile:
        soft: 8192
        hard: 8192

    deploy:
      resources:
        limits:
          memory: ${mem_limit}m

    healthcheck:
      test: ['CMD', 'curl', '-fsS', 'http://127.0.0.1:${SENTINEL_PORT}/api/v1/system/health']
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s

volumes:
  sentinel-data:
COMPOSE
}

write_provision_script() {
	local path="$1"
	{
		printf '%s\n' '#!/usr/bin/env bash'
		printf '%s\n' '# Generated by install-proxmox.sh. Runs once, inside the container.'
		printf 'INSTALL_DIR=%q\n' "$INSTALL_DIR"
		printf 'RECORDINGS_DIR=%q\n' "$RECORDINGS_DIR"
		printf 'APP_UID=%q\n' "$APP_UID"
		cat <<'PROVISION'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

say() { printf '    %s\n' "$*"; }

# A container that has just won the DHCP race can still lose DNS for a second or
# two, and a mirror can be briefly unhappy. Three tries covers both.
apt_retry() {
	local attempt
	for attempt in 1 2 3; do
		if apt-get -o Acquire::Retries=3 "$@"; then
			return 0
		fi
		say "apt-get $1 failed (attempt ${attempt}/3); retrying in 5s"
		sleep 5
	done
	return 1
}

. /etc/os-release
codename="${VERSION_CODENAME:-}"
arch="$(dpkg --print-architecture)"
say "Debian ${VERSION_ID:-?} (${codename}), ${arch}"

apt_retry update
apt_retry install -y --no-install-recommends ca-certificates curl gnupg

install_from_docker_repo() {
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' \
		"$arch" "$codename" >/etc/apt/sources.list.d/docker.list
	apt_retry update
	apt_retry install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_from_debian_repo() {
	if apt-cache policy docker-compose-v2 2>/dev/null | grep -q 'Candidate: (none)'; then
		echo "Debian ${codename} has neither Docker's repository nor docker-compose-v2." >&2
		echo "Install Docker by hand, then run: cd ${INSTALL_DIR} && docker compose up -d" >&2
		exit 1
	fi
	apt_retry install -y docker.io docker-compose-v2
}

# Docker publishes for a new Debian release some weeks after it ships. Rather
# than guessing, ask: if there is no Release file for this codename, use the
# packages Debian itself provides.
if curl -fsS -o /dev/null "https://download.docker.com/linux/debian/dists/${codename}/Release"; then
	say "Installing Docker from download.docker.com"
	install_from_docker_repo
else
	say "Docker publishes no packages for Debian '${codename}' yet; using Debian's own."
	install_from_debian_repo
fi

systemctl enable --now docker
docker --version
docker compose version

driver="$(docker info --format '{{.Driver}}' 2>/dev/null || echo unknown)"
say "Docker storage driver: ${driver}"
if [ "$driver" = "vfs" ]; then
	echo "warning: Docker fell back to the vfs storage driver, which copies whole" >&2
	echo "         layers and is slow. See docs/proxmox.md, 'Docker storage driver'." >&2
fi

install -d -m 0755 "$INSTALL_DIR"
install -m 0644 /root/sentinel-compose.yml "$INSTALL_DIR/compose.yml"

# Recordings live outside the Docker volume so that they outlive the container
# and so a Proxmox mountpoint can be attached over this path later.
if [ ! -d "$RECORDINGS_DIR" ]; then
	install -d -m 0755 -o "$APP_UID" -g "$APP_UID" "$RECORDINGS_DIR"
elif [ -z "$(ls -A "$RECORDINGS_DIR" 2>/dev/null)" ]; then
	chown "${APP_UID}:${APP_UID}" "$RECORDINGS_DIR"
else
	# Something is already mounted or stored here. Whose it is and who should own
	# it is the operator's call, not this script's.
	say "${RECORDINGS_DIR} is not empty; leaving its contents and ownership untouched."
fi

cd "$INSTALL_DIR"
say "Pulling ${INSTALL_DIR}/compose.yml images"
for attempt in 1 2 3; do
	if docker compose pull; then
		break
	fi
	if [ "$attempt" -eq 3 ]; then
		echo "Could not pull the Sentinel image after 3 attempts." >&2
		exit 1
	fi
	say "Pull failed (attempt ${attempt}/3); retrying in 5s"
	sleep 5
done

docker compose up -d
docker compose ps
PROVISION
	} >"$path"
}

provision_container() {
	step "Installing Docker and Sentinel inside the container"

	WORK_DIR="$(mktemp -d)"
	PROVISION_FILE="$WORK_DIR/sentinel-provision.sh"
	COMPOSE_FILE="$WORK_DIR/sentinel-compose.yml"
	write_provision_script "$PROVISION_FILE"
	write_compose_file "$COMPOSE_FILE"

	if [ "$DRY_RUN" -eq 1 ]; then
		note "(dry run: the compose file that would be written to ${INSTALL_DIR}/compose.yml)"
		sed 's/^/      /' "$COMPOSE_FILE"
		return 0
	fi

	run pct push "$CTID" "$COMPOSE_FILE" /root/sentinel-compose.yml --perms 0644
	run pct push "$CTID" "$PROVISION_FILE" /root/sentinel-provision.sh --perms 0700
	run pct exec "$CTID" -- bash /root/sentinel-provision.sh ||
		die "Provisioning failed inside the container. Look with: pct enter $CTID"
}

wait_for_sentinel() {
	step "Waiting for Sentinel to answer"

	if [ "$DRY_RUN" -eq 1 ]; then
		note "(dry run: skipping the health check)"
		return 0
	fi

	local attempt
	for attempt in $(seq 1 40); do
		if curl -fsS -m 5 "http://${CT_IP}:${SENTINEL_PORT}/api/v1/system/health" >/dev/null 2>&1; then
			info "Healthy after $((attempt * 3))s or less."
			return 0
		fi
		sleep 3
	done

	warn "Sentinel did not answer on http://${CT_IP}:${SENTINEL_PORT} within two minutes."
	warn "The container is up; the application may still be starting. Check with:"
	warn "  pct exec $CTID -- docker compose -f ${INSTALL_DIR}/compose.yml logs --tail 50"
}

# --- Result -------------------------------------------------------------------

final_message() {
	local host_uid owner_hint
	host_uid="$(mapped_host_uid "$APP_UID")"
	if [ "$UNPRIVILEGED" = "yes" ]; then
		owner_hint="${host_uid}:${host_uid}   # unprivileged: container uid ${APP_UID} maps to host uid ${host_uid}"
	else
		owner_hint="${APP_UID}:${APP_UID}       # privileged: uids are not remapped"
	fi

	printf '\n%s%sSentinel is installed.%s\n\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
	printf '      Open        %shttp://%s:%s%s\n' "$C_BOLD" "$CT_IP" "$SENTINEL_PORT" "$C_RESET"
	printf '      Container   %s (%s)\n' "$CTID" "$CT_HOSTNAME"
	printf '      Compose     %s/compose.yml (inside the container)\n' "$INSTALL_DIR"
	if [ -n "$RECORDINGS_PATH" ]; then
		printf '      Recordings  %s -> bind-mounted from %s on the host\n' "$RECORDINGS_DIR" "$RECORDINGS_PATH"
	else
		printf '      Recordings  %s (on the %s GB container disk for now)\n' "$RECORDINGS_DIR" "$DISK"
	fi

	printf '\n%sNext%s\n' "$C_BOLD" "$C_RESET"
	printf '  1. Open the URL and create your administrator account.\n'
	if [ -n "$RECORDINGS_PATH" ]; then
		printf '  2. Add your cameras by RTSP URL.\n'
	else
		printf '  2. Attach real storage for recordings. This only mounts a path you already\n'
		printf '     have -- nothing is formatted and nothing is deleted:\n\n'
		printf '       pct stop %s\n' "$CTID"
		printf '       pct set %s -mp0 /your/existing/path,mp=%s\n' "$CTID" "$RECORDINGS_DIR"
		printf '       chown -R %s\n' "$owner_hint"
		printf '       pct start %s\n' "$CTID"
		printf '\n     Then point Sentinel at %s in Settings > Storage.\n' "$RECORDINGS_DIR"
		printf '  3. Add your cameras by RTSP URL.\n'
	fi

	printf '\n%sHandy%s\n' "$C_BOLD" "$C_RESET"
	printf '  Shell in the container    pct enter %s\n' "$CTID"
	printf '  Logs                      pct exec %s -- docker compose -f %s/compose.yml logs -f\n' "$CTID" "$INSTALL_DIR"
	printf '  Update                    re-run this script with --yes --ctid %s, or:\n' "$CTID"
	printf '                            pct exec %s -- sh -c "cd %s && docker compose pull && docker compose up -d"\n' "$CTID" "$INSTALL_DIR"
	printf '  Remove (destroys the container and its disk)\n'
	printf '                            pct stop %s && pct destroy %s\n' "$CTID" "$CTID"

	printf '\n%sDocs%s\n' "$C_BOLD" "$C_RESET"
	printf '  Proxmox     %s\n' "$DOCS_PROXMOX"
	printf '  Recording   %s\n\n' "$DOCS_RECORDING"
}

# --- Failure handling ---------------------------------------------------------

cleanup_temp() {
	[ -n "$WORK_DIR" ] || return 0
	rm -f "$PROVISION_FILE" "$COMPOSE_FILE" 2>/dev/null || true
	rmdir "$WORK_DIR" 2>/dev/null || true
	WORK_DIR=""
}

# Removes the container this run created, and nothing else. Guarded three ways:
# the ID must have been set by create_container, the container must still carry
# our marker, and the user must agree unless --yes was given.
rollback() {
	[ -n "$CREATED_CTID" ] || return 0
	[ "$KEEP_ON_FAILURE" -eq 0 ] || {
		info "Leaving container $CREATED_CTID in place (--keep-on-failure)."
		return 0
	}

	# Only ever removes a directory this exact run created (RECORDINGS_PATH_CREATED),
	# and only while it is still empty -- if Sentinel got far enough to write a
	# single recording into it before the failure, that recording is no longer
	# this run's to delete, only the operator's.
	if [ "$RECORDINGS_PATH_CREATED" -eq 1 ] && [ -d "$RECORDINGS_PATH" ] && [ -z "$(ls -A "$RECORDINGS_PATH" 2>/dev/null)" ]; then
		info "Removing $RECORDINGS_PATH (empty; created moments ago by this run)."
		rmdir "$RECORDINGS_PATH" 2>/dev/null || true
		RECORDINGS_PATH_CREATED=0
	fi

	if ! pct config "$CREATED_CTID" 2>/dev/null | grep -q "$MARKER"; then
		warn "Container $CREATED_CTID does not carry this installer's marker; leaving it alone."
		return 0
	fi

	printf '\n'
	warn "The install failed after container $CREATED_CTID was created."
	if ! confirm "Remove container $CREATED_CTID and its disk? (it was created moments ago by this script)"; then
		info "Left in place. Inspect it with: pct enter $CREATED_CTID"
		info "Remove it later with: pct stop $CREATED_CTID && pct destroy $CREATED_CTID"
		return 0
	fi

	run pct stop "$CREATED_CTID" || true
	run pct destroy "$CREATED_CTID" || warn "Could not remove container $CREATED_CTID; remove it by hand."
	CREATED_CTID=""
}

on_error() {
	error "Failed at line $1."
}

on_exit() {
	local status=$?
	trap - EXIT
	cleanup_temp
	if [ "$status" -ne 0 ]; then
		rollback || true
	fi
	exit "$status"
}

# --- Entry point --------------------------------------------------------------

main() {
	init_colors
	parse_args "$@"
	init_colors

	if [ "$PRINT_CONFIG" -eq 1 ]; then
		print_config
		exit 0
	fi

	trap 'on_error $LINENO' ERR
	trap on_exit EXIT
	trap 'printf "\n"; error "Interrupted."; exit 130' INT TERM

	printf '%s%sSentinel CCTV -- Proxmox installer%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
	note "Never formats a disk, never creates a filesystem, never deletes existing data,"
	note "and never touches a container ID it did not create itself."
	[ "$DRY_RUN" -eq 1 ] && warn "Dry run: commands are printed, nothing is executed."

	preflight
	detect_existing_install
	if [ -n "$UPDATE_TARGET" ]; then
		update_existing_container
		return 0
	fi

	resolve_settings
	resolve_recordings_storage
	resolve_template
	summary_and_confirm
	create_container
	start_container
	provision_container
	wait_for_sentinel
	final_message
}

# Runs main only when this file is actually being executed, not when it is
# sourced -- which is how tools/test-install-proxmox.sh gets at the pure
# helpers above without also triggering a real install. `${BASH_SOURCE[0]:-$0}`
# is deliberate, not `$0` alone: under `bash -c "$(curl ...)" -- --yes`, the
# script has no source file at all, so BASH_SOURCE is empty and this falls
# back to comparing "$0" with itself -- true, so main runs, exactly as it must
# for the one-line install command that is this project's whole pitch.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
	main "$@"
fi
