"""ONVIF media profiles over SOAP.

WS-Discovery only ever hands back a device's control address (its ``XAddr``)
and a handful of scope strings -- never a usable stream URL. Getting a real
RTSP address means actually speaking ONVIF: ask the device for its Media
service address, list the encoder profiles it offers, and ask for the stream
URI of each one. Guessing a path instead (as ``app.discovery.profiles`` does
for cameras that do not speak ONVIF at all) is a fallback, not the norm --
when ONVIF is available its answer is authoritative and the guesswork is
unnecessary.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from app.discovery import xmlsafe
from app.logging_config import get_logger
from app.security.redaction import redact_url

logger = get_logger("DISCOVERY")

MIN_TIMEOUT_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = 30.0

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"

_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
<s:Header>{header}</s:Header>
<s:Body>{body}</s:Body>
</s:Envelope>"""

_WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"

_SECURITY_HEADER = (
    '<Security xmlns="{wsse_ns}" '
    's:mustUnderstand="1">'
    "<UsernameToken>"
    "<Username>{username}</Username>"
    '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
    "{digest}</Password>"
    '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
    "{nonce}</Nonce>"
    '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
    "{created}</Created>"
    "</UsernameToken>"
    "</Security>"
)


class OnvifError(RuntimeError):
    """A device could not be reached or its answer made no sense.

    The message is safe to show a user directly -- it never includes a
    password, and network detail is translated into something actionable.
    """


def _ws_security_header(username: str, password: str) -> str:
    """Build a WS-UsernameToken header with a PasswordDigest.

    Many ONVIF cameras refuse ``PasswordText`` outright once configured with
    a password, so this is not an optional hardening -- it is the only mode
    that works at all against that class of device.
    """
    nonce = secrets.token_bytes(20)
    # WS-Security requires a UTC timestamp in this exact profile; a handful of
    # cameras reject the request if the fractional seconds or the trailing
    # "Z" are missing.
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # PasswordDigest = Base64(SHA1(nonce + created + password)), and critically
    # the *raw* nonce bytes go into the hash -- not the base64 text that is
    # what actually travels on the wire for it. Hashing the base64 form
    # instead is the single most common way to reimplement this "correctly"
    # and still get rejected by every real device.
    digest_input = nonce + created.encode("ascii") + password.encode("utf-8")
    digest = base64.b64encode(
        hashlib.sha1(digest_input).digest()  # noqa: S324 -- SHA1 is mandated by the WS-Security UsernameToken Digest profile, not a choice made here
    ).decode("ascii")

    return _SECURITY_HEADER.format(
        wsse_ns=_WSSE_NS,
        username=escape(username),
        digest=digest,
        nonce=base64.b64encode(nonce).decode("ascii"),
        created=created,
    )


async def _soap_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    action: str,
    body: str,
    username: str | None,
    password: str | None,
) -> ET.Element:
    header = _ws_security_header(username, password) if username and password else ""
    envelope = _ENVELOPE.format(header=header, body=body)
    safe_url = redact_url(url)

    try:
        response = await client.post(
            url,
            content=envelope.encode("utf-8"),
            headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
        )
    except httpx.TimeoutException as exc:
        raise OnvifError(f"{safe_url} did not respond in time.") from exc
    except httpx.HTTPError as exc:
        raise OnvifError(f"Could not reach {safe_url}: {exc}") from exc

    if response.status_code == 401:
        raise OnvifError("The device rejected the username or password.")
    if response.status_code >= 400:
        raise OnvifError(f"The device answered {safe_url} with HTTP {response.status_code}.")

    try:
        root = xmlsafe.parse_bounded(response.content)
    except xmlsafe.UnsafeXmlError as exc:
        raise OnvifError(f"The device's answer could not be parsed: {exc}") from exc

    # SOAP faults are often returned with HTTP 200. Treating that as success
    # makes a PTZ button appear to work while the camera rejected the command.
    fault = xmlsafe.find_first(root, "Fault")
    if fault is not None:
        reason = xmlsafe.find_first(fault, "Text")
        detail = reason.text.strip() if reason is not None and reason.text else "ONVIF fault"
        raise OnvifError(f"The device rejected the ONVIF request: {detail}.")
    return root


