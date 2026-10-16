"""Defensive XML parsing for responses from devices on the network.

Every document handled in this package -- a WS-Discovery ``ProbeMatches``
reply, an ONVIF SOAP response -- comes from whatever answered a UDP multicast
or a POST to a user-supplied address, not from a source Sentinel controls.
``xml.etree.ElementTree`` is built on the C ``expat`` parser, which resolves
in-document ``<!ENTITY>`` definitions before it hands elements back: a few
hundred bytes of nested entity references (the classic "billion laughs")
expand into gigabytes of memory, and there is no keyword argument that turns
that off. Pulling in ``defusedxml`` would fix it in one line, but this
project's dependency list is frozen, so the mitigation has to be built from
what the standard library already gives us:

* Reject anything declaring a ``DOCTYPE`` outright. No legitimate
  WS-Discovery or ONVIF reply needs one, so refusing them closes the entity
  route entirely rather than trying to sanitise it.
* Cap the input size before it ever reaches the parser, so even a
  DOCTYPE-free document cannot be an enormous flat pile of elements.
* Cap nesting depth while parsing (checked incrementally, not after the fact)
  so a deeply-recursive document cannot blow the stack or cost more to parse
  than the shallow SOAP envelopes this package actually expects.
"""

from __future__ import annotations

import re
from typing import cast
from xml.etree import ElementTree as ET

#: Comfortably larger than any real ProbeMatches or GetProfiles response
#: (typically a few KB), tiny next to what a deliberate flood would need to
#: matter.
MAX_RESPONSE_BYTES = 262_144

#: A SOAP envelope for the messages this package sends is 6-10 levels deep.
#: Twice that leaves headroom for verbose implementations without opening the
#: door to pathological nesting.
MAX_ELEMENT_DEPTH = 40

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


class UnsafeXmlError(ValueError):
    """A response was refused before or during parsing.

    Covers both a document that looks like it was crafted to attack the
    parser and one that is simply malformed -- either way the caller's only
    correct move is to skip this response and move on, so both cases share
    one exception type.
    """


def parse_bounded(
    data: bytes,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_depth: int = MAX_ELEMENT_DEPTH,
) -> ET.Element:
    """Parse ``data`` as XML, refusing anything that looks like an attack.

    Returns the root element. Raises :class:`UnsafeXmlError` for an oversized
    payload, a declared ``DOCTYPE``, excessive nesting, or XML that simply
    does not parse.
    """
    if len(data) > max_bytes:
        raise UnsafeXmlError(
            f"Response is {len(data)} bytes, over the {max_bytes}-byte limit for a "
            "discovery reply; refusing to parse it."
        )
    if _DOCTYPE_RE.search(data):
        raise UnsafeXmlError(
            "Response declares a DOCTYPE, which no genuine WS-Discovery or ONVIF "
            "reply needs; refusing to parse it rather than risk entity expansion."
        )

    # An incremental pull-parser lets depth be checked element-by-element as
    # the document is consumed, instead of only after the whole tree (however
    # large) has already been built.
    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(["start", "end"])
    try:
        parser.feed(data)
        parser.close()
        depth = 0
        root: ET.Element | None = None
        for item in parser.read_events():
            # `read_events` is typed for the whole iterparse event protocol,
            # which also covers "start-ns"/"end-ns" events (a 1-tuple, or a
            # (prefix, uri) pair) -- neither shape can occur for the "start"
            # and "end" events requested above, so this narrows the type back
            # to what the parser actually yields for them.
            event, elem = cast("tuple[str, ET.Element]", item)
            if event == "start":
                depth += 1
                if depth > max_depth:
                    raise UnsafeXmlError(
                        f"Response nests more than {max_depth} elements deep; refusing to parse it."
                    )
            else:  # "end"
                depth -= 1
                # Elements are fully built (children attached) by the time
                # their "end" event fires, and the very last one is the
                # document root -- overwriting on every "end" event is
                # simpler than tracking parent/child bookkeeping ourselves.
                root = elem
    except ET.ParseError as exc:
        raise UnsafeXmlError(f"Response is not well-formed XML: {exc}") from exc

    if root is None:
        raise UnsafeXmlError("Response contained no XML element.")
    return root


def local_name(tag: str) -> str:
    """Strip a ``{namespace}`` prefix, for matching elements by tag alone.

    Different ONVIF stacks disagree on namespace prefixes (and a few omit the
    namespace declaration they should carry); matching on the local name is
    more robust than requiring an exact ``{uri}tag`` match everywhere.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find_all(root: ET.Element | None, local: str) -> list[ET.Element]:
    """Every descendant (any namespace) whose local tag name is ``local``.

    Accepts ``None`` and returns an empty list so a caller chaining lookups
    (``find_first(find_first(root, "A"), "B")``) does not need an explicit
    ``is not None`` check at every step -- ``Element`` objects with no
    children are falsy under Python's default truthiness rules, which makes
    ``if element:`` silently wrong for a perfectly valid empty element, so
    every check in this module is against identity, not truthiness.
    """
    if root is None:
        return []
    return [elem for elem in root.iter() if local_name(elem.tag) == local]


def find_first(root: ET.Element | None, local: str) -> ET.Element | None:
    if root is None:
        return None
    for elem in root.iter():
        if local_name(elem.tag) == local:
            return elem
    return None
