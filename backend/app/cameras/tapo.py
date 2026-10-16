"""Vendor capability layer for Tapo cameras, spoken entirely over the LAN.

pytapo is a synchronous, single-vendor client that raises bare ``Exception``
for everything from "wrong password" to "this model has no siren", and whose
messages embed the raw device response. Letting that reach the API or a log
would leak vendor payloads and make every caller re-implement the same guess
work, so this module is the only place in Sentinel that touches it: it owns one
cached session per camera, runs every call in a worker thread under a per-camera
lock, and converts failures into four typed errors with fixed, safe wording.

Two rules shape the design. Capability support is only ever reported when the
individual camera positively confirmed it in a ``getMost`` batch -- Tapo models
differ wildly and an optimistic guess turns into a command that a camera
answers with an error the operator cannot act on. And nothing here may disturb
recording or live view: this module deliberately imports no recording,
streaming or motion manager, so a camera that stops answering its vendor API
costs a 503 on these routes and nothing else.

Three vendor methods make the *camera* reach out to TP-Link over the internet
(``isUpdateAvailable``, ``startFirmwareUpgrade``, ``getFirmwareUpdateStatus``).
They are never called, and the last of them is omitted from the discovery batch
that would otherwise include it. Firmware upgrade, SD-card format and factory
reset have no representation here at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, get_args

from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras import vendor_pool
from app.database.base import utcnow
from app.database.models import Camera
from app.discovery import netsafety
from app.logging_config import get_logger
from app.security.crypto import try_decrypt_secret
from app.system import settings_store

logger = get_logger("CAMERA")

#: One JSON row for every camera's last confirmed capability set. Persisting it
#: means a restart serves the cached answer instead of probing every camera the
#: first time somebody opens its settings page.
CAPABILITIES_SETTING_KEY = "cameras.tapo_capabilities"

#: ``Tapo.__init__`` itself performs ``getBasicInfo``, so connecting is a
#: network round trip and gets the same budget as a discovery call.
#:
#: These numbers are sized against the *proxy*, not against the camera. An
#: installation reached through Cloudflare, a tunnel or any other reverse proxy
#: has an origin response window; exceeding it makes the proxy invent its own
#: error page, which the operator then reads as a camera fault. Sentinel answers
#: first, with something true, every time.
CONNECT_TIMEOUT_SECONDS = 6.0
DISCOVERY_TIMEOUT_SECONDS = 8.0
COMMAND_TIMEOUT_SECONDS = 6.0

#: The hard ceiling for one HTTP request's worth of vendor work, across every
#: attempt and every fallback. Well under any reverse-proxy origin window, and
#: under the browser client's own 20-second abort, so a slow camera produces a
#: Sentinel error rather than a proxy page or a silent client timeout.
REQUEST_DEADLINE_SECONDS = 14.0

#: Capability discovery is expensive and near-static; state is cheap and moves.
CAPABILITY_TTL_SECONDS = 600.0
#: A discovery that only partly answered is still worth keeping -- the confirmed
#: controls stay usable -- but it is re-attempted far sooner.
PARTIAL_CAPABILITY_TTL_SECONDS = 60.0
STATE_TTL_SECONDS = 5.0
#: A persisted capability set older than this is re-probed rather than trusted:
#: a camera may have been swapped for a different model at the same address.
PERSISTED_CAPABILITY_MAX_AGE_SECONDS = 7 * 24 * 3600.0

RETRY_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.4

#: Consecutive transient failures before the breaker opens. Tapo firmware
#: answers a handful of HTTPS clients at a time; hammering an unhealthy camera
#: is how a slow camera becomes an unreachable one.
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60.0
#: A rejected credential opens the breaker immediately and for far longer:
#: repeated bad logins are what triggers a device-side lockout.
BREAKER_AUTH_COOLDOWN_SECONDS = 30 * 60.0

#: Cached sessions are bounded so a large install cannot pin one HTTPS session
#: per camera forever; the least recently used one is dropped first.
SESSION_LIMIT = 16

#: Vendor calls run on their own threads, never on the interpreter's default
#: executor.
#:
#: This matters more than it looks. A vendor call that times out cannot be
#: cancelled -- the worker thread still owns an open TLS socket and only returns
#: when the operating system gives up on it, which can be minutes. On the shared
#: default executor (``min(32, cpu_count + 4)`` workers -- a dozen on a
#: typical small host) a handful of unhealthy cameras will occupy every slot, and
#: then *every* ``asyncio.to_thread`` in the application queues behind them:
#: ffprobe, snapshots, export muxing, storage statistics, PTZ. Measured on a
#: test installation, twelve stuck vendor calls turned an unrelated
#: 10-millisecond thread hop into an 18-second one. Requests that slow stop
#: being answered inside a proxy's origin window, which is how one flaky camera
#: became a site-wide outage.
#:
#: Four is deliberate: Tapo firmware serves a handful of HTTPS clients at a
#: time, and Sentinel already serialises per camera, so more threads would only
#: buy the ability to hold more sockets open on a camera that is not answering.
VENDOR_THREAD_LIMIT = 4

_UNAVAILABLE_MESSAGE = "The camera did not answer its local control service."
_AUTH_MESSAGE = "The camera rejected the stored Tapo credentials."
_UNSUPPORTED_MESSAGE = "This camera does not support that feature."
_LENS_MASK_MESSAGE = "The camera's own lens mask is active, so it refused the command."
_BREAKER_MESSAGE = "The camera's local control service is failing; it will be retried shortly."
_NOT_CONFIGURED_MESSAGE = "No Tapo credentials are stored for this camera."
_PRIVATE_HOST_MESSAGE = "This camera's address is not on your own network."


class TapoError(RuntimeError):
    """A safe, user-facing Tapo failure. Never carries vendor text."""


class TapoUnsupportedError(TapoError):
    """The camera has no such feature, or refused the command outright."""


class TapoAuthError(TapoError):
    """The camera rejected the stored credentials."""


class TapoUnavailableError(TapoError):
    """The camera did not answer in time, or the breaker is open."""


# --- Vocabulary ---------------------------------------------------------

#: Features a caller may write through ``POST /{camera_id}/tapo/{feature}``.
#: Siren and reboot are deliberately absent: both have their own route because
#: both require an explicit confirmation flag.
TapoFeature = Literal[
    "led",
    "privacy_mask",
    "day_night",
    "smart_track",
    "floodlight",
    "motion",
    "person",
    "vehicle",
    "pet",
    "package",
    "line_crossing",
    "tamper",
    "baby_cry",
    "bark",
]
WRITEABLE_FEATURES: tuple[str, ...] = get_args(TapoFeature)

DayNightMode = Literal["day", "night", "auto"]
SensitivityLabel = Literal["low", "normal", "high"]

#: The camera's own night-vision vocabulary is "on"/"off"/"auto", where "on"
#: means the infrared cut filter is engaged. Sentinel says day/night/auto
#: instead, because "night vision off" reading as "day mode" is not obvious.
_DAY_NIGHT_TO_VENDOR: dict[str, str] = {"day": "off", "night": "on", "auto": "auto"}
_DAY_NIGHT_FROM_VENDOR: dict[str, str] = {"off": "day", "on": "night", "auto": "auto"}

_LABEL_TO_NUMBER: dict[str, int] = {"low": 20, "normal": 50, "high": 80}


@dataclass(frozen=True, slots=True)
class _Family:
    """One detection family and the exact shape its setter demands.

    The conventions genuinely differ per family in the vendor client, so a
    single generic setter would raise at runtime on half of them. Sentinel
    accepts one uniform sensitivity from callers and converts it here.
    """

    feature: str
    probe: tuple[str, ...]
    """``getMost`` methods whose presence positively confirms the family."""
    setter: str
    sensitivity: Literal["either", "label", "numeric", "none"]
    root: str
    """Top-level key of the ``getMost`` entry holding this family's state."""
    leaf: str


