"""Deciding whether a camera is a TP-Link/Tapo device Sentinel may control.

This exists because "is this a Tapo camera?" used to be answered with "is it not
a battery camera?", which is true of every RTSP source on the network. The
result was a *Tapo controls* button on four Hikvision NVR channels, and a
support burden made of vendor calls that were never going to succeed.

The decision here is deliberately narrow. Only three things make Sentinel offer
the controls on its own:

* the camera *is* a Tapo battery device (its whole transport is proprietary);
* its manufacturer string names TP-Link or Tapo;
* its model string is a recognised Tapo model.

Manufacturer and model come from ONVIF ``GetDeviceInformation`` where the camera
supports it, or from what the administrator typed. Everything else -- notably
the address -- is evidence *against* a guess, never for one: several channels of
one NVR share a host, and a Tapo camera on that same subnet proves nothing about
its neighbours.

Where the metadata is absent, Sentinel says so and offers an explicit opt-in
rather than probing a stranger's camera on the chance that it answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from app.database.models import Camera


class TapoControlSource(StrEnum):
    """Why the answer is what it is. Shown to the administrator verbatim."""

    MANUAL = "manual"
    """An administrator answered explicitly for this camera."""
    KIND = "kind"
    """The camera is registered as a Tapo battery device."""
    METADATA = "metadata"
    """The manufacturer or model identifies a TP-Link/Tapo device."""
    OTHER_VENDOR = "other_vendor"
    """The manufacturer, model or stream layout identifies a different vendor."""
    UNKNOWN = "unknown"
    """Nothing identifies this camera either way."""


@dataclass(frozen=True, slots=True)
class TapoClassification:
    """What Sentinel believes about one camera, and how firmly."""

    supported: bool
    """Whether the Tapo LAN controls should be offered at all."""
    source: TapoControlSource
    detected_vendor: str | None
    """The vendor Sentinel recognised, when it recognised one."""
    reason: str
    """One sentence an administrator can act on."""

    @property
    def can_opt_in(self) -> bool:
        """Whether offering "Enable Tapo LAN controls" would be honest here.

        Only for cameras nothing identifies. A device that announced itself as a
        Hikvision NVR channel is not a candidate, and neither is one already
        recognised as Tapo.
        """
        return self.source is TapoControlSource.UNKNOWN


#: Manufacturer strings that mean TP-Link. Compared case- and separator-
#: insensitively, so "TP-Link", "TP LINK" and "tplink" all match.
_TAPO_MANUFACTURERS: frozenset[str] = frozenset(
    {
        "tplink",
        "tplinktechnologies",
        "tplinktechnologiesco",
        "tapo",
        "tapocamera",
        "vigi",
    }
)

#: Manufacturers that are definitively *not* TP-Link. Not exhaustive, and it
#: does not need to be: an unrecognised manufacturer falls through to "unknown",
#: which is a safe answer. This list only exists so the common brands on a mixed
#: installation stop being asked Tapo questions.
_OTHER_MANUFACTURERS: frozenset[str] = frozenset(
    {
        "hikvision",
        "hikvisiondigitaltechnology",
        "dahua",
        "dahuatechnology",
        "reolink",
        "axis",
        "axiscommunications",
        "uniview",
        "univision",
        "amcrest",
        "hanwha",
        "hanwhatechwin",
        "samsungtechwin",
        "bosch",
        "vivotek",
        "annke",
        "swann",
        "lorex",
        "foscam",
        "ubiquiti",
        "avigilon",
        "panasonic",
        "sony",
        "milesight",
        "tiandy",
        "xiongmai",
        "hisilicon",
        "eufy",
        "wyze",
        "aqara",
        "imou",
        "ezviz",
    }
)

#: Tapo and VIGI model numbers. ``C`` covers the mains cameras (C100/C200/C210/
#: C310/C320WS/C420...), ``TC`` the tri-band and battery range (TC60/TC65/TC70/
#: TC71/TC82...), ``D`` the doorbells, ``H`` the hubs. ``VIGI`` is TP-Link's
#: business line and speaks the same local control protocol.
_TAPO_MODEL_PATTERN = re.compile(
    r"""
    ^\s*
    (?:tapo|tp-?link)?\s*        # an optional brand prefix people often type
    (?:
        tc\d{2,3}               # TC60, TC70, TC82
      | c\d{3,4}                # C100, C200, C320, C420
      | d\d{3}                  # D130, D230 doorbells
      | h\d{3}                  # H100, H200 hubs
      | vigi\s*c\w*             # VIGI C340, VIGI C440
    )
    [a-z0-9\-]*                 # trailing suffixes: WS, -2K, KIT, W
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Model strings that look Tapo-ish but belong to someone else. Checked before
#: the pattern above so a Hikvision "DS-2CD2043" cannot be read as a "D" model.
_OTHER_MODEL_PREFIXES: tuple[str, ...] = (
    "ds-",  # Hikvision
    "ipc-",  # Dahua / Amcrest
    "sd",  # Dahua PTZ
    "rlc-",  # Reolink
    "e1",  # Reolink E1 series
    "trassir",
    "nvr",
    "dvr",
    "xvr",
)

