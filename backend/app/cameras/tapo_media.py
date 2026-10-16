"""Standalone media authentication using the camera's advertised hash version.

The HTTP Digest algorithm is MD5, but its *password input* is SHA256 when
the camera advertises encrypt_type=3. This is independent of control port 443.
Keep pytapo's multipart/AES implementation; adapt only its handshake choice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import suppress

from pytapo.const import EncryptionMethod
from pytapo.media_stream._utils import pwd_digest
from pytapo.media_stream.session import HttpMediaSession


class StandaloneMediaSession(HttpMediaSession):
    async def _send_http_request(self, delimiter: bytes, headers: Mapping[bytes, bytes]) -> None:
        if b"Authorization" in headers:
            # pytapo parsed the unauthenticated challenge before this call.
            # Choose once, instead of making another wrong-password attempt
            # against firmware that explicitly told us its password version.
            auth = self._auth_data
            method = (
                EncryptionMethod.SHA256 if auth.get("encrypt_type") == "3" else EncryptionMethod.MD5
            )
            self.encryptionMethod = method
            self.hashed_password = pwd_digest(self.cloud_password.encode(), method).decode()

            def digest(value: str) -> str:
                # Required by the camera's HTTP Digest protocol, not password storage.
                return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()

            ha1 = digest(f"{self.username}:{auth['realm']}:{self.hashed_password}")
            ha2 = digest("POST:/stream")
            response = digest(
                f"{ha1}:{auth['nonce']}:{auth['nc']}:{auth['cnonce']}:{auth['qop']}:{ha2}"
            )
            self._authorization = (
                f'Digest username="{self.username}",realm="{auth["realm"]}",'
                f'uri="/stream",algorithm=MD5,nonce="{auth["nonce"]}",'
                f'nc={auth["nc"]},cnonce="{auth["cnonce"]}",qop={auth["qop"]},'
                f'response="{response}",opaque="{auth["opaque"]}"'
            ).encode()
            headers = {**headers, b"Authorization": self._authorization}
        await super()._send_http_request(delimiter, headers)

    async def close(self) -> None:
        # The upstream close only closes a *started* session. Cancellation
        # during start() must close the unauthenticated socket too.
        try:
            await super().close()
        finally:
            if self._writer is not None:
                self._writer.close()
                with suppress(OSError):
                    await self._writer.wait_closed()
