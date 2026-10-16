"""Encryption of camera credentials at rest.

Camera passwords must survive a restart (recording is unattended), so they
cannot be one-way hashed. They are encrypted with a key derived from a local
secret generated at install time and stored with 0600 permissions next to the
database. The plaintext is only ever decrypted in-process to build an FFmpeg or
go2rtc argument list -- it is never returned by the API or written to a log.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

_CREDENTIAL_INFO = b"sentinel-cctv/camera-credentials/v1"


class DecryptionError(RuntimeError):
    """Raised when a stored credential cannot be decrypted.

    In practice this means the local secret was lost or replaced -- the camera
    password has to be entered again.
    """


def _derive_key(secret: bytes, info: bytes) -> bytes:
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    ).derive(secret)
    return base64.urlsafe_b64encode(raw)


@lru_cache(maxsize=4)
def _fernet_for(secret: bytes) -> Fernet:
    return Fernet(_derive_key(secret, _CREDENTIAL_INFO))


def _fernet() -> Fernet:
    return _fernet_for(get_settings().load_or_create_secret())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a camera password. Returns an opaque, storable token."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a stored camera password."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise DecryptionError(
            "Stored credential could not be decrypted. The local secret may have "
            "changed; re-enter the camera password."
        ) from exc


def try_decrypt_secret(token: str | None) -> str | None:
    """Best-effort decryption used on hot paths that must not crash a worker."""
    if not token:
        return None
    try:
        return decrypt_secret(token)
    except DecryptionError:
        return None


def reset_key_cache() -> None:
    """Test helper: drop the derived-key cache."""
    _fernet_for.cache_clear()
