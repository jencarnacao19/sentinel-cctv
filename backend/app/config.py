"""Application settings.

Configuration is intentionally minimal: a normal user never edits any of this.
Everything that matters at runtime (storage path, retention, cameras) is stored
in the database and changed through the web interface. These settings only cover
process-level concerns that must be known before the database exists.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_PORT = 8090


class Settings(BaseSettings):
    """Process-level configuration, overridable through SENTINEL_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths -----------------------------------------------------------
    data_dir: Path = Field(default=Path("/data"))
    """Holds the database, secrets, thumbnails and snapshots."""

    recordings_dir: Path = Field(default=Path("/recordings"))
    """Default recordings root. The user can point this elsewhere at setup."""

    # --- HTTP ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Extra browser origins allowed to call the API. The bundled frontend is
    served from the same origin and never needs an entry here."""

    frame_ancestors: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Origins allowed to embed Sentinel, in addition to Sentinel itself.

    HTTPS is required except for an explicitly private/loopback HTTP host used
    by the Home Assistant app on the local network. Keeping origins explicit
    prevents arbitrary websites from framing an authenticated session.
    """

    enable_api_docs: bool = False
    """FastAPI /docs. Off by default in production, on in development."""

    # --- Auth ------------------------------------------------------------
    access_token_ttl_minutes: int = 60 * 12
    session_cookie_name: str = "sentinel_session"
    cookie_secure: bool = False
    """Set when Sentinel is served over HTTPS behind a reverse proxy."""

    trusted_proxy_cidrs: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Network addresses allowed to supply proxy identity headers.

    ``CF-Connecting-IP`` is security-sensitive: accepting it from an arbitrary
    LAN client would let that client forge another visitor's address and block
    it.  Public login protection therefore trusts the header only when the TCP
    peer is inside one of these explicitly configured networks.
    """

    # --- Read-only viewer ------------------------------------------------
    viewer_enabled: bool = False
    """Expose the deliberately small read-only viewer API.

    This never makes the administration API public. A viewer request needs a
    verified Cloudflare Access JWT on a trusted hostname or the separate viewer
    key used by an embedded Home Assistant dashboard.
    """

    viewer_trusted_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Hostnames whose viewer traffic is protected by Cloudflare Access.

    Sentinel also verifies the Access JWT's signature, issuer, audience and
    expiry. A hostname or assertion header alone never grants viewer access.
    """

    viewer_cloudflare_issuer: str | None = None
    """Access team origin, https://<team>.cloudflareaccess.com.

    Keys are fetched only from this configured origin's /cdn-cgi/access/certs,
    never from a URL supplied by a token. Unset disables the Cloudflare grant.
    """

    viewer_cloudflare_audiences: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Accepted Access application AUD tags. At least one is required."""

    viewer_key: SecretStr | None = None
    """Optional shared key for a read-only embedded viewer.

    It is intentionally separate from users and API tokens, cannot call any
    administration endpoint, and should only be supplied through deployment
    secrets.  A value shorter than 32 characters is rejected.
    """

    # --- go2rtc ----------------------------------------------------------
    go2rtc_host: str = "127.0.0.1"
    go2rtc_api_port: int = 1984
    go2rtc_rtsp_port: int = 8554
    go2rtc_webrtc_port: int = 8555
    go2rtc_binary: str = "go2rtc"
    manage_go2rtc: bool = True
    """When true Sentinel writes go2rtc.yaml and supervises the process."""

    # --- Media tooling ---------------------------------------------------
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    ffprobe_timeout_seconds: float = 15.0

    # --- Recording -------------------------------------------------------
    segment_seconds: int = 60

    record_via_go2rtc: bool = True
    """Read the recorder's input from go2rtc rather than the camera directly.

    Consumer cameras commonly cap concurrent RTSP clients at two or three, which
    the recorder, motion detection, the browser and Home Assistant reach
    immediately. Turn this off only when running an unmanaged go2rtc that
    Sentinel cannot reach over RTSP.
    """

    #: How often unindexed segments on disk are picked up.
    index_interval_seconds: float = 30.0

    # --- Motion ----------------------------------------------------------
    motion_fps: float = 5.0
    motion_width: int = 640
    motion_height: int = 360

    # --- Discovery -------------------------------------------------------
    enable_mdns: bool = True

    # --- Runtime ---------------------------------------------------------
    log_level: str = "INFO"
    timezone: str = "UTC"
    """Display timezone. Timestamps are always stored in UTC."""

    testing: bool = False

    @field_validator("data_dir", "recordings_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator(
        "cors_origins",
        "frame_ancestors",
        "viewer_trusted_hosts",
        "viewer_cloudflare_audiences",
        "trusted_proxy_cidrs",
        mode="before",
    )
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("viewer_cloudflare_issuer", mode="after")
    @classmethod
    def _validate_viewer_cloudflare_issuer(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        if (
            re.fullmatch(
                r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com",
                value,
            )
            is None
        ):
            raise ValueError("viewer_cloudflare_issuer must be https://<team>.cloudflareaccess.com")
        return value

    @field_validator("viewer_cloudflare_audiences", mode="after")
    @classmethod
    def _validate_viewer_cloudflare_audiences(cls, value: list[str]) -> list[str]:
        audiences = list(dict.fromkeys(entry.strip() for entry in value))
        if len(audiences) > 16 or any(not entry or len(entry) > 256 for entry in audiences):
            raise ValueError("viewer_cloudflare_audiences must contain up to 16 nonempty AUD tags")
        return audiences

    @field_validator("trusted_proxy_cidrs", mode="after")
    @classmethod
    def _validate_trusted_proxy_cidrs(cls, value: list[str]) -> list[str]:
        networks: list[str] = []
        for entry in value:
            try:
                networks.append(str(ipaddress.ip_network(entry, strict=False)))
            except ValueError as exc:
                raise ValueError(f"Invalid trusted proxy network: {entry}") from exc
        return networks

    @field_validator("frame_ancestors", mode="after")
    @classmethod
    def _validate_frame_ancestors(cls, value: list[str]) -> list[str]:
        validated: list[str] = []
        for origin in value:
            parsed = urlsplit(origin)
            hostname = parsed.hostname or ""
            local_http = False
            if parsed.scheme == "http":
                if hostname == "localhost" or hostname.endswith(".local"):
                    local_http = True
                else:
                    with contextlib.suppress(ValueError):
                        address = ipaddress.ip_address(hostname)
                        local_http = (
                            address.is_private or address.is_loopback or address.is_link_local
                        )
            if (
                (parsed.scheme != "https" and not local_http)
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "frame_ancestors entries must be HTTPS origins or private local HTTP origins"
                )
            validated.append(origin.rstrip("/"))
        return validated

    @field_validator("viewer_key", mode="before")
    @classmethod
    def _empty_viewer_key_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("viewer_key", mode="after")
    @classmethod
    def _validate_viewer_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("viewer_key must contain at least 32 characters")
        return value

    # --- Derived paths ---------------------------------------------------
    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "database" / "sentinel.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path.as_posix()}"

    @property
    def sync_database_url(self) -> str:
        """Used by Alembic, which runs migrations synchronously."""
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def secret_key_path(self) -> Path:
        return self.config_dir / "secret.key"

    @property
    def go2rtc_config_path(self) -> Path:
        return self.config_dir / "go2rtc.yaml"

    @property
    def go2rtc_api_url(self) -> str:
        return f"http://{self.go2rtc_host}:{self.go2rtc_api_port}"

    def ensure_directories(self) -> None:
        """Create the directories Sentinel owns. Never touches recordings_dir
        contents, only ensures the root exists."""
        for path in (
            self.data_dir,
            self.config_dir,
            self.database_path.parent,
            self.thumbnails_dir,
            self.snapshots_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def load_or_create_secret(self) -> bytes:
        """Return the local secret used to derive the credential encryption key.

        Generated once at install time and never leaves the machine.
        """
        path = self.secret_key_path
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32).hex().encode("ascii")
        path.write_bytes(secret)
        # Best effort: Windows and some network filesystems have no POSIX mode.
        with contextlib.suppress(OSError, NotImplementedError):
            path.chmod(0o600)
        return secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: forget the memoised settings instance."""
    get_settings.cache_clear()