#: ``either`` accepts an int or one of the three labels; ``label`` accepts only
#: the labels (the vendor remaps "normal" to "medium" itself); ``numeric``
#: accepts only an int 1-100; ``none`` takes no sensitivity at all.
DETECTION_FAMILIES: tuple[_Family, ...] = (
    _Family(
        "motion",
        ("getDetectionConfig",),
        "setMotionDetection",
        "either",
        "motion_detection",
        "motion_det",
    ),
    _Family(
        "person",
        ("getPersonDetectionConfig",),
        "setPersonDetection",
        "either",
        "people_detection",
        "detection",
    ),
    _Family(
        "vehicle",
        ("getVehicleDetectionConfig",),
        "setVehicleDetection",
        "either",
        "vehicle_detection",
        "detection",
    ),
    _Family(
        "pet",
        ("getPetDetectionConfig",),
        "setPetDetection",
        "either",
        "pet_detection",
        "detection",
    ),
    _Family(
        "bark",
        ("getBarkDetectionConfig",),
        "setBarkDetection",
        "either",
        "bark_detection",
        "detection",
    ),
    _Family(
        "tamper",
        ("getTamperDetectionConfig",),
        "setTamperDetection",
        "label",
        "tamper_detection",
        "tamper_det",
    ),
    _Family(
        "baby_cry",
        ("getBCDConfig",),
        "setBabyCryDetection",
        "label",
        "sound_detection",
        "bcd",
    ),
    _Family(
        "package",
        ("getPackageDetectionConfig",),
        "setPackageDetection",
        "numeric",
        "package_detection",
        "detection",
    ),
    _Family(
        "line_crossing",
        ("getLinecrossingDetectionConfig",),
        "setLinecrossingDetection",
        "none",
        "linecrossing_detection",
        "detection",
    ),
)

_FAMILIES_BY_FEATURE: dict[str, _Family] = {family.feature: family for family in DETECTION_FAMILIES}

#: Everything else a camera may or may not have. The value is the set of
#: ``getMost`` methods that, when the camera answers any one of them with real
#: data, prove the feature exists on *this* device.
_SIMPLE_PROBES: dict[str, tuple[str, ...]] = {
    "device_info": ("getDeviceInfo",),
    "sd_card": ("getSdCardStatus",),
    "led": ("getLedStatus",),
    "privacy_mask": ("getLensMaskConfig",),
    "day_night": ("getLightFrequencyInfo",),
    "smart_track": ("getTargetTrackConfig",),
    "floodlight": ("getFloodlightStatus", "getFloodlightConfig"),
    "alarm": ("getLastAlarmInfo", "getAlarmConfig", "getSirenStatus"),
    "presets": ("getPresetConfig",),
    "cruise": ("getPatrolAction", "getPatrolSchedule"),
    "reboot": ("getReboot",),
}

CAPABILITY_NAMES: tuple[str, ...] = (
    *_SIMPLE_PROBES.keys(),
    *(family.feature for family in DETECTION_FAMILIES),
)

#: Which vendor methods confirm each feature. Used to tell "this pass could not
#: check it" apart from "this camera does not have it" -- the difference between
#: keeping a control the operator was using and silently removing it because one
#: request timed out.
_FEATURE_METHODS: dict[str, frozenset[str]] = {
    **{feature: frozenset(methods) for feature, methods in _SIMPLE_PROBES.items()},
    **{family.feature: frozenset(family.probe) for family in DETECTION_FAMILIES},
}


def _carry_unchecked(
    fresh: TapoCapabilities, previous: TapoCapabilities | None, unanswered: Sequence[str]
) -> frozenset[str]:
    """Keep features whose probes did not answer this time but did before.

    A capability is only dropped when the camera answered its probe and said no.
    Dropping one because a request timed out would take a working control off
    the panel, which is the behaviour this whole discovery path exists to fix.
    """
    if previous is None or not unanswered:
        return fresh.supported
    silent = set(unanswered)
    carried = {
        feature
        for feature in previous.supported
        if feature not in fresh.supported
        and _FEATURE_METHODS.get(feature, frozenset())
        and _FEATURE_METHODS[feature] <= silent
    }
    return frozenset(fresh.supported | carried)


