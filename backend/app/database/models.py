"""ORM models.

Models are introduced phase by phase alongside the Alembic revision that creates
their tables, so every schema change is reversible and no upgrade path requires
deleting the database.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, new_uuid, utcnow


class RecordingMode(StrEnum):
    """How a camera is recorded. ``CONTINUOUS`` is the default and the point of
    the product: motion-only recording always misses the moment before it
    triggers."""

    CONTINUOUS = "continuous"
    MOTION = "motion"
    SCHEDULED = "scheduled"
    EVENTS = "events"
    """Provider-owned event clips; no continuous recorder or motion worker."""
    OFF = "off"


class CameraStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    CONNECTING = "connecting"
    ERROR = "error"
    DISABLED = "disabled"


class CameraKind(StrEnum):
    """The transport contract a camera exposes to Sentinel."""

    RTSP = "rtsp"
    TAPO_BATTERY = "tapo_battery"
    """Event clips plus an on-demand proprietary LAN stream; never continuous."""


class SegmentStatus(StrEnum):
    """Lifecycle of one recording file.

    ``DAMAGED`` and ``MISSING`` exist so a bad file is recorded as a known fact
    rather than silently skipped: a gap in the timeline should be visible.
    """

    RECORDING = "recording"
    """Still being written by FFmpeg."""
    COMPLETE = "complete"
    DAMAGED = "damaged"
    """Unreadable, typically a segment interrupted by a power cut."""
    MISSING = "missing"
    """Indexed but the file is no longer on disk."""


class SegmentOrigin(StrEnum):
    """How a playable file entered Sentinel's recording index."""

    CONTINUOUS = "continuous"
    TAPO_EVENT = "tapo_event"
    TAPO_MANUAL = "tapo_manual"
    """A bounded, explicitly requested Tapo battery-camera recording."""


class IncidentReason(StrEnum):
    """Why a camera stopped operating normally.

    A closed, sanitised vocabulary. Nothing derived from a camera URL, host,
    credential or raw FFmpeg output is ever stored as a reason: an incident row
    is meant to be readable in a diagnostics bundle without leaking anything.
    """

    RECORDING_NOT_FRESH = "recording_not_fresh"
    """The recorder is alive but no new completed segment has appeared."""
    RECORDER_RESTARTING = "recorder_restarting"
    RECORDER_UNAVAILABLE = "recorder_unavailable"
    """Recording is configured but no recorder process is supervised."""
    STREAM_SOURCE_FAILURE = "stream_source_failure"
    MOTION_WORKER_UNAVAILABLE = "motion_worker_unavailable"
    RELAY_UNAVAILABLE = "relay_unavailable"
    """go2rtc is expected but is not answering, so live view is impaired."""
    STORAGE_UNAVAILABLE = "storage_unavailable"
    RECORDING_COVERAGE_LOW = "recording_coverage_low"
    """Segments keep arriving, but they do not add up to continuous footage.

    The failure a freshness check cannot see: a recorder that writes a short
    segment every couple of minutes looks alive and recent while most of the
    hour was never recorded at all.
    """
    RECORDING_UNSTABLE = "recording_unstable"
    """The recorder keeps restarting, so footage is repeatedly interrupted."""
    STREAM_CORRUPTION = "stream_corruption"
    """The source is delivering damaged media faster than a threshold allows."""


class StreamFault(StrEnum):
    """What a recorder's last failure looked like, as a fixed label.

    Derived by classifying the supervisor's already-redacted last line of
    FFmpeg output. Only the label is kept: the original text is never stored,
    so an incident cannot grow into an unbounded log of camera output.
    """

    MISSING_STREAM_PARAMETERS = "missing_stream_parameters"
    """No SPS/PPS or usable dimensions, so the muxer could not be opened."""
    CORRUPT_PACKETS = "corrupt_packets"
    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    AUTHENTICATION_REJECTED = "authentication_rejected"
    UNKNOWN = "unknown"


