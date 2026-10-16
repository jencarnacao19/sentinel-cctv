"""Continuous recording.

The one thing Sentinel must never do is lose footage. Everything here is built
around that: stream copy so the CPU is never the bottleneck, short segments so a
power cut costs seconds rather than hours, fragmented MP4 so even an interrupted
file still plays, and a supervisor that never stops trying to reconnect.
"""