#: The exact vendor requests Sentinel needs, and nothing else.
#:
#: The library's own batch helper asks for eighty-seven methods in one
#: ``multipleRequest`` and then refuses the entire answer if the response count
#: does not match, or if one particular method comes back a different number of
#: times than it expected. On real firmware that turns a single unsupported
#: capability into "no controls at all", which is the failure this map exists to
#: end: Sentinel asks for the twenty-four methods it can actually use, owns the
#: decoding, and can split the batch when a camera chokes on one of them.
#:
#: No firmware method appears here. ``getFirmwareUpdateStatus`` and its
#: relatives make the *camera* contact TP-Link, which a LAN-only control surface
#: must never cause; they are not omitted from a larger list, they are simply
#: never asked for.
_PROBE_REQUESTS: dict[str, dict[str, Any]] = {
    "getDeviceInfo": {"device_info": {"name": ["basic_info"]}},
    "getSdCardStatus": {"harddisk_manage": {"table": ["hd_info"]}},
    "getLedStatus": {"led": {"name": ["config"]}},
    "getLensMaskConfig": {"lens_mask": {"name": ["lens_mask_info"]}},
    "getLightFrequencyInfo": {"image": {"name": ["common"]}},
    "getTargetTrackConfig": {"target_track": {"name": ["target_track_info"]}},
    "getFloodlightStatus": {"floodlight": {"get_floodlight_status": ""}},
    "getFloodlightConfig": {"floodlight": {"name": "config"}},
    "getLastAlarmInfo": {"msg_alarm": {"name": ["chn1_msg_alarm_info"]}},
    "getAlarmConfig": {"msg_alarm": {}},
    "getSirenStatus": {"siren": {}},
    "getPresetConfig": {"preset": {"name": ["preset"]}},
    "getPatrolAction": {"patrol": {"get_patrol_action": {}}},
    "getPatrolSchedule": {"patrol": {"get_patrol_schedule": {}}},
    "getReboot": {"timing_reboot": {"name": ["reboot"]}},
    "getDetectionConfig": {"motion_detection": {"name": ["motion_det"]}},
    "getPersonDetectionConfig": {"people_detection": {"name": ["detection"]}},
    "getVehicleDetectionConfig": {"vehicle_detection": {"name": ["detection"]}},
    "getPetDetectionConfig": {"pet_detection": {"name": ["detection"]}},
    "getBarkDetectionConfig": {"bark_detection": {"name": ["detection"]}},
    "getTamperDetectionConfig": {"tamper_detection": {"name": ["tamper_det"]}},
    "getBCDConfig": {"sound_detection": {"name": ["bcd"]}},
    "getPackageDetectionConfig": {"package_detection": {"name": ["detection"]}},
    "getLinecrossingDetectionConfig": {
        "linecrossing_detection": {"name": ["detection", "arming_schedule"]}
    },
}

#: Methods asked for together. Small enough that one bad method costs its group
#: rather than the panel, large enough that a healthy camera answers in two
#: round trips instead of twenty-four.
_PROBE_BATCH_SIZE = 8

#: How far the batch is allowed to split when a camera rejects it. Eight, four,
#: two, one: four levels isolates any single offending method, and stopping
#: there keeps a camera that rejects *everything* from being asked twenty-four
#: separate times.
_PROBE_MAX_SPLIT_DEPTH = 4


def _batch_request(methods: Sequence[str]) -> Callable[[Any], dict[str, Any]]:
    """Build the worker that asks one group of methods and decodes the answer."""
    group = list(methods)
    payload = {
        "method": "multipleRequest",
        "params": {
            "requests": [{"method": method, "params": _PROBE_REQUESTS[method]} for method in group]
        },
    }

    def work(client: Any) -> dict[str, Any]:
        return _merge_batch(group, client.performRequest(payload))

    return work


@dataclass(frozen=True, slots=True)
class _Discovery:
    """One discovery pass, including what the camera would not answer."""

    payload: dict[str, Any]
    unanswered: tuple[str, ...]

    @property
    def partial(self) -> bool:
        return bool(self.unanswered)


def _merge_batch(methods: Sequence[str], results: Any) -> dict[str, Any]:
    """Turn one ``multipleRequest`` answer into ``{method: [result | False]}``.

    ``False`` means "asked, not answered", which is exactly what the capability
    decoder needs in order to tell a missing feature from a present one. Some
    firmware answers out of order and some answers methods that were not asked
    for; both are tolerated here rather than treated as a fatal surprise.
    """
    merged: dict[str, Any] = {method: [False] for method in methods}
    if not isinstance(results, dict):
        return merged
    responses = results.get("result", {})
    entries = responses.get("responses") if isinstance(responses, dict) else None
    if not isinstance(entries, list):
        return merged
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("error_code") != 0:
            continue
        method = entry.get("method")
        payload = entry.get("result")
        if not isinstance(method, str) or not isinstance(payload, dict):
            continue
        slots = merged.setdefault(method, [False])
        for index, existing in enumerate(slots):
            if existing is False:
                slots[index] = payload
                break
        else:
            slots.append(payload)
    return merged


FORBIDDEN_METHODS: frozenset[str] = frozenset(
    {
        "isUpdateAvailable",
        "startFirmwareUpgrade",
        "getFirmwareUpdateStatus",
        "format",
    }
)

_AUTH_MARKERS: tuple[str, ...] = (
    "invalid authentication",
    "invalid login credentials",
    "-40209",
    "-40418",
    "-40412",
    "-71103",
    "user id is not authorized",
    "authentication failed",
)
_UNSUPPORTED_MARKERS: tuple[str, ...] = (
    "-40105",
    "-40106",
    "-40210",
    "-40101",
    "method does not exist",
    "unsupported_method",
    "method_do_not_exist",
    "is not supported by this camera",
    "failed to detect device type",
    "unexpected number of",
)


# --- Value objects ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TapoDeviceInfo:
    """The only three device fields Sentinel ever repeats to a caller.

    Everything else ``getDeviceInfo`` returns -- MAC, IP, device id, the
    account alias -- identifies the installation rather than describing the
    camera, so it is dropped here rather than filtered further downstream.
    """

    model: str | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None


@dataclass(frozen=True, slots=True)
class TapoSdCard:
    present: bool = False
    status: Literal["normal", "unformatted", "abnormal", "missing", "unknown"] = "unknown"
    total_gb: float | None = None
    free_gb: float | None = None


@dataclass(frozen=True, slots=True)
class TapoDetectionState:
    supported: bool = False
    enabled: bool | None = None
    sensitivity: int | None = None
    """Normalised to 0-100; ``None`` when the camera reports only a label."""
    sensitivity_label: str | None = None