async def get_media_xaddr(
    client: httpx.AsyncClient, device_xaddr: str, username: str | None, password: str | None
) -> str:
    """Ask the device where its Media service lives.

    Consumer cameras overwhelmingly answer every ONVIF service at the single
    address WS-Discovery already gave us, but that is an implementation
    convenience, not something the spec guarantees -- some NVRs and higher-end
    cameras put Media at a distinct path, so this call is not skippable.
    """
    body = f'<GetCapabilities xmlns="{_DEVICE_NS}"><Category>Media</Category></GetCapabilities>'
    try:
        root = await _soap_post(
            client,
            device_xaddr,
            action=f"{_DEVICE_NS}/GetCapabilities",
            body=body,
            username=username,
            password=password,
        )
    except OnvifError:
        # A device that cannot even answer GetCapabilities has bigger
        # problems, but plenty of minimal ONVIF stacks only implement the
        # Media operations at their one address -- worth trying there before
        # giving up entirely.
        return device_xaddr

    media = xmlsafe.find_first(root, "Media")
    if media is not None:
        xaddr = xmlsafe.find_first(media, "XAddr")
        if xaddr is not None and xaddr.text and xaddr.text.strip():
            return xaddr.text.strip()
    return device_xaddr


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """What a device says it is, straight from ONVIF ``GetDeviceInformation``.

    Every field is optional because plenty of firmware answers the call with a
    partial record. An absent manufacturer is a real answer -- "this device
    would not say" -- and is treated differently from a wrong guess.
    """

    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    hardware_id: str | None = None


async def get_device_information(
    client: httpx.AsyncClient, device_xaddr: str, username: str | None, password: str | None
) -> DeviceIdentity:
    """Ask the device who made it.

    Read-only, and the cheapest useful ONVIF call there is: one request, no
    state changed on the camera. It is what lets Sentinel stop offering a
    vendor's controls to another vendor's camera without asking anyone to type
    a brand name.

    The serial number is deliberately not read. It is the one field in this
    record that identifies a specific unit, it is of no use to any decision
    Sentinel makes, and not reading it means it cannot end up in a log or a
    diagnostics bundle.
    """
    body = f'<GetDeviceInformation xmlns="{_DEVICE_NS}"/>'
    root = await _soap_post(
        client,
        device_xaddr,
        action=f"{_DEVICE_NS}/GetDeviceInformation",
        body=body,
        username=username,
        password=password,
    )
    return DeviceIdentity(
        manufacturer=_clip(_text(xmlsafe.find_first(root, "Manufacturer"))),
        model=_clip(_text(xmlsafe.find_first(root, "Model"))),
        firmware_version=_clip(_text(xmlsafe.find_first(root, "FirmwareVersion"))),
        hardware_id=_clip(_text(xmlsafe.find_first(root, "HardwareId"))),
    )


def _clip(value: str | None, limit: int = 96) -> str | None:
    """Keep a device's self-description inside the column that stores it."""
    if value is None:
        return None
    trimmed = " ".join(value.split())
    return trimmed[:limit] or None


@dataclass(frozen=True, slots=True)
class MediaProfile:
    """One encoder profile, resolved to a real stream address."""

    token: str
    name: str | None
    encoding: str | None
    width: int | None
    height: int | None
    fps: float | None
    stream_uri: str


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    stripped = element.text.strip()
    return stripped or None


def _int(element: ET.Element | None) -> int | None:
    value = _text(element)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(element: ET.Element | None) -> float | None:
    value = _text(element)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


async def get_profiles(
    client: httpx.AsyncClient, media_xaddr: str, username: str | None, password: str | None
) -> list[MediaProfile]:
    body = f'<GetProfiles xmlns="{_MEDIA_NS}"/>'
    root = await _soap_post(
        client,
        media_xaddr,
        action=f"{_MEDIA_NS}/GetProfiles",
        body=body,
        username=username,
        password=password,
    )

    profiles: list[MediaProfile] = []
    for profile_el in xmlsafe.find_all(root, "Profiles"):
        token = profile_el.get("token")
        if not token:
            continue
        # `find_first`/`find_all` tolerate a None root, so chaining lookups
        # through a video encoder config that a particular profile lacks
        # needs no `is not None` guard at every step -- see the note on
        # Element truthiness in xmlsafe.find_all.
        video_config = xmlsafe.find_first(profile_el, "VideoEncoderConfiguration")
        resolution = xmlsafe.find_first(video_config, "Resolution")
        rate_control = xmlsafe.find_first(video_config, "RateControl")
        profiles.append(
            MediaProfile(
                token=token,
                name=_text(xmlsafe.find_first(profile_el, "Name")),
                encoding=_text(xmlsafe.find_first(video_config, "Encoding")),
                width=_int(xmlsafe.find_first(resolution, "Width")),
                height=_int(xmlsafe.find_first(resolution, "Height")),
                fps=_float(xmlsafe.find_first(rate_control, "FrameRateLimit")),
                stream_uri="",
            )
        )
    return profiles