class RecordingProfile(StrEnum):
    """How a camera's bitstream is handled on the way to disk.

    The default is ``AUTO``, which starts as a plain stream copy and escalates
    to ``RESILIENT`` only when Sentinel has measured corruption on that camera.
    ``NORMALISED`` decodes and re-encodes and is never selected automatically:
    on a four-core host it costs most of a core per camera, which
    is a trade an administrator makes deliberately, not one a heuristic makes
    at three in the morning.
    """

    AUTO = "auto"
    PASSTHROUGH = "passthrough"
    """Stream copy exactly as the camera sent it. Cheapest, least forgiving."""
    RESILIENT = "resilient"
    """Stream copy that drops corrupt packets and repairs timestamps."""
    NORMALISED = "normalised"
    """Decode and re-encode to browser-safe H.264 with a predictable GOP."""


class RecordingSourceMode(StrEnum):
    """How the recorder chooses between the local relay and a direct RTSP session."""

    AUTO = "auto"
    RELAY = "relay"
    """Stay on go2rtc even after short camera-driven session closures."""
    DIRECT = "direct"
    """Bypass go2rtc. Intended only for a measured relay-specific problem."""


class RecorderSource(StrEnum):
    """Which input a recorder is reading from.

    Deliberately not a URL: the interface only ever needs to know whether the
    shared local relay or a direct camera connection is in use.
    """

    RELAY = "relay"
    DIRECT = "direct"


class ExportStatus(StrEnum):
    """Lifecycle of one export job.

    ``EXPIRED`` is separate from ``FAILED`` on purpose: a download link that has
    aged out is not an error, and telling the user to re-export is a different
    message from telling them something broke.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    EXPIRED = "expired"
    """The file was cleaned up after its retention window."""
    CANCELLED = "cancelled"


class User(Base):
    """A local operator account. There is no cloud identity of any kind."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)

    tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ApiTokenScope(StrEnum):
    READ_ONLY = "read_only"
    CAMERA_CONTROL = "camera_control"


class ApiToken(Base):
    """A long-lived token for machine clients -- in practice, Home Assistant.

    Only the hash is stored. The plaintext is shown once, at creation.
    """

    __tablename__ = "api_tokens"
    __table_args__ = (
        Index("ix_api_tokens_prefix", "prefix"),
        UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        CheckConstraint("scope IN ('read_only', 'camera_control')", name="api_token_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ApiTokenScope.READ_ONLY, server_default="read_only"
    )
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    """Non-secret leading characters, so the UI can identify a token in a list."""
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    user: Mapped[User | None] = relationship(back_populates="tokens")

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= utcnow())