@dataclass(frozen=True, slots=True)
class TapoCapabilities:
    """What this individual camera confirmed it can do."""

    supported: frozenset[str] = frozenset()
    device: TapoDeviceInfo = TapoDeviceInfo()
    checked_at: datetime | None = None
    unavailable: tuple[str, ...] = ()
    """Vendor methods this camera would not answer during the last discovery.

    Recorded rather than hidden: "the SD-card read failed" and "this model has
    no SD card" look identical in a list of confirmed capabilities, and only one
    of them is worth retrying.
    """

    def has(self, feature: str) -> bool:
        return feature in self.supported

    @property
    def partial(self) -> bool:
        return bool(self.unavailable)


@dataclass(frozen=True, slots=True)
class TapoState:
    led_enabled: bool | None = None
    privacy_mask_enabled: bool | None = None
    day_night_mode: str | None = None
    smart_track_enabled: bool | None = None
    floodlight_enabled: bool | None = None
    siren_active: bool | None = None
    cruise_enabled: bool | None = None
    preset_count: int | None = None
    sd_card: TapoSdCard | None = None
    detections: dict[str, TapoDetectionState] = field(default_factory=dict)
    read_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Target:
    """Everything one vendor call needs, resolved away from the ORM object."""

    camera_id: str
    camera_name: str
    host: str
    user: str
    password: str
    fingerprint: str


@dataclass(slots=True)
class _Session:
    client: Any
    fingerprint: str
    last_used: float


@dataclass(slots=True)
class _Breaker:
    failures: int = 0
    opened_until: float = 0.0


@dataclass(slots=True)
class _CapabilityCache:
    value: TapoCapabilities
    fingerprint: str
    expires_at: float


@dataclass(slots=True)
class _StateCache:
    value: TapoState
    fingerprint: str
    expires_at: float


# --- Helpers ------------------------------------------------------------


def _now() -> float:
    """Monotonic clock for every TTL and breaker window in this module."""
    return time.monotonic()


def _fingerprint(host: str, user: str, password: str) -> str:
    """Identify a credential set without keeping a second plaintext copy.

    The cached session has to be thrown away when the host, the account or the
    password changes. Comparing digests answers that without this module
    holding the password anywhere but inside the vendor client.
    """
    digest = hashlib.sha256()
    for part in (host, user, password):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _translate(exc: BaseException) -> TapoError:
    """Convert a bare vendor exception into a typed, safe error.

    The vendor formats failures as ``Error: <message>, Response: <raw json>``,
    so the text is matched for known markers and then discarded -- the returned
    error only ever carries one of this module's fixed messages.
    """
    if isinstance(exc, TapoError):
        return exc
    if isinstance(exc, TimeoutError):
        return TapoUnavailableError(_UNAVAILABLE_MESSAGE)
    text = str(exc).lower()
    if any(marker in text for marker in _AUTH_MARKERS):
        return TapoAuthError(_AUTH_MESSAGE)
    if "-64324" in text:
        return TapoUnsupportedError(_LENS_MASK_MESSAGE)
    if any(marker in text for marker in _UNSUPPORTED_MARKERS):
        return TapoUnsupportedError(_UNSUPPORTED_MESSAGE)
    return TapoUnavailableError(_UNAVAILABLE_MESSAGE)


def _entry(payload: dict[str, Any], method: str) -> dict[str, Any] | None:
    """Return the first real answer for ``method`` in a ``getMost`` batch.

    ``getMost`` pre-allocates ``False`` for every requested method and only
    overwrites the ones the camera actually answered, so ``False`` is precisely
    "this model did not confirm it" and must never be read as support.
    """
    results = payload.get(method)
    if not isinstance(results, list):
        return None
    for result in results:
        if isinstance(result, dict) and result:
            return result
    return None


def _nested(entry: dict[str, Any] | None, *keys: str) -> Any:
    current: Any = entry
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _is_on(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"on", "true", "1", "start"}:
            return True
        if lowered in {"off", "false", "0", "stop"}:
            return False
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        with suppress(ValueError):
            return float(value.strip())
    return None