async def get_stream_uri(
    client: httpx.AsyncClient,
    media_xaddr: str,
    username: str | None,
    password: str | None,
    profile_token: str,
) -> str:
    body = (
        f'<GetStreamUri xmlns="{_MEDIA_NS}">'
        "<StreamSetup>"
        f'<Stream xmlns="{_SCHEMA_NS}">RTP-Unicast</Stream>'
        f'<Transport xmlns="{_SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport>'
        "</StreamSetup>"
        f"<ProfileToken>{escape(profile_token)}</ProfileToken>"
        "</GetStreamUri>"
    )
    root = await _soap_post(
        client,
        media_xaddr,
        action=f"{_MEDIA_NS}/GetStreamUri",
        body=body,
        username=username,
        password=password,
    )
    uri = _text(xmlsafe.find_first(root, "Uri"))
    if uri is None:
        raise OnvifError("The device did not return a stream address for this profile.")
    return uri


def _strip_userinfo(uri: str) -> str:
    """Drop any credentials the device embedded in its own answer.

    ONVIF stream URIs are not supposed to carry userinfo (RTSP authentication
    is negotiated separately), but nothing stops a device from doing it
    anyway, and the API layer must never hand a password back to the caller
    that just supplied it -- returning it, even redundantly, is a needless way
    for it to end up in a browser history or a support screenshot.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(uri)
    if "@" not in parts.netloc:
        return uri
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


async def reachable_stream_uri(uri: str, discovered_host: str) -> str:
    """Fix up a stream URI whose host will never resolve.

    A recurring firmware quirk: the camera reports its ``GetStreamUri``
    result using a hostname that means something only inside its own
    configuration -- a factory default like the model name, or an internal
    hostname nothing on this network has ever heard of -- instead of the
    address it was actually just reached at. ``discovered_host`` is proven
    reachable (it is whatever WS-Discovery or the caller supplied to get this
    far), so substituting it whenever the camera's own hostname does not
    resolve turns an unusable URL into a working one.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(uri)
    hostname = parts.hostname
    if hostname is None:
        return uri

    try:
        ipaddress.ip_address(hostname)
        return uri  # already a literal address; nothing to fix
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo(hostname, None), timeout=2.0)
        return uri  # resolves fine here; trust the camera's own answer
    except (OSError, TimeoutError):
        pass

    netloc = discovered_host if parts.port is None else f"{discovered_host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


async def fetch_streams(
    *,
    device_xaddr: str,
    discovered_host: str,
    username: str | None,
    password: str | None,
    timeout_seconds: float = 10.0,
) -> list[MediaProfile]:
    """The full flow: locate the Media service, list its profiles, and
    resolve each to a real, reachable stream URI."""
    timeout = min(max(timeout_seconds, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        media_xaddr = await get_media_xaddr(client, device_xaddr, username, password)
        profiles = await get_profiles(client, media_xaddr, username, password)

        resolved: list[MediaProfile] = []
        for profile in profiles:
            try:
                uri = await get_stream_uri(client, media_xaddr, username, password, profile.token)
            except OnvifError as exc:
                # One misbehaving profile (a disabled analytics profile with
                # no stream, for instance) should not cost the caller every
                # profile the device does offer a URI for.
                logger.debug(
                    "Skipping a profile with no usable stream URI",
                    extra={"token": profile.token, "error": str(exc)},
                )
                continue
            fixed = await reachable_stream_uri(uri, discovered_host)
            resolved.append(replace(profile, stream_uri=_strip_userinfo(fixed)))
        return resolved
