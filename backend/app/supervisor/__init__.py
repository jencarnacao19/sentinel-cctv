"""Process supervision.

Everything Sentinel spawns -- go2rtc, the FFmpeg recorders, the motion frame
readers -- is long-lived and will eventually die: a camera reboots, the network
drops, a codec confuses FFmpeg. The supervisor is what makes that a non-event.
"""

from app.supervisor.process import (
    RESTART_BACKOFF_SECONDS,
    ProcessState,
    SupervisedProcess,
    backoff_delay,
)

__all__ = [
    "RESTART_BACKOFF_SECONDS",
    "ProcessState",
    "SupervisedProcess",
    "backoff_delay",
]
