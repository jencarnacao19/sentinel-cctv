"""Camera discovery: ONVIF WS-Discovery, known vendor URL patterns, ONVIF
media profiles and NVR multi-channel import.

Everything a saved camera needs -- a name, a stream URL, credentials -- can
also be produced by this package from nothing but "there is a device on the
network". Nothing here writes to the database on its own; every flow ends at
:mod:`app.discovery.schemas`'s ``ImportCamerasRequest``, which reuses
``app.cameras.service.create_camera`` so a discovered camera is stored exactly
like a hand-typed one.
"""
