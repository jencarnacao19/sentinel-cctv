"""Stream distribution through go2rtc.

Cameras are connected to exactly once. go2rtc fans that single connection out to
the recorder, the motion worker, every browser tab and Home Assistant, so a
camera never sees four simultaneous RTSP clients -- which is what makes cheap
consumer cameras stop responding.
"""
