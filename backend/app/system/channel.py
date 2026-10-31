"""Moving the server between the stable and the dev build.

Sentinel never controls Docker itself: that would need the Docker socket, which
is root on the host. The administrator's choice is written as a request file
in the data directory; a small helper on the host
(``deploy/channel/sentinel-channel``) applies it with ``docker compose`` and
reports back through two more files there.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.auth.deps import AdminPrincipal
from app.config import get_settings

REQUEST_FILE = "update-request.json"
STATUS_FILE = "update-status.json"
HELPER_FILE = "update-helper.json"

Channel = Literal["stable", "dev"]


class ChannelState(BaseModel):
    current: Channel
    requested: Channel | None
    #: The helper's report of its last switch: channel, state, message, finished_at.
    last_result: dict[str, Any] | None
    #: When the helper last looked for a request; null means it is not installed.
    helper_seen_at: str | None


class ChannelRequest(BaseModel):
    channel: Channel
    #: The channel name typed again in capitals, the extra confirmation.
    confirm: str


router = APIRouter(prefix="/system/channel", tags=["system"])


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _state() -> ChannelState:
    settings = get_settings()
    request = _read(settings.data_dir / REQUEST_FILE)
    helper = _read(settings.data_dir / HELPER_FILE)
    requested = request.get("channel") if request else None
    seen_at = helper.get("seen_at") if helper else None
    return ChannelState(
        current=settings.build_channel,
        requested=requested if requested in ("stable", "dev") else None,
        last_result=_read(settings.data_dir / STATUS_FILE),
        helper_seen_at=seen_at if isinstance(seen_at, str) else None,
    )


@router.get("", response_model=ChannelState, summary="Which build runs, and any switch")
async def channel_state(_: AdminPrincipal) -> ChannelState:
    return _state()


@router.post(
    "",
    response_model=ChannelState,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask the host helper to switch build",
)
async def request_channel(payload: ChannelRequest, _: AdminPrincipal) -> ChannelState:
    if payload.confirm != payload.channel.upper():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Type {payload.channel.upper()} to confirm the switch.",
        )
    settings = get_settings()
    if payload.channel == settings.build_channel:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This server already runs the {payload.channel} build.",
        )
    target = settings.data_dir / REQUEST_FILE
    partial = target.with_suffix(".tmp")
    partial.write_text(
        json.dumps({"channel": payload.channel, "requested_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )
    # The helper runs as root on the host but only needs to read it.
    partial.chmod(0o644)
    os.replace(partial, target)
    return _state()