def _short_text(value: Any) -> str | None:
    """Keep a device string only when it looks like a model or version label."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > 64:
        return None
    return stripped


def _decode_device_info(entry: dict[str, Any] | None) -> TapoDeviceInfo:
    basic = _nested(entry, "device_info", "basic_info")
    if not isinstance(basic, dict):
        basic = entry if isinstance(entry, dict) else {}
    return TapoDeviceInfo(
        model=_short_text(basic.get("device_model") or basic.get("model")),
        hardware_version=_short_text(basic.get("hw_version")),
        firmware_version=_short_text(basic.get("sw_version") or basic.get("fw_ver")),
    )


def _decode_sd_card(entry: dict[str, Any] | None) -> TapoSdCard | None:
    info = _nested(entry, "harddisk_manage", "hd_info")
    if isinstance(info, list):
        for item in info:
            if isinstance(item, dict):
                inner = next((value for value in item.values() if isinstance(value, dict)), item)
                return _decode_sd_card_entry(inner)
        return TapoSdCard(present=False, status="missing")
    if isinstance(info, dict):
        return _decode_sd_card_entry(info)
    return None


def _decode_sd_card_entry(info: dict[str, Any]) -> TapoSdCard:
    raw = str(info.get("status", "")).strip().lower()
    # A closed vocabulary rather than whatever string the firmware returned:
    # the interface has to render it, and vendor text is not a stable contract.
    if raw in {"normal", "ok"}:
        status: Any = "normal"
    elif raw in {"unformatted", "unformat", "unknown_format"}:
        status = "unformatted"
    elif raw in {"", "none", "not_exist", "unplugged"}:
        status = "missing"
    elif raw == "unknown":
        status = "unknown"
    else:
        status = "abnormal"
    return TapoSdCard(
        present=status not in {"missing", "unknown"},
        status=status,
        total_gb=_as_float(info.get("total_space")),
        free_gb=_as_float(info.get("free_space")),
    )


def _decode_detection(family: _Family, entry: dict[str, Any] | None) -> TapoDetectionState:
    detection = _nested(entry, family.root, family.leaf)
    if not isinstance(detection, dict):
        return TapoDetectionState()
    label = detection.get("sensitivity")
    numeric = _as_int(detection.get("digital_sensitivity"))
    if numeric is None:
        numeric = _as_int(label)
    return TapoDetectionState(
        supported=True,
        enabled=_is_on(detection.get("enabled")),
        sensitivity=numeric,
        # "medium" is the vendor's own spelling of what Sentinel calls normal.
        sensitivity_label=(
            "normal"
            if label == "medium"
            else label
            if isinstance(label, str) and label in {"low", "high"}
            else None
        ),
    )


def _decode_capabilities(payload: dict[str, Any], checked_at: datetime) -> TapoCapabilities:
    supported: set[str] = set()
    for feature, methods in _SIMPLE_PROBES.items():
        if any(_entry(payload, method) is not None for method in methods):
            supported.add(feature)
    for family in DETECTION_FAMILIES:
        entry = next(
            (found for method in family.probe if (found := _entry(payload, method)) is not None),
            None,
        )
        # An answered method is not enough on its own: several models answer
        # the request with an empty body for a family they do not have.
        if _nested(entry, family.root, family.leaf) is not None:
            supported.add(family.feature)
    device = _decode_device_info(_entry(payload, "getDeviceInfo"))
    return TapoCapabilities(
        supported=frozenset(supported),
        device=device,
        checked_at=checked_at,
    )


def _decode_state(payload: dict[str, Any], capabilities: TapoCapabilities) -> TapoState:
    """Derive every readable value from the single discovery/poll batch."""
    detections = {
        family.feature: _decode_detection(
            family,
            next(
                (
                    found
                    for method in family.probe
                    if (found := _entry(payload, method)) is not None
                ),
                None,
            ),
        )
        for family in DETECTION_FAMILIES
        if capabilities.has(family.feature)
    }
    day_night_raw = _nested(_entry(payload, "getLightFrequencyInfo"), "image", "common", "inf_type")
    presets = _nested(_entry(payload, "getPresetConfig"), "preset", "preset", "id")
    floodlight = _nested(
        _entry(payload, "getFloodlightStatus"), "floodlight", "get_floodlight_status"
    )
    if isinstance(floodlight, dict):
        floodlight = floodlight.get("status") or floodlight.get("value")
    return TapoState(
        led_enabled=_is_on(_nested(_entry(payload, "getLedStatus"), "led", "config", "enabled")),
        privacy_mask_enabled=_is_on(
            _nested(_entry(payload, "getLensMaskConfig"), "lens_mask", "lens_mask_info", "enabled")
        ),
        day_night_mode=_DAY_NIGHT_FROM_VENDOR.get(str(day_night_raw))
        if day_night_raw is not None
        else None,
        smart_track_enabled=_is_on(
            _nested(
                _entry(payload, "getTargetTrackConfig"),
                "target_track",
                "target_track_info",
                "enabled",
            )
        ),
        floodlight_enabled=_is_on(floodlight),
        siren_active=_is_on(_nested(_entry(payload, "getSirenStatus"), "msg_alarm", "status")),
        cruise_enabled=_is_on(
            _nested(_entry(payload, "getPatrolSchedule"), "patrol", "patrol", "enabled")
        ),
        preset_count=len(presets) if isinstance(presets, list) else None,
        sd_card=_decode_sd_card(_entry(payload, "getSdCardStatus")),
        detections=detections,
        read_at=utcnow(),
    )


def _sensitivity_for(family: _Family, sensitivity: int | str | None) -> int | str | None:
    """Coerce one uniform caller value into what *this* family's setter takes.

    Sentinel offers a single sensitivity control across every detection type.
    The vendor does not: two families accept only labels, one accepts only an
    integer and one accepts none at all. Converting here is what keeps a single
    generic setter from raising on half the families at runtime.
    """
    if sensitivity is None:
        return None
    if family.sensitivity == "none":
        raise TapoUnsupportedError("Line-crossing detection has no sensitivity setting.")

    if isinstance(sensitivity, str):
        label = sensitivity.strip().lower()
        if label not in _LABEL_TO_NUMBER:
            raise TapoUnsupportedError("Sensitivity must be low, normal or high.")
        number = _LABEL_TO_NUMBER[label]
    else:
        number = int(sensitivity)
        if not 1 <= number <= 100:
            raise TapoUnsupportedError("Sensitivity must be between 1 and 100.")
        label = "low" if number <= 20 else "normal" if number <= 50 else "high"

    if family.sensitivity == "label":
        # setTamperDetection/setBabyCryDetection reject anything but these
        # three words and remap "normal" to "medium" themselves.
        return label
    if family.sensitivity == "numeric":
        # setPackageDetection compares the value with int() and refuses 0.
        return max(1, number)
    return number


# --- Manager ------------------------------------------------------------


class TapoManager:
    """Own one bounded vendor session per camera and everything guarding it."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._breakers: dict[str, _Breaker] = {}
        self._capabilities: dict[str, _CapabilityCache] = {}
        self._states: dict[str, _StateCache] = {}
        # Capability rows for every camera share one JSON setting. Serialise
        # its read-modify-write so two cameras discovered at once cannot erase
        # each other's confirmed capabilities.
        self._persistence_lock = asyncio.Lock()

    # -- lifecycle --

    async def close(self, camera_id: str) -> None:
        """Drop everything held for one camera, on edit or removal."""
        lock = self._locks.get(camera_id)
        if lock is not None:
            async with lock:
                await self._discard(camera_id)
        else:
            await self._discard(camera_id)
        self._locks.pop(camera_id, None)
        self._breakers.pop(camera_id, None)
        self._capabilities.pop(camera_id, None)
        self._states.pop(camera_id, None)

    async def close_all(self) -> None:
        for camera_id in list(self._sessions):
            await self._discard(camera_id)
        self._locks.clear()
        self._breakers.clear()
        self._capabilities.clear()
        self._states.clear()

    async def _discard(self, camera_id: str) -> None:
        session = self._sessions.pop(camera_id, None)
        if session is None:
            return
        with suppress(Exception):
            await vendor_pool.run(session.client.close, max_workers=VENDOR_THREAD_LIMIT)

    # -- public reads and writes --

    async def capabilities(
        self,
        session: AsyncSession,
        camera: Camera,
        *,
        refresh: bool = False,
    ) -> TapoCapabilities:
        """Return what this camera confirmed it supports, cached and persisted."""
        target = self._target(camera)
        if not refresh:
            cached = self._capabilities.get(camera.id)
            if cached is not None and cached.fingerprint == target.fingerprint:
                if cached.expires_at > _now():
                    return cached.value
            else:
                restored = await self._restore(session, target)
                if restored is not None:
                    return restored

        previous = self._known(session, target)
        discovery = await self._discover(target)
        capabilities = _decode_capabilities(discovery.payload, utcnow())
        capabilities = replace(
            capabilities,
            supported=_carry_unchecked(capabilities, previous, discovery.unanswered),
            unavailable=discovery.unanswered,
        )
        self._capabilities[camera.id] = _CapabilityCache(
            value=capabilities,
            fingerprint=target.fingerprint,
            # A partial answer is cached for a fraction of the usual life. It is
            # worth keeping -- the confirmed controls stay usable -- but the
            # camera should get another chance soon rather than in ten minutes.
            expires_at=_now()
            + (PARTIAL_CAPABILITY_TTL_SECONDS if discovery.partial else CAPABILITY_TTL_SECONDS),
        )
        self._states[camera.id] = _StateCache(
            value=_decode_state(discovery.payload, capabilities),
            fingerprint=target.fingerprint,
            expires_at=_now() + STATE_TTL_SECONDS,
        )
        await self._persist(session, target, capabilities)
        return capabilities

    async def state(
        self,
        session: AsyncSession,
        camera: Camera,
        *,
        refresh: bool = False,
    ) -> TapoState:
        """Read every supported value in one round trip, cached for a few seconds."""
        target = self._target(camera)
        cached = self._states.get(camera.id)
        if (
            not refresh
            and cached is not None
            and cached.fingerprint == target.fingerprint
            and cached.expires_at > _now()
        ):
            return cached.value

        previous = self._known(session, target)
        discovery = await self._discover(target)
        capabilities = _decode_capabilities(discovery.payload, utcnow())
        capabilities = replace(
            capabilities,
            supported=_carry_unchecked(capabilities, previous, discovery.unanswered),
            unavailable=discovery.unanswered,
        )
        self._capabilities[camera.id] = _CapabilityCache(
            value=capabilities,
            fingerprint=target.fingerprint,
            expires_at=_now()
            + (PARTIAL_CAPABILITY_TTL_SECONDS if discovery.partial else CAPABILITY_TTL_SECONDS),
        )
        state = _decode_state(discovery.payload, capabilities)
        self._states[camera.id] = _StateCache(
            value=state,
            fingerprint=target.fingerprint,
            expires_at=_now() + STATE_TTL_SECONDS,
        )
        await self._persist(session, target, capabilities)
        return state

    async def set_feature(
        self,
        session: AsyncSession,
        camera: Camera,
        feature: str,
        *,
        enabled: bool | None = None,
        sensitivity: int | str | None = None,
        mode: str | None = None,
    ) -> None:
        """Apply one write, after confirming this camera has the feature."""
        target = self._target(camera)
        capabilities = await self.capabilities(session, camera)
        if not capabilities.has(feature):
            raise TapoUnsupportedError(_UNSUPPORTED_MESSAGE)

        family = _FAMILIES_BY_FEATURE.get(feature)
        work: Callable[[Any], Any]
        if family is not None:
            work = self._detection_write(family, enabled, sensitivity)
        elif feature == "day_night":
            work = self._day_night_write(mode)
        else:
            if enabled is None:
                raise TapoUnsupportedError("This feature needs an enabled value.")
            work = self._switch_write(feature, enabled)

        await self._invoke(target, feature, work, timeout_seconds=COMMAND_TIMEOUT_SECONDS)
        # The camera has moved on; the next read must not serve the old values.
        self._states.pop(camera.id, None)

    async def set_siren(self, session: AsyncSession, camera: Camera, *, active: bool) -> None:
        """Start or stop the manual siren. Never touches the alarm schedule."""
        target = self._target(camera)
        capabilities = await self.capabilities(session, camera)
        if not capabilities.has("alarm"):
            raise TapoUnsupportedError("This camera has no siren.")
        work: Callable[[Any], Any] = (
            (lambda client: client.startManualAlarm())
            if active
            else (lambda client: client.stopManualAlarm())
        )
        await self._invoke(target, "siren", work, timeout_seconds=COMMAND_TIMEOUT_SECONDS)
        self._states.pop(camera.id, None)

    async def reboot(self, session: AsyncSession, camera: Camera) -> None:
        """Reboot the camera. Recording is left entirely alone."""
        target = self._target(camera)
        capabilities = await self.capabilities(session, camera)
        if not capabilities.has("reboot"):
            raise TapoUnsupportedError("This camera does not support a remote reboot.")
        await self._invoke(
            target,
            "reboot",
            lambda client: client.reboot(),
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
            attempts=1,
        )
        # The session dies with the camera, and everything cached about it is
        # stale until it comes back.
        await self.close(camera.id)

    # -- write builders --

    @staticmethod
    def _detection_write(
        family: _Family,
        enabled: bool | None,
        sensitivity: int | str | None,
    ) -> Callable[[Any], Any]:
        if enabled is None:
            raise TapoUnsupportedError("A detection change needs an enabled value.")
        value = _sensitivity_for(family, sensitivity)
        setter = family.setter
        if family.sensitivity == "none":
            return lambda client: getattr(client, setter)(enabled)
        if value is None:
            return lambda client: getattr(client, setter)(enabled)
        return lambda client: getattr(client, setter)(enabled, value)

    @staticmethod
    def _day_night_write(mode: str | None) -> Callable[[Any], Any]:
        if mode is None or mode not in _DAY_NIGHT_TO_VENDOR:
            raise TapoUnsupportedError("Day/night mode must be day, night or auto.")
        vendor_mode = _DAY_NIGHT_TO_VENDOR[mode]
        return lambda client: client.setDayNightMode(vendor_mode)

    @staticmethod
    def _switch_write(feature: str, enabled: bool) -> Callable[[Any], Any]:
        if feature == "led":
            return lambda client: client.setLEDEnabled(enabled)
        if feature == "privacy_mask":
            # The camera's own lens mask. Sentinel's global privacy mode is a
            # separate, server-side control and is never wired to this.
            return lambda client: client.setPrivacyMode(enabled)
        if feature == "smart_track":
            return lambda client: client.setAutoTrackTarget(enabled)
        if feature == "floodlight":
            return lambda client: client.manualFloodlightOp(enabled)
        raise TapoUnsupportedError(_UNSUPPORTED_MESSAGE)

    # -- vendor plumbing --

    async def _discover(self, target: _Target) -> _Discovery:
        """Ask this camera only what Sentinel can use, and keep what answers.

        A single batch when the camera is healthy. When it is not, the batch is
        halved and retried, down to individual methods, so one capability the
        firmware mishandles costs that capability and not the whole panel.

        Everything runs inside one deadline. A camera that has stopped answering
        must not be able to hold an HTTP request open past a proxy's origin
        window, because what the operator then sees is the proxy's error page
        with Sentinel's name nowhere on it.
        """
        methods = list(_PROBE_REQUESTS)
        deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
        payload: dict[str, Any] = {}
        unanswered: list[str] = []
        first_fatal: TapoError | None = None
        answered_any = False

        batches: list[tuple[list[str], int]] = [
            (methods[index : index + _PROBE_BATCH_SIZE], 0)
            for index in range(0, len(methods), _PROBE_BATCH_SIZE)
        ]
        while batches:
            group, depth = batches.pop(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                unanswered.extend(group)
                unanswered.extend(name for rest, _ in batches for name in rest)
                batches.clear()
                break
            try:
                answer = await self._invoke(
                    target,
                    "discovery",
                    _batch_request(group),
                    timeout_seconds=min(DISCOVERY_TIMEOUT_SECONDS, remaining),
                    attempts=1,
                )
            except TapoAuthError as exc:
                # A rejected credential is the same answer for every method.
                # Splitting the batch would only spend the device's lockout
                # budget proving it again.
                raise exc
            except TapoError as exc:
                if first_fatal is None:
                    first_fatal = exc
                if len(group) > 1 and depth < _PROBE_MAX_SPLIT_DEPTH:
                    middle = len(group) // 2
                    batches[:0] = [(group[:middle], depth + 1), (group[middle:], depth + 1)]
                else:
                    unanswered.extend(group)
                continue
            answered_any = True
            payload.update(answer)

        if not answered_any:
            raise first_fatal or TapoUnavailableError(_UNAVAILABLE_MESSAGE)
        if unanswered:
            logger.info(
                "Some Tapo capabilities did not answer; keeping the confirmed ones",
                extra={
                    "camera_id": target.camera_id,
                    "camera": target.camera_name,
                    "unanswered": len(unanswered),
                },
            )
        return _Discovery(payload=payload, unanswered=tuple(unanswered))

    async def _invoke(
        self,
        target: _Target,
        operation: str,
        work: Callable[[Any], Any],
        *,
        timeout_seconds: float,
        attempts: int = RETRY_ATTEMPTS,
        deadline: float | None = None,
    ) -> Any:
        """Run one synchronous vendor call safely: breaker, lock, thread, deadline."""
        self._check_breaker(target)
        await self._ensure_local(target)
        if deadline is None:
            deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
        lock = self._locks.setdefault(target.camera_id, asyncio.Lock())
        last: TapoError = TapoUnavailableError(_UNAVAILABLE_MESSAGE)
        for attempt in range(1, attempts + 1):
            budget = min(timeout_seconds, deadline - time.monotonic())
            if budget <= 0:
                break
            async with lock:
                try:
                    result = await self._run_once(target, work, timeout_seconds=budget)
                except TapoError as exc:
                    last = exc
                    self._record_failure(target, exc, operation)
                    if not isinstance(exc, TapoUnavailableError):
                        raise
                else:
                    self._breakers.pop(target.camera_id, None)
                    return result
            if attempt < attempts:
                if deadline - time.monotonic() <= RETRY_BACKOFF_SECONDS * attempt:
                    # No room for another attempt inside the deadline. Answering
                    # now with the real reason beats being cut off mid-retry.
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
                self._check_breaker(target)
        raise last

    async def _run_once(
        self, target: _Target, work: Callable[[Any], Any], *, timeout_seconds: float
    ) -> Any:
        cached = self._sessions.get(target.camera_id)
        if cached is not None and cached.fingerprint != target.fingerprint:
            await self._discard(target.camera_id)
            cached = None
        connecting = cached is None
        holder: list[Any] = []

        def job() -> Any:
            client = cached.client if cached is not None else _connect(target)
            holder.append(client)
            return work(client)

        try:
            result = await asyncio.wait_for(
                vendor_pool.run(job, max_workers=VENDOR_THREAD_LIMIT),
                timeout=timeout_seconds + (CONNECT_TIMEOUT_SECONDS if connecting else 0.0),
            )
        except TimeoutError as exc:
            # The worker thread cannot be cancelled and still owns that HTTPS
            # session, so the cached entry is dropped without being closed:
            # closing a socket another thread is mid-request on is worse than
            # letting the orphan finish and be collected.
            self._sessions.pop(target.camera_id, None)
            raise TapoUnavailableError(_UNAVAILABLE_MESSAGE) from exc
        except Exception as exc:  # noqa: BLE001 - vendor raises bare Exception
            error = _translate(exc)
            if isinstance(error, TapoUnsupportedError) and holder:
                # The session is healthy; only this model lacks the feature.
                self._store(target, holder[0])
            else:
                self._sessions.pop(target.camera_id, None)
            raise error from None

        if holder:
            self._store(target, holder[0])
        return result

    def _store(self, target: _Target, client: Any) -> None:
        self._sessions[target.camera_id] = _Session(
            client=client,
            fingerprint=target.fingerprint,
            last_used=_now(),
        )
        if len(self._sessions) <= SESSION_LIMIT:
            return
        oldest = min(self._sessions, key=lambda key: self._sessions[key].last_used)
        evicted = self._sessions.pop(oldest, None)
        if evicted is not None:
            with suppress(Exception):
                evicted.client.close()

    # -- breaker --

    def _check_breaker(self, target: _Target) -> None:
        breaker = self._breakers.get(target.camera_id)
        if breaker is None:
            return
        if breaker.opened_until > _now():
            raise TapoUnavailableError(_BREAKER_MESSAGE)
        if breaker.opened_until:
            # The cooldown elapsed: let exactly one call through and judge the
            # camera on that result rather than on its old failure count.
            breaker.opened_until = 0.0
            breaker.failures = 0

    def _record_failure(self, target: _Target, error: TapoError, operation: str) -> None:
        if isinstance(error, TapoUnsupportedError):
            return
        breaker = self._breakers.setdefault(target.camera_id, _Breaker())
        breaker.failures += 1
        if isinstance(error, TapoAuthError):
            breaker.opened_until = _now() + BREAKER_AUTH_COOLDOWN_SECONDS
        elif breaker.failures >= BREAKER_FAILURE_THRESHOLD:
            breaker.opened_until = _now() + BREAKER_COOLDOWN_SECONDS
        if not breaker.opened_until:
            return
        logger.warning(
            "Tapo local control suspended for this camera",
            extra={
                "camera_id": target.camera_id,
                "camera": target.camera_name,
                "operation": operation,
                # The safe message only -- vendor text carries the raw response.
                "reason": str(error),
            },
        )

    # -- persistence --

    def _known(self, session: AsyncSession, target: _Target) -> TapoCapabilities | None:
        """The last capability set confirmed for this exact target, if any."""
        del session  # kept for symmetry with the async persistence helpers
        cached = self._capabilities.get(target.camera_id)
        if cached is not None and cached.fingerprint == target.fingerprint:
            return cached.value
        return None

    async def _restore(self, session: AsyncSession, target: _Target) -> TapoCapabilities | None:
        """Adopt the persisted capability set so a restart does not re-probe."""
        stored = await settings_store.get_setting(session, CAPABILITIES_SETTING_KEY, {})
        entry = stored.get(target.camera_id) if isinstance(stored, dict) else None
        if not isinstance(entry, dict) or entry.get("fingerprint") != target.fingerprint:
            return None
        stored_at = entry.get("stored_at")
        if not isinstance(stored_at, int | float):
            return None
        if (utcnow().timestamp() - float(stored_at)) > PERSISTED_CAPABILITY_MAX_AGE_SECONDS:
            return None
        features = entry.get("features")
        if not isinstance(features, list):
            return None
        stored_device = entry.get("device")
        device: dict[str, Any] = stored_device if isinstance(stored_device, dict) else {}
        capabilities = TapoCapabilities(
            supported=frozenset(
                str(name) for name in features if str(name) in set(CAPABILITY_NAMES)
            ),
            device=TapoDeviceInfo(
                model=_short_text(device.get("model")),
                hardware_version=_short_text(device.get("hardware_version")),
                firmware_version=_short_text(device.get("firmware_version")),
            ),
            checked_at=datetime.fromtimestamp(float(stored_at), tz=utcnow().tzinfo),
            unavailable=tuple(
                sorted(str(name) for name in entry.get("unavailable", []) if isinstance(name, str))
            ),
        )
        self._capabilities[target.camera_id] = _CapabilityCache(
            value=capabilities,
            fingerprint=target.fingerprint,
            expires_at=_now() + CAPABILITY_TTL_SECONDS,
        )
        return capabilities

    async def _persist(
        self,
        session: AsyncSession,
        target: _Target,
        capabilities: TapoCapabilities,
    ) -> None:
        async with self._persistence_lock:
            stored = await settings_store.get_setting(session, CAPABILITIES_SETTING_KEY, {})
            if not isinstance(stored, dict):
                stored = {}
            existing = stored.get(target.camera_id)
            features = set(capabilities.supported)
            if capabilities.partial and isinstance(existing, dict):
                # Only a complete discovery may remove a feature from the
                # persisted record. A partial one adds what it confirmed.
                previous = existing.get("features")
                if existing.get("fingerprint") == target.fingerprint and isinstance(previous, list):
                    features |= {str(name) for name in previous}
            stored[target.camera_id] = {
                "fingerprint": target.fingerprint,
                "stored_at": utcnow().timestamp(),
                "features": sorted(features),
                "unavailable": sorted(capabilities.unavailable),
                "device": {
                    "model": capabilities.device.model,
                    "hardware_version": capabilities.device.hardware_version,
                    "firmware_version": capabilities.device.firmware_version,
                },
            }
            await settings_store.set_setting(session, CAPABILITIES_SETTING_KEY, stored)
            # Committed here rather than left to the request: the whole point
            # of the persisted copy is that it survives a restart that happens
            # next. The lock is intentionally held through commit so the next
            # writer's read sees this camera too.
            await session.commit()

    # -- target --

    @staticmethod
    def _target(camera: Camera) -> _Target:
        """Resolve credentials exactly as the PTZ patrol path already does."""
        password = try_decrypt_secret(camera.encrypted_password)
        tapo_password = try_decrypt_secret(camera.encrypted_tapo_password)
        user = "admin" if tapo_password else camera.username
        secret = tapo_password or password
        if not user or not secret:
            raise TapoUnsupportedError(_NOT_CONFIGURED_MESSAGE)
        return _Target(
            camera_id=camera.id,
            camera_name=camera.name,
            host=camera.host,
            user=user,
            password=secret,
            fingerprint=_fingerprint(camera.host, user, secret),
        )

    @staticmethod
    async def _ensure_local(target: _Target) -> None:
        """Refuse a target that is not on the local network, before connecting.

        The host always comes from the stored camera record and never from a
        request, and it still goes through the same guard discovery uses: a
        camera row pointed at a public address must not turn these routes into
        a generic outbound request tool.
        """
        try:
            await netsafety.ensure_scannable_host(target.host)
        except netsafety.UnscannableHostError as exc:
            raise TapoUnavailableError(_PRIVATE_HOST_MESSAGE) from exc


def _connect(target: _Target) -> Any:
    """Build one vendor client. Runs in a worker thread: this does network I/O.

    ``isKLAP=False`` skips a plaintext probe that costs up to two seconds per
    construction, and ``transportMethod`` is pinned so the client cannot fall
    back to the other transport on its own.
    """
    # Imported lazily so an ONVIF-only installation never initialises the
    # vendor client at process startup.
    from pytapo import Tapo

    return Tapo(
        host=target.host,
        user=target.user,
        password=target.password,
        controlPort=443,
        isKLAP=False,
        transportMethod="pytapo",
        printWarnInformation=False,
    )


_manager: TapoManager | None = None


def get_manager() -> TapoManager:
    global _manager
    if _manager is None:
        _manager = TapoManager()
    return _manager


def set_manager(manager: TapoManager | None) -> None:
    """Replace the process singleton in isolated tests."""
    global _manager
    _manager = manager
