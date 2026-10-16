"""Classification of a recorder's last failure into a fixed label.

FFmpeg's diagnostics are the most useful signal Sentinel has about *why* a
camera stopped recording, and the least safe thing to store: they are unbounded,
they quote the input URL, and they are exactly the sort of text that ends up
pasted into a public issue.

So they are never kept. This module reads the supervisor's already-redacted
last line and returns nothing but a member of :class:`StreamFault`. No caller
can obtain the original text through it, which is what makes it safe to persist
the result on an incident row and show it in the browser.
"""

from __future__ import annotations

import re

from app.database.models import StreamFault

#: Ordered: the first pattern that matches wins, so the most actionable
#: explanation is preferred over a generic one. Missing SPS/PPS comes first
#: because it is the failure an operator can act on -- it means the recorder
#: joined a relayed stream that never repeated its H.264 setup data, and the
#: fix is a different input rather than a different camera.
_PATTERNS: tuple[tuple[StreamFault, re.Pattern[str]], ...] = (
    (
        StreamFault.MISSING_STREAM_PARAMETERS,
        re.compile(
            r"non[- ]?existing\s+(?:pps|sps)"
            r"|(?:pps|sps)\b[^.]{0,32}\b(?:missing|not\s+(?:yet\s+)?available)"
            r"|dimensions\s+not\s+set"
            r"|unspecified\s+(?:pixel\s+format|size)"
            r"|could\s+not\s+find\s+codec\s+parameters"
            r"|no\s+start\s+code\s+is\s+found",
            re.IGNORECASE,
        ),
    ),
    (
        StreamFault.AUTHENTICATION_REJECTED,
        re.compile(r"401\s+unauthorized|403\s+forbidden|authorization\s+failed", re.IGNORECASE),
    ),
    (
        StreamFault.TIMEOUT,
        re.compile(r"timed\s+out|\btimeout\b|max\s+delay\s+reached", re.IGNORECASE),
    ),
    (
        StreamFault.CONNECTION_FAILED,
        re.compile(
            r"connection\s+refused"
            r"|connection\s+reset"
            r"|no\s+route\s+to\s+host"
            r"|network\s+is\s+unreachable"
            r"|error\s+opening\s+input"
            r"|(?:server|method\s+\w+)\s+returned\s+[45]\d\d"
            r"|immediate\s+exit\s+requested",
            re.IGNORECASE,
        ),
    ),
    (
        StreamFault.CORRUPT_PACKETS,
        re.compile(
            r"bad\s+sequence\b"
            r"|bad\s+cseq\b"
            r"|rtp:\s*(?:bad|dropping)"
            r"|missed\s+\d+\s+packets"
            r"|corrupt(?:ed)?\s+(?:packet|macroblock|frame)"
            r"|error\s+while\s+decoding",
            re.IGNORECASE,
        ),
    ),
)


def classify(message: str | None) -> StreamFault | None:
    """Return a label for ``message``, or ``None`` when there is nothing to say.

    The return value is always a :class:`StreamFault` member. No fragment of
    ``message`` is ever returned, logged or stored by this function.
    """
    if not message or not message.strip():
        return None
    for fault, pattern in _PATTERNS:
        if pattern.search(message):
            return fault
    # Something failed and Sentinel does not recognise it. Saying so is more
    # useful than silently reporting no fault at all.
    return StreamFault.UNKNOWN
