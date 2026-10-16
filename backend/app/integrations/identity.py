"""A stable, non-secret identity for this Sentinel install.

Home Assistant keys its config entry on this value (``unique_id`` in the
handshake payload), so it has to survive a restart and an IP address change
without ever changing on its own -- and, since it is broadcast in the mDNS
TXT record in plaintext on the LAN, it must never expose anything about the
install secret it is derived from.

Generating a fresh random value at every start would make Home Assistant see
a "new" device every time Sentinel restarts. Using the raw install secret
itself is worse: that secret must never leave the process (see
``app.security.crypto`` and ``app.auth.tokens``, which derive their own
purpose-specific keys from it the same way this does). A one-way HKDF
derivation gets both properties: stable for the life of the install, and
safe to hand to anyone who asks.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

_INSTALL_ID_INFO = b"sentinel-cctv/mdns-install-id/v1"
_INSTALL_ID_BYTES = 16


def install_id() -> str:
    """A stable identifier for this install, safe to broadcast on the LAN.

    Not cached: the underlying secret read is a single small file read (itself
    cached by nothing, and cheap), and every caller in this process resolves
    the same settings singleton, so a fresh derivation each call is simpler
    than adding cache-invalidation machinery for a value that changes only
    when the install secret itself does.
    """
    secret = get_settings().load_or_create_secret()
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=_INSTALL_ID_BYTES,
        salt=None,
        info=_INSTALL_ID_INFO,
    ).derive(secret)
    return raw.hex()
