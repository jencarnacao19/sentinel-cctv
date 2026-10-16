"""Home Assistant facing surface: mDNS announcement and the HA REST handshake.

Everything a Home Assistant integration needs beyond the ordinary API
(cameras, snapshots, storage, motion events -- all reused as-is, see
``CONTRACT.md``) lives here: a stable install identity, the network
announcement, and the one new endpoint that ties them together. See
``CONTRACT.md`` in this directory for the full request/response contract the
integration in ``custom_components/sentinel`` is built against.
"""

from __future__ import annotations
