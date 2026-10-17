"""Moving an installation to another machine with one YAML file.

Unlike a backup, which restores a whole database onto the same kind of
install, this file is meant to be read and carried: every camera with its
streams, recording and motion settings and named zones, plus the handful of
settings that describe how footage is kept. It deliberately leaves out what
belongs to one machine (the storage path, users, API tokens, recorded
footage) and what is only state (privacy mode, login blocks).

Camera passwords are optional. When they are included they are sealed with
AES-GCM under a key derived from a passphrase the administrator chooses
(scrypt), so the file can travel through e-mail or a USB stick without the
passwords being readable. Without a passphrase they are simply left out and
re-entered after the import.

Importing goes through the same validation as the API: a camera the file
describes is created with :func:`app.cameras.service.create_camera`, its zones
with the zone editor's own model, and a storage policy that could delete
footage is never applied without the review the Storage page requires.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.cameras import service as camera_service
from app.cameras.schemas import CameraCreate
from app.database.models import Camera, CameraKind
from app.logging_config import get_logger
from app.motion.zones import parse_zones, serialise_zones
from app.security.crypto import try_decrypt_secret
from app.storage import policy_preview
from app.system import settings_store

logger = get_logger("SYSTEM")

FORMAT = "sentinel-config"
VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CAMERAS = 256
MIN_PASSPHRASE_LENGTH = 8

# scrypt at N=2^15 takes a fraction of a second here and makes guessing a
# passphrase against a stolen file expensive.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_CHECK_PLAINTEXT = b"sentinel-config"

_CAMERA_FIELDS = (
    "kind",
    "host",
    "manufacturer",
    "model",
    "nvr_group",
    "main_stream_url",
    "sub_stream_url",
    "username",
    "tapo_child_id",
    "onvif_device_xaddr",
    "onvif_profile_token",
    "enabled",
    "recording_mode",
    "recording_profile",
    "recording_source_mode",
    "recording_segment_seconds",
    "recording_enabled",
    "retention_days",
    "motion_enabled",
    "motion_sensitivity",
    "tapo_patrol_mode",
    "tapo_controls_enabled",
)
_SECRET_FIELDS = (("password", "encrypted_password"), ("tapo_password", "encrypted_tapo_password"))
#: For an RTSP camera these come from its stream URL, and the API refuses them
#: given separately.
_RTSP_DERIVED = frozenset({"host", "tapo_child_id"})

_HEADER = (
    "# Sentinel CCTV configuration: cameras, motion zones and settings.\n"
    "# Import it on another Sentinel under Settings > Move to another machine.\n"
    "# Recordings, users and API tokens are not included.\n"
)


class ConfigTransferError(ValueError):
    """The file cannot be imported at all; the message says why."""


@dataclass(slots=True)
class ImportSummary:
    cameras_added: list[str] = field(default_factory=list)
    cameras_skipped: list[dict[str, str]] = field(default_factory=list)
    settings_applied: list[str] = field(default_factory=list)
    settings_skipped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passwords_restored: bool = False


# --- Export ------------------------------------------------------------------


async def export_config(session: AsyncSession, *, passphrase: str | None) -> str:
    """The whole configuration as YAML; camera passwords only with a passphrase."""
    sealer: _Sealer | None = None
    document: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sentinel_version": __version__,
        "passwords": "omitted",
    }
    if passphrase is not None:
        _check_passphrase(passphrase)
        sealer = _Sealer.new(passphrase)
        document["passwords"] = "encrypted"
        document["encryption"] = sealer.header()

    document["settings"] = await _export_settings(session)
    cameras = await camera_service.list_cameras(session)
    document["cameras"] = [_export_camera(camera, sealer) for camera in cameras]

    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=4096)
    logger.info(
        "Configuration exported",
        extra={"cameras": len(cameras), "passwords": document["passwords"]},
    )
    return _HEADER + body


def _export_camera(camera: Camera, sealer: _Sealer | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": camera.name}
    for name in _CAMERA_FIELDS:
        if name in _RTSP_DERIVED and camera.kind is CameraKind.RTSP:
            continue
        value = getattr(camera, name)
        if value is not None:
            entry[name] = value.value if hasattr(value, "value") else value
    if sealer is not None:
        for field_name, column in _SECRET_FIELDS:
            secret = try_decrypt_secret(getattr(camera, column))
            if secret:
                entry[field_name] = sealer.seal(secret, field_name)
    entry["motion_zones"] = [zone.to_dict() for zone in parse_zones(camera.motion_zones)]
    return entry


async def _export_settings(session: AsyncSession) -> dict[str, Any]:
    values = await settings_store.get_many(
        session,
        [
            settings_store.TIMEZONE,
            settings_store.SEGMENT_SECONDS,
            settings_store.MOTION_PRE_ROLL_SECONDS,
            settings_store.MOTION_POST_ROLL_SECONDS,
            settings_store.MOTION_END_GRACE_SECONDS,
            settings_store.STORAGE_RETENTION_DAYS,
            settings_store.STORAGE_MAX_BYTES,
            settings_store.STORAGE_MIN_FREE_BYTES,
        ],
    )
    return {
        "timezone": values[settings_store.TIMEZONE],
        "recording": {"segment_seconds": values[settings_store.SEGMENT_SECONDS]},
        "motion": {
            "pre_roll_seconds": values[settings_store.MOTION_PRE_ROLL_SECONDS],
            "post_roll_seconds": values[settings_store.MOTION_POST_ROLL_SECONDS],
            "end_grace_seconds": values[settings_store.MOTION_END_GRACE_SECONDS],
        },
        "storage": {
            "retention_days": values[settings_store.STORAGE_RETENTION_DAYS],
            "max_bytes": values[settings_store.STORAGE_MAX_BYTES],
            "min_free_bytes": values[settings_store.STORAGE_MIN_FREE_BYTES],
        },
    }


# --- Import ------------------------------------------------------------------


def read_document(raw: bytes) -> dict[str, Any]:
    """Parse and sanity-check an uploaded file before anything is changed."""
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigTransferError("That file is too large to be a Sentinel configuration.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigTransferError("That file is not a text file.") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigTransferError("That file is not valid YAML.") from exc
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ConfigTransferError("That file is not a Sentinel configuration export.")
    if document.get("version") != VERSION:
        raise ConfigTransferError(
            "That file was written by a different version of Sentinel's export format."
        )
    cameras = document.get("cameras", [])
    if not isinstance(cameras, list) or len(cameras) > MAX_CAMERAS:
        raise ConfigTransferError("The camera list in that file cannot be read.")
    return document


def has_encrypted_passwords(document: dict[str, Any]) -> bool:
    return document.get("passwords") == "encrypted"


async def import_config(
    session: AsyncSession, document: dict[str, Any], *, passphrase: str | None
) -> ImportSummary:
    """Add the file's cameras and apply its settings. Existing cameras are kept."""
    summary = ImportSummary()
    opener: _Sealer | None = None
    if has_encrypted_passwords(document):
        if not passphrase:
            raise ConfigTransferError(
                "This file includes encrypted camera passwords. Enter the passphrase "
                "chosen when it was exported."
            )
        opener = _Sealer.from_header(document.get("encryption"), passphrase)
        summary.passwords_restored = True

    existing = {camera.name.casefold() for camera in await camera_service.list_cameras(session)}
    for entry in document.get("cameras", []):
        await _import_camera(session, entry, opener, existing, summary)

    settings = document.get("settings")
    if isinstance(settings, dict):
        await _import_settings(session, settings, summary)

    logger.info(
        "Configuration imported",
        extra={
            "added": len(summary.cameras_added),
            "skipped": len(summary.cameras_skipped),
            "passwords": summary.passwords_restored,
        },
    )
    return summary


