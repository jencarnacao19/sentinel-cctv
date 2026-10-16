"""Camera Reliability Center.

Answers the questions a live status badge cannot: *what* failed, whether
recording is fresh right now, how many times the supervisor restarted, which
input recovered it, how long the interruption lasted, and whether the problem
is current or historical.

The hard constraint on this package is that it is a *reader*. Deciding that a
camera is unhealthy must never mean opening another RTSP, ONVIF or vendor
connection to it: consumer cameras cap concurrent clients at two or three, so a
monitor that dialled every camera would itself cause the outages it reports.
Every input comes from telemetry Sentinel already owns -- supervised process
state, the segment index, manager flags -- so watching every camera costs one
indexed query and a few dictionary lookups.
"""

from app.reliability.state import CameraState

__all__ = ["CameraState"]