class AppSetting(Base):
    """Key/value store for runtime configuration edited through the UI.

    Kept deliberately generic: storage paths, retention policy and setup state
    all live here, so adding a setting never needs a migration.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    """JSON-encoded. Typed access goes through app.system.settings_store."""
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)


class LoginClientState(Base):
    """Failed public administrator sign-ins, keyed by canonical client IP.

    Rows below the configured threshold remember consecutive failures.  A
    non-null ``blocked_at`` means the address is blocked; ``blocked_until`` is
    null for a permanent block.  Successful sign-in removes a pending row.
    """

    __tablename__ = "login_client_states"
    __table_args__ = (
        CheckConstraint("failed_attempts >= 0", name="login_client_failed_attempts"),
        Index("ix_login_client_states_blocked_at", "blocked_at"),
    )

    ip_address: Mapped[str] = mapped_column(String(45), primary_key=True)
    public_hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    last_failed_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    blocked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)


class Camera(Base):
    """A single video source.

    An NVR channel is a camera in its own right: it has its own RTSP URL, its own
    recording settings and its own retention. ``nvr_group`` only ties the
    channels together for display.
    """

    __tablename__ = "cameras"
    __table_args__ = (
        CheckConstraint(
            "recording_segment_seconds IS NULL OR recording_segment_seconds BETWEEN 10 AND 300",
            name="recording_segment_seconds",
        ),
        Index("ix_cameras_enabled", "enabled"),
        UniqueConstraint("name", name="uq_cameras_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(96), nullable=False)

    kind: Mapped[CameraKind] = mapped_column(
        Enum(
            CameraKind,
            values_callable=lambda members: [member.value for member in members],
            native_enum=False,
            length=24,
        ),
        nullable=False,
        default=CameraKind.RTSP,
        server_default=CameraKind.RTSP.value,
    )

    manufacturer: Mapped[str | None] = mapped_column(String(96), nullable=True)
    model: Mapped[str | None] = mapped_column(String(96), nullable=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    nvr_group: Mapped[str | None] = mapped_column(String(96), nullable=True)
    """Display grouping for channels that came from the same NVR."""

    #: Stored without credentials. The password is re-attached only when an
    #: FFmpeg or go2rtc argument list is built.
    main_stream_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    sub_stream_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Fernet token. Never leaves the process in plaintext."""

    encrypted_tapo_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Optional Tapo account password for LAN control. Encrypted and never returned."""

    tapo_child_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    tapo_controls_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Administrator's explicit answer to "is this a Tapo camera?".

    Tri-state on purpose. ``None`` means nobody has said, so Sentinel decides
    from the camera's own metadata; ``True`` and ``False`` are an operator
    overriding that decision for a device whose metadata is missing or wrong.
    A camera's address is never part of that decision -- several unrelated
    channels routinely share one NVR host.
    """
    """Optional child-device id when the battery camera is reached through a local hub."""

    onvif_device_xaddr: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    """ONVIF device service selected during discovery. Never contains credentials."""
    onvif_ptz_xaddr: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    """Validated PTZ service address discovered from the device capabilities."""
    onvif_profile_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Media profile token used by ONVIF PTZ commands."""
    ptz_supported: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Three states: supported, unsupported, or not checked yet."""
    ptz_zoom_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ptz_status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ptz_last_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    tapo_patrol_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """Tapo-only cruise direction to restore after manual PTZ: horizontal/vertical."""
    onvif_patrol_tour_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Camera-owned ONVIF preset tour. Preferred over proprietary vendor control."""

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recording_mode: Mapped[RecordingMode] = mapped_column(
        Enum(RecordingMode, native_enum=False, length=16),
        nullable=False,
        default=RecordingMode.CONTINUOUS,
    )
    recording_profile: Mapped[RecordingProfile] = mapped_column(
        Enum(
            RecordingProfile,
            values_callable=lambda members: [member.value for member in members],
            native_enum=False,
            length=16,
        ),
        nullable=False,
        default=RecordingProfile.AUTO,
        server_default=RecordingProfile.AUTO.value,
    )
    """How this camera's bitstream is handled. See :class:`RecordingProfile`."""

    recording_source_mode: Mapped[RecordingSourceMode] = mapped_column(
        Enum(
            RecordingSourceMode,
            values_callable=lambda members: [member.value for member in members],
            native_enum=False,
            length=8,
        ),
        nullable=False,
        default=RecordingSourceMode.AUTO,
        server_default=RecordingSourceMode.AUTO.value,
    )
    """Automatic failover, or an explicit local relay/direct pin."""

    recording_segment_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    recording_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    """Whether this camera's own recorder may run.

    Separate from ``enabled`` (which also removes live view and ONVIF) and from
    ``recording_mode`` (which describes *how* it records). Turning this off
    stops one recorder and nothing else; turning it back on restarts that
    recorder with the profile, source and segment length already configured.
    """
    """Optional per-camera segment duration; null inherits the system default."""

    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Overrides the global retention policy when set."""

    motion_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    motion_sensitivity: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    """0-100 as shown on the slider; mapped to detector thresholds in phase 6."""
    motion_zones: Mapped[str | None] = mapped_column(Text, nullable=True)
    """JSON array of normalised polygons to ignore. Populated in phase 6."""

    status: Mapped[CameraStatus] = mapped_column(
        Enum(CameraStatus, native_enum=False, length=16),
        nullable=False,
        default=CameraStatus.OFFLINE,
    )
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: Last successful probe, so the interface can show codec and resolution
    #: without re-opening the stream every time the page loads.
    codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)

    segments: Mapped[list[RecordingSegment]] = relationship(
        back_populates="camera",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    motion_events: Mapped[list[MotionEvent]] = relationship(
        back_populates="camera",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    incidents: Mapped[list[CameraIncident]] = relationship(
        back_populates="camera",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def stream_name(self) -> str:
        """Stable identifier used for the go2rtc stream and the recordings path."""
        return f"cam_{self.id.replace('-', '')}"


class RecordingSegment(Base):
    """One recorded file.

    Recordings are stored as short segments rather than one file per day: a
    power cut can then cost at most one segment, and retention can delete the
    oldest footage without rewriting anything. The database -- not the filename
    -- is the index; the path layout is a convenience for humans reading the
    disk directly.
    """

    __tablename__ = "recording_segments"
    __table_args__ = (
        # The timeline query is always "this camera, this time range", and
        # retention is always "oldest first, unprotected".
        Index("ix_segments_camera_started", "camera_id", "started_at"),
        Index("ix_segments_started", "started_at"),
        Index("ix_segments_protected", "protected"),
        UniqueConstraint("path", name="uq_recording_segments_path"),
        UniqueConstraint("camera_id", "external_id", name="uq_recording_segments_camera_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )

    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    """Relative to the storage root, so moving the storage does not invalidate
    every row."""

    origin: Mapped[SegmentOrigin] = mapped_column(
        Enum(
            SegmentOrigin,
            values_callable=lambda members: [member.value for member in members],
            native_enum=False,
            length=16,
        ),
        nullable=False,
        default=SegmentOrigin.CONTINUOUS,
        server_default=SegmentOrigin.CONTINUOUS.value,
    )
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    """Provider-stable clip key used to make imports idempotent."""

    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(nullable=True)

    has_motion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    motion_seconds: Mapped[float] = mapped_column(nullable=False, default=0.0)

    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Protected segments are never removed by retention."""

    status: Mapped[SegmentStatus] = mapped_column(
        Enum(SegmentStatus, native_enum=False, length=16),
        nullable=False,
        default=SegmentStatus.COMPLETE,
    )

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    camera: Mapped[Camera] = relationship(back_populates="segments")

    @property
    def is_playable(self) -> bool:
        return self.status in (SegmentStatus.COMPLETE, SegmentStatus.RECORDING)


