"""Constants shared by every module of the Sentinel CCTV integration.

Deliberately free of Home Assistant imports so the API client and the parsing
helpers can be unit tested without a Home Assistant installation.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sentinel"

#: Advertised by the Sentinel server; the config flow is offered when it is seen.
ZEROCONF_SERVICE_TYPE: Final = "_sentinel._tcp.local."

DEFAULT_PORT: Final = 8090
DEFAULT_NAME: Final = "Sentinel CCTV"

CONF_URL: Final = "url"
CONF_TOKEN: Final = "token"

API_PREFIX: Final = "/api/v1"

#: The handshake endpoint the config flow validates against, and the one whose
#: absence (a plain 404) means the server predates the Home Assistant
#: integration entirely rather than merely being unreachable.
HANDSHAKE_PATH: Final = "/integrations/home-assistant"

#: Bumped only if the handshake payload shape changes in a way old code would
#: misread. The server echoes back the version it speaks; a mismatch is reported
#: as "unsupported server version" rather than a confusing parse failure.
SUPPORTED_API_VERSION: Final = 1

#: Full path, including the API prefix: the WebSocket is not nested under the
#: same router mount as everything else, but it lives on the same version.
DEFAULT_WEBSOCKET_PATH: Final = f"{API_PREFIX}/events/stream"

#: REST is only a safety net: state arrives over the WebSocket. This poll exists
#: to pick up cameras added while the socket was up, to refresh metadata that has
#: no event (bitrate, resolution) and to notice a server that died quietly.
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 60

#: While the socket is down the safety net is all there is, so it runs much more
#: often than the leisurely default -- until the socket comes back and the
#: coordinator relaxes to the configured interval again.
FALLBACK_SCAN_INTERVAL_SECONDS: Final = 15

#: Options-flow bounds for the user-configurable fallback interval.
MIN_SCAN_INTERVAL_SECONDS: Final = 15
MAX_SCAN_INTERVAL_SECONDS: Final = 3600

REQUEST_TIMEOUT_SECONDS: Final = 10.0
SNAPSHOT_TIMEOUT_SECONDS: Final = 20.0

#: aiohttp's protocol-level heartbeat detects a dead socket. Sentinel does not
#: receive WebSocket control frames in its ASGI application, so a separate JSON
#: ping below is required to refresh the server's client-idle timer.
WEBSOCKET_HEARTBEAT_SECONDS: Final = 30.0
WEBSOCKET_APPLICATION_PING_SECONDS: Final = 25.0

RECONNECT_BASE_SECONDS: Final = 1.0
RECONNECT_MAX_SECONDS: Final = 60.0

#: Events fired on the Home Assistant bus for use in automations.
EVENT_MOTION_STARTED: Final = "sentinel_motion_started"
EVENT_MOTION_ENDED: Final = "sentinel_motion_ended"

ATTR_CAMERA_ID: Final = "camera_id"
ATTR_CAMERA_NAME: Final = "camera_name"
ATTR_EVENT_ID: Final = "event_id"
ATTR_TIMESTAMP: Final = "timestamp"

MANUFACTURER: Final = "Sentinel CCTV"

STATUS_ONLINE: Final = "online"

# WebSocket frame types this integration acts on. Anything else is ignored, so a
# newer server can add frames without breaking an older integration.
EVENT_TYPE_MOTION_STARTED: Final = "motion.started"
EVENT_TYPE_MOTION_ENDED: Final = "motion.ended"
EVENT_TYPE_CAMERA_ONLINE: Final = "camera.online"
EVENT_TYPE_CAMERA_OFFLINE: Final = "camera.offline"

EVENT_TYPE_RECORDING_STARTED: Final = "recording.started"
EVENT_TYPE_RECORDING_STOPPED: Final = "recording.stopped"
EVENT_TYPE_STORAGE_WARNING: Final = "storage.warning"
EVENT_TYPE_STORAGE_CRITICAL: Final = "storage.critical"
EVENT_TYPE_CAMERA_ADDED: Final = "camera.added"
EVENT_TYPE_CAMERA_REMOVED: Final = "camera.removed"

#: Frames that change the camera list, recording state or storage figures in
#: ways only REST can describe; each one triggers a coordinator refresh rather
#: than a partial local update. Kept in sync with ``app.events.bus.EventName``
#: on the Sentinel server -- an event outside this set and outside the ones
#: handled directly (motion, camera online/offline) is ignored outright, so a
#: newer server can add event types without breaking an older integration.
REFRESH_EVENT_TYPES: Final = frozenset(
    {
        EVENT_TYPE_CAMERA_ADDED,
        EVENT_TYPE_CAMERA_REMOVED,
        EVENT_TYPE_RECORDING_STARTED,
        EVENT_TYPE_RECORDING_STOPPED,
        EVENT_TYPE_STORAGE_WARNING,
        EVENT_TYPE_STORAGE_CRITICAL,
    }
)