#: RTSP path shapes that belong to a specific non-TP-Link product line. A path
#: is a vendor's own naming scheme, which makes it real evidence -- unlike the
#: address, which says only which box answers.
_OTHER_PATH_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("/streaming/channels/", "Hikvision-compatible NVR"),
    ("/isapi/", "Hikvision-compatible NVR"),
    ("/cam/realmonitor", "Dahua"),
    ("/h264preview_", "Reolink"),
    ("/h265preview_", "Reolink"),
    ("/axis-media/", "Axis"),
    ("/media/video", "Uniview"),
    ("/unicast/c", "Uniview"),
    ("/live/ch", "generic NVR channel"),
    ("/chid=", "generic NVR channel"),
    ("/videomain", "Amcrest"),
    ("/profile1/media.smp", "Hanwha"),
)


def _normalise(value: str | None) -> str:
    """Strip everything that varies between how vendors spell themselves."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _paths(camera: Camera) -> list[str]:
    out: list[str] = []
    for url in (camera.main_stream_url, camera.sub_stream_url):
        if not url:
            continue
        try:
            split = urlsplit(url)
        except ValueError:  # pragma: no cover - urlsplit is extremely permissive
            continue
        # Query included: Dahua puts the channel in it, and nothing here is
        # sensitive -- credentials live in the userinfo, which is dropped.
        path = split.path or ""
        if split.query:
            path = f"{path}?{split.query}"
        out.append(path.lower())
    return out


def _model_says_other(model: str | None) -> bool:
    if not model:
        return False
    lowered = model.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _OTHER_MODEL_PREFIXES)


def classify_tapo_controls(camera: Camera) -> TapoClassification:
    """Decide whether to offer Tapo LAN controls for one camera.

    A manual opt-out always wins. A manual opt-in is deliberately weaker than
    positive evidence that the device is another vendor: it exists only for a
    camera Sentinel could not identify, not as a way to send TP-Link requests
    to a Hikvision/Dahua/Reolink device somebody selected by mistake.
    """
    # Imported here rather than at module scope: models.py imports nothing from
    # this module, and keeping it that way avoids a cycle.
    from app.database.models import CameraKind

    if camera.kind is CameraKind.TAPO_BATTERY:
        # Not overridable: the whole transport is the vendor's, so there is no
        # meaningful "off" that still leaves a working camera.
        return TapoClassification(
            supported=True,
            source=TapoControlSource.KIND,
            detected_vendor="Tapo",
            reason="This camera is registered as a Tapo battery device.",
        )

    if camera.tapo_controls_enabled is False:
        return TapoClassification(
            supported=False,
            source=TapoControlSource.MANUAL,
            detected_vendor=camera.manufacturer or None,
            reason="An administrator turned Tapo LAN controls off for this camera.",
        )

    manufacturer = _normalise(camera.manufacturer)
    if manufacturer:
        if any(manufacturer.startswith(known) for known in _TAPO_MANUFACTURERS):
            return TapoClassification(
                supported=True,
                source=TapoControlSource.METADATA,
                detected_vendor=camera.manufacturer,
                reason=f"{camera.manufacturer} identifies this as a TP-Link/Tapo camera.",
            )
        if any(manufacturer.startswith(known) for known in _OTHER_MANUFACTURERS):
            return TapoClassification(
                supported=False,
                source=TapoControlSource.OTHER_VENDOR,
                detected_vendor=camera.manufacturer,
                reason=f"{camera.manufacturer} is not a TP-Link/Tapo camera.",
            )

    model = (camera.model or "").strip()
    if model:
        if _model_says_other(model):
            return TapoClassification(
                supported=False,
                source=TapoControlSource.OTHER_VENDOR,
                detected_vendor=camera.manufacturer or None,
                reason=f"Model {model} is not a TP-Link/Tapo camera.",
            )
        if _TAPO_MODEL_PATTERN.match(model):
            return TapoClassification(
                supported=True,
                source=TapoControlSource.METADATA,
                detected_vendor=camera.manufacturer or "Tapo",
                reason=f"Model {model} is a TP-Link/Tapo camera.",
            )

    for path in _paths(camera):
        for signature, vendor in _OTHER_PATH_SIGNATURES:
            if signature in path:
                return TapoClassification(
                    supported=False,
                    source=TapoControlSource.OTHER_VENDOR,
                    detected_vendor=vendor,
                    reason=(
                        f"The stream address uses the {vendor} layout, "
                        "so Tapo controls do not apply."
                    ),
                )

    # Opt-in is an answer to missing evidence, never an override of contrary
    # evidence. Keep it after every manufacturer/model/path exclusion so even
    # a stale value written by an older build cannot re-enable vendor calls on
    # a positively identified NVR channel.
    if camera.tapo_controls_enabled is True:
        return TapoClassification(
            supported=True,
            source=TapoControlSource.MANUAL,
            detected_vendor=camera.manufacturer or None,
            reason="An administrator enabled Tapo LAN controls for this camera.",
        )

    return TapoClassification(
        supported=False,
        source=TapoControlSource.UNKNOWN,
        detected_vendor=None,
        reason=(
            "Sentinel could not identify this camera's manufacturer. Run Identify, or turn "
            "on Tapo LAN controls in Configure if you know it is a TP-Link/Tapo camera."
        ),
    )


def supports_tapo_controls(camera: Camera) -> bool:
    """Shorthand for the single boolean the routers gate on."""
    return classify_tapo_controls(camera).supported