class MotionEvent(Base):
    """A period of movement on one camera.

    One event covers a whole incident, not a frame: a person crossing the view
    produces one row, not forty. That aggregation is the difference between a
    usable events list and an unreadable one.
    """

    __tablename__ = "motion_events"
    __table_args__ = (
        Index("ix_motion_camera_started", "camera_id", "started_at"),
        Index("ix_motion_started", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """Null while the movement is still happening."""
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)

    peak_score: Mapped[float] = mapped_column(nullable=False, default=0.0)
    """Highest fraction of the frame in motion, 0.0-1.0."""

    #: Playback bounds including roll, so the clip starts before the movement
    #: rather than at the instant it was already underway.
    pre_roll_start: Mapped[datetime] = mapped_column(nullable=False)
    post_roll_end: Mapped[datetime | None] = mapped_column(nullable=True)

    snapshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    camera: Mapped[Camera] = relationship(back_populates="motion_events")

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


class CameraIncident(Base):
    """One period during which a camera was not operating as configured.

    Incidents exist because the interesting question is almost never "is this
    camera fine right now?" but "did it stop overnight, for how long, and did
    it come back on its own?". A supervisor restart that recovers in thirty
    seconds is invisible in a live status view and obvious here.

    Rows are opened only after a fault has persisted through a debounce window
    and closed only after recovery has been sustained, so a camera that
    reconnects every few minutes produces one incident rather than hundreds.
    Retention is bounded per camera and by age; see ``app.reliability.service``.
    """

    __tablename__ = "camera_incidents"
    __table_args__ = (
        # The two access patterns: one camera's history, and "what is open
        # right now" across every camera.
        Index("ix_camera_incidents_camera_started", "camera_id", "started_at"),
        Index("ix_camera_incidents_started", "started_at"),
        Index("ix_camera_incidents_resolved", "resolved_at"),
        # SQLite treats NULL values as distinct in a normal unique index, so a
        # partial index is required to guarantee the invariant the monitor
        # relies on: at most one unresolved incident per camera.
        Index(
            "uq_camera_incidents_open_camera",
            "camera_id",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )

    reason: Mapped[IncidentReason] = mapped_column(
        Enum(IncidentReason, native_enum=False, length=32), nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """Null while the incident is still open. This is what "active" means."""
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)

    detail: Mapped[str | None] = mapped_column(String(240), nullable=True)
    """A short sentence Sentinel composed itself, never captured output."""

    fault: Mapped[StreamFault | None] = mapped_column(
        Enum(StreamFault, native_enum=False, length=32), nullable=True
    )
    source: Mapped[RecorderSource | None] = mapped_column(
        Enum(RecorderSource, native_enum=False, length=16), nullable=True
    )
    """Recorder input at the moment the incident opened or last changed."""

    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Supervisor restarts observed by the monitor during this incident."""

    coverage_ratio: Mapped[float | None] = mapped_column(nullable=True)
    """Worst measured recorded fraction of wall clock while this was open.

    Nullable because incidents that have nothing to do with continuity -- a
    stopped motion worker, an unreachable relay -- never measure it.
    """
    damaged_segments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Segments the indexer could not read, counted during this incident."""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    camera: Mapped[Camera] = relationship(back_populates="incidents")

    @property
    def is_active(self) -> bool:
        return self.resolved_at is None


class ExportJob(Base):
    """One requested clip, and how far along producing it is.

    Exports are rows rather than in-flight requests for two reasons. An hour of
    footage takes long enough that holding the HTTP connection open would fail
    on any reverse proxy with a sane timeout. And a job that was running when
    the process died has to be visible afterwards -- as failed, so the user can
    retry -- instead of vanishing and leaving a stale file behind.
    """

    __tablename__ = "export_jobs"
    __table_args__ = (
        Index("ix_export_created", "created_at"),
        Index("ix_export_protected", "protected"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False
    )

    #: The requested range. Trimming is inclusive of ``start_at`` and exclusive
    #: of ``end_at``, matching how the timeline selects.
    start_at: Mapped[datetime] = mapped_column(nullable=False)
    end_at: Mapped[datetime] = mapped_column(nullable=False)

    status: Mapped[ExportStatus] = mapped_column(
        Enum(ExportStatus, native_enum=False, length=16),
        nullable=False,
        default=ExportStatus.QUEUED,
    )
    progress: Mapped[float] = mapped_column(nullable=False, default=0.0)
    """0.0-1.0. Derived from output duration against requested duration, so it
    reflects work actually done rather than a timer."""

    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why it failed, in words a user can act on."""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """When a temporary file becomes eligible for cleanup. Null for a
    permanent saved clip; source recordings remain under their own retention."""

    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Protected exports are saved clips and never expire automatically.

    This is separate from protecting the source recording segments: once the
    stand-alone MP4 is complete, retention may reclaim the source footage while
    this copy remains available until an administrator deletes it explicitly.
    """

    camera: Mapped[Camera] = relationship()

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ExportStatus.COMPLETE,
            ExportStatus.FAILED,
            ExportStatus.EXPIRED,
            ExportStatus.CANCELLED,
        )

    @property
    def is_downloadable(self) -> bool:
        return self.status is ExportStatus.COMPLETE and self.file_path is not None
