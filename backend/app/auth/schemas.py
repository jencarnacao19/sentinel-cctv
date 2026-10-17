"""Request and response models for authentication."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models import ApiTokenScope
from app.security.passwords import MIN_PASSWORD_LENGTH


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - scheme name, not a credential
    expires_at: datetime
    is_admin: bool
    """False for a viewer account, which belongs on the viewer and not in /admin."""


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    is_admin: bool
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime


class CreateAdminRequest(BaseModel):
    """Used once, by the first-run wizard."""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class CreateViewerAccountRequest(BaseModel):
    """An account that can only watch: cameras, recordings and saved clips."""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class LoginSecurityUpdate(BaseModel):
    enabled: bool
    failure_threshold: int = Field(ge=1, le=20)
    block_duration_seconds: int | None = Field(default=None, ge=60, le=365 * 24 * 60 * 60)


class LoginSecurityResponse(LoginSecurityUpdate):
    public_hostnames: list[str]
    trusted_proxy_cidrs: list[str]
    proxy_ready: bool
    blocked_count: int


class LoginBlockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ip_address: str
    public_hostname: str
    failed_attempts: int
    first_failed_at: datetime
    last_failed_at: datetime
    blocked_at: datetime
    blocked_until: datetime | None


class ClearLoginBlocksRequest(BaseModel):
    confirm: bool


class ApiTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    scope: ApiTokenScope = ApiTokenScope.READ_ONLY
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name must not be blank.")
        return stripped


class ApiTokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    scope: ApiTokenScope
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiTokenCreatedResponse(ApiTokenResponse):
    """Carries the plaintext token. Returned exactly once, at creation."""

    token: str