async def _import_camera(
    session: AsyncSession,
    entry: Any,
    opener: _Sealer | None,
    existing: set[str],
    summary: ImportSummary,
) -> None:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        summary.cameras_skipped.append({"name": "?", "reason": "This entry has no name."})
        return
    name = entry["name"]
    if name.casefold() in existing:
        summary.cameras_skipped.append({"name": name, "reason": "A camera with this name exists."})
        return
    if entry.get("kind", CameraKind.RTSP.value) not in {kind.value for kind in CameraKind}:
        summary.cameras_skipped.append(
            {"name": name, "reason": "This Sentinel does not support that kind of camera."}
        )
        return

    fields: dict[str, Any] = {"name": name}
    fields.update({key: entry[key] for key in _CAMERA_FIELDS if key in entry})
    if fields.get("kind", CameraKind.RTSP.value) == CameraKind.RTSP.value:
        for key in _RTSP_DERIVED:
            fields.pop(key, None)
    if opener is not None:
        for field_name, _column in _SECRET_FIELDS:
            if field_name in entry:
                fields[field_name] = opener.open(entry[field_name], field_name)

    try:
        payload = CameraCreate.model_validate(fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        summary.cameras_skipped.append({"name": name, "reason": str(first.get("msg"))})
        return
    try:
        # Every refusal create_camera can give is raised before it adds the
        # row; the one that is not (a duplicate name) was ruled out above.
        camera = await camera_service.create_camera(session, payload)
    except camera_service.CameraError as exc:
        summary.cameras_skipped.append({"name": name, "reason": str(exc)})
        return

    zones = entry.get("motion_zones")
    if zones:
        from app.motion.router import MotionZonesUpdate

        try:
            update = MotionZonesUpdate.model_validate({"zones": zones})
        except ValidationError:
            summary.warnings.append(
                f"The motion zones of {name} could not be read and were left out."
            )
        else:
            camera.motion_zones = serialise_zones([zone.to_zone() for zone in update.zones])

    existing.add(name.casefold())
    summary.cameras_added.append(name)


async def _import_settings(
    session: AsyncSession, settings: dict[str, Any], summary: ImportSummary
) -> None:
    timezone = settings.get("timezone")
    if isinstance(timezone, str):
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            summary.settings_skipped.append({"name": "timezone", "reason": "Unknown time zone."})
        else:
            await settings_store.set_setting(session, settings_store.TIMEZONE, timezone)
            summary.settings_applied.append("timezone")

    simple = (
        (("recording", "segment_seconds"), settings_store.SEGMENT_SECONDS, 10, 300),
        (("motion", "pre_roll_seconds"), settings_store.MOTION_PRE_ROLL_SECONDS, 0, 60),
        (("motion", "post_roll_seconds"), settings_store.MOTION_POST_ROLL_SECONDS, 0, 120),
        (("motion", "end_grace_seconds"), settings_store.MOTION_END_GRACE_SECONDS, 1, 120),
    )
    for (group, key), store_key, low, high in simple:
        section = settings.get(group)
        value = section.get(key) if isinstance(section, dict) else None
        if value is None:
            continue
        label = f"{group}.{key}"
        numeric = isinstance(value, int | float) and not isinstance(value, bool)
        if not numeric or not low <= value <= high:
            summary.settings_skipped.append({"name": label, "reason": "Out of range."})
            continue
        await settings_store.set_setting(session, store_key, value)
        summary.settings_applied.append(label)

    storage = settings.get("storage")
    if isinstance(storage, dict):
        await _import_storage_policy(session, storage, summary)


async def _import_storage_policy(
    session: AsyncSession, storage: dict[str, Any], summary: ImportSummary
) -> None:
    keys = {
        "retention_days": settings_store.STORAGE_RETENTION_DAYS,
        "max_bytes": settings_store.STORAGE_MAX_BYTES,
        "min_free_bytes": settings_store.STORAGE_MIN_FREE_BYTES,
    }
    current_values = await settings_store.get_many(session, list(keys.values()))
    current = {name: current_values[key] for name, key in keys.items()}
    proposed = dict(current)
    for name in keys:
        if name not in storage:
            continue
        value = storage[name]
        valid = value is None or (
            isinstance(value, int)
            and not isinstance(value, bool)
            and (
                (name == "retention_days" and 1 <= value <= 3650)
                or (name == "max_bytes" and value >= 1024**3)
                or (name == "min_free_bytes" and value >= 0)
            )
        )
        if not valid:
            summary.settings_skipped.append({"name": f"storage.{name}", "reason": "Out of range."})
            return
        proposed[name] = value
    if proposed == current:
        return
    if policy_preview.needs_review(current, proposed):
        # A tighter policy can delete footage; that always goes through the
        # preview and typed confirmation on the Storage page.
        summary.settings_skipped.append(
            {
                "name": "storage",
                "reason": (
                    "This storage policy could delete footage. Review it on the Storage page."
                ),
            }
        )
        return
    for name, key in keys.items():
        if proposed[name] != current[name]:
            await settings_store.set_setting(session, key, proposed[name])
            summary.settings_applied.append(f"storage.{name}")


# --- Passwords ---------------------------------------------------------------


def _check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ConfigTransferError(
            f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters."
        )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ConfigTransferError("The encrypted passwords in that file are damaged.")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigTransferError("The encrypted passwords in that file are damaged.") from exc


class _Sealer:
    """AES-GCM under a scrypt key; each value bound to the field it belongs to."""

    def __init__(self, key: bytes, salt: bytes, check: dict[str, str] | None = None) -> None:
        self._aead = AESGCM(key)
        self._salt = salt
        self._check = check or self._seal(_CHECK_PLAINTEXT, "check")

    @classmethod
    def new(cls, passphrase: str) -> _Sealer:
        salt = secrets.token_bytes(16)
        return cls(_derive(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P), salt)

    @classmethod
    def from_header(cls, header: Any, passphrase: str) -> _Sealer:
        if not isinstance(header, dict) or header.get("kdf") != "scrypt":
            raise ConfigTransferError("The encrypted passwords in that file are damaged.")
        try:
            n, r, p = int(header["n"]), int(header["r"]), int(header["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigTransferError("The encrypted passwords in that file are damaged.") from exc
        if not (2**14 <= n <= 2**20 and 1 <= r <= 16 and 1 <= p <= 4):
            raise ConfigTransferError("The encrypted passwords in that file are damaged.")
        check = header.get("check")
        if not isinstance(check, dict):
            raise ConfigTransferError("The encrypted passwords in that file are damaged.")
        sealer = cls(_derive(passphrase, _unb64(header.get("salt")), n, r, p), b"", check)
        if sealer.open(check, "check", raw=True) != _CHECK_PLAINTEXT.decode():
            raise ConfigTransferError("That passphrase does not open this file.")
        return sealer

    def header(self) -> dict[str, Any]:
        return {
            "kdf": "scrypt",
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "salt": _b64(self._salt),
            "check": self._check,
        }

    def seal(self, value: str, field_name: str) -> dict[str, str]:
        return self._seal(value.encode("utf-8"), field_name)

    def _seal(self, plaintext: bytes, field_name: str) -> dict[str, str]:
        nonce = secrets.token_bytes(12)
        sealed = self._aead.encrypt(nonce, plaintext, field_name.encode("ascii"))
        return {"nonce": _b64(nonce), "ciphertext": _b64(sealed)}

    def open(self, sealed: Any, field_name: str, *, raw: bool = False) -> str:
        if not isinstance(sealed, dict):
            raise ConfigTransferError("The encrypted passwords in that file are damaged.")
        try:
            plaintext = self._aead.decrypt(
                _unb64(sealed.get("nonce")),
                _unb64(sealed.get("ciphertext")),
                field_name.encode("ascii"),
            )
        except InvalidTag as exc:
            if raw:
                raise ConfigTransferError("That passphrase does not open this file.") from exc
            raise ConfigTransferError("The encrypted passwords in that file are damaged.") from exc
        return plaintext.decode("utf-8")


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))
