"""Where each playable piece of a recorded segment file starts and ends.

Recordings are fragmented MP4 (``frag_keyframe``): an init header (``ftyp`` +
``moov``) followed by one ``moof``/``mdat`` pair per keyframe. That layout is
exactly what an HLS playlist can address with byte ranges, so playback serves
the files as they are -- no remux, no re-encode -- and a player downloads a few
seconds at a time instead of a whole minute of footage before the first frame.

Only box headers and the small ``moof`` boxes are read; the media data is
skipped. Results are cached by path, size and modification time, because a
finished segment never changes and a playlist asks for the same files again.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

#: Fragments are grouped until a chunk lasts at least this long. HLS players
#: fetch one chunk per request; a keyframe interval of one or two seconds would
#: otherwise mean a request per second at 1x and dozens per second at 16x.
CHUNK_TARGET_SECONDS = 4.0

#: A moof holds sample tables only; anything bigger is not a file we wrote.
_MAX_MOOF_BYTES = 4 * 1024 * 1024
_CONTAINERS = {b"moov", b"trak", b"mdia", b"mvex", b"moof", b"traf"}


class FragmentIndexError(ValueError):
    """The file is not a fragmented MP4 this module can address."""


@dataclass(frozen=True, slots=True)
class Chunk:
    offset: int
    length: int
    seconds: float


@dataclass(frozen=True, slots=True)
class FragmentIndex:
    init_length: int
    """Bytes from the start of the file up to the first ``moof``."""
    chunks: tuple[Chunk, ...]

    @property
    def seconds(self) -> float:
        return sum(chunk.seconds for chunk in self.chunks)


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """Yield ``(type, payload_start, box_end)`` for the boxes in ``data``."""
    position, end = start, len(data) if end is None else end
    while position + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, position + 8)
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise FragmentIndexError("truncated box")
        yield kind, position + header, position + size
        position += size


def _walk(data: bytes, start: int, end: int, path: tuple[bytes, ...] = ()):
    for kind, payload, box_end in _boxes(data, start, end):
        yield (*path, kind), payload, box_end
        if kind in _CONTAINERS:
            yield from _walk(data, payload, box_end, (*path, kind))


def _video_track(moov: bytes) -> tuple[int, int, int]:
    """``(track_id, timescale, default_sample_duration)`` of the video track."""
    tracks: dict[int, dict[str, int]] = {}
    current: dict[str, int] = {}
    defaults: dict[int, int] = {}
    for path, payload, _ in _walk(moov, 0, len(moov)):
        kind = path[-1]
        if kind == b"trak":
            current = {}
        elif kind == b"tkhd":
            version = moov[payload]
            current["id"] = struct.unpack_from(">I", moov, payload + (20 if version else 12))[0]
            tracks[current["id"]] = current
        elif kind == b"mdhd":
            version = moov[payload]
            offset = payload + (20 if version else 12)
            current["timescale"] = struct.unpack_from(">I", moov, offset)[0]
        elif kind == b"hdlr" and moov[payload + 8 : payload + 12] == b"vide":
            current["video"] = 1
        elif kind == b"trex":
            track_id, _, duration = struct.unpack_from(">III", moov, payload + 4)
            defaults[track_id] = duration
    for track_id, track in tracks.items():
        if track.get("video") and track.get("timescale"):
            return track_id, track["timescale"], defaults.get(track_id, 0)
    raise FragmentIndexError("no video track")


def _fragment_ticks(moof: bytes, track_id: int, default_duration: int) -> int:
    """Total sample duration of ``track_id`` in one ``moof``, in track ticks."""
    total = 0
    in_track = False
    track_default = default_duration
    for path, payload, _ in _walk(moof, 0, len(moof)):
        kind = path[-1]
        if kind == b"traf":
            in_track = False
        elif kind == b"tfhd":
            flags = int.from_bytes(moof[payload + 1 : payload + 4], "big")
            in_track = struct.unpack_from(">I", moof, payload + 4)[0] == track_id
            position = payload + 8
            position += 8 if flags & 0x01 else 0  # base_data_offset
            position += 4 if flags & 0x02 else 0  # sample_description_index
            track_default = (
                struct.unpack_from(">I", moof, position)[0] if flags & 0x08 else default_duration
            )
        elif kind == b"trun" and in_track:
            flags = int.from_bytes(moof[payload + 1 : payload + 4], "big")
            (count,) = struct.unpack_from(">I", moof, payload + 4)
            position = payload + 8
            position += 4 if flags & 0x01 else 0  # data_offset
            position += 4 if flags & 0x04 else 0  # first_sample_flags
            if not flags & 0x100:
                total += count * track_default
                continue
            stride = 4 * sum(1 for bit in (0x100, 0x200, 0x400, 0x800) if flags & bit)
            for sample in range(count):
                total += struct.unpack_from(">I", moof, position + sample * stride)[0]
    return total


def _read_box_header(handle: BinaryIO) -> tuple[bytes, int, int] | None:
    """``(type, header_length, box_length)`` at the current position."""
    header = handle.read(8)
    if len(header) < 8:
        return None
    size, kind = struct.unpack(">I4s", header)
    if size == 1:
        large = handle.read(8)
        if len(large) < 8:
            return None
        return kind, 16, struct.unpack(">Q", large)[0]
    if size == 0:
        current = handle.tell()
        end = handle.seek(0, 2)
        handle.seek(current)
        return kind, 8, end - current + 8
    return kind, 8, size


def _index(path: Path) -> FragmentIndex:
    fragments: list[tuple[int, int, float]] = []
    init_length: int | None = None
    track: tuple[int, int, int] | None = None
    # A moof waits here until its mdat is complete: the file being recorded
    # right now ends in a fragment that is still being written.
    pending: tuple[int, float] | None = None
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(0)
        offset = 0
        while (header := _read_box_header(handle)) is not None:
            kind, header_length, length = header
            if length < header_length:
                raise FragmentIndexError("invalid box size")
            if offset + length > size:
                break
            if kind == b"moov":
                track = _video_track(handle.read(length - header_length))
            elif kind == b"moof":
                if track is None:
                    raise FragmentIndexError("moof before moov")
                if length > _MAX_MOOF_BYTES:
                    raise FragmentIndexError("moof too large")
                if init_length is None:
                    init_length = offset
                body = handle.read(length - header_length)
                pending = (offset, _fragment_ticks(body, track[0], track[2]) / track[1])
            elif kind == b"mdat" and pending is not None:
                # The chunk runs from its moof to the end of the mdat after it.
                start, seconds = pending
                fragments.append((start, offset + length - start, seconds))
                pending = None
            offset += length
            handle.seek(offset)
    if init_length is None or not fragments:
        raise FragmentIndexError("not a fragmented MP4")

    chunks: list[Chunk] = []
    start, length, seconds = fragments[0]
    for fragment_start, fragment_length, fragment_seconds in fragments[1:]:
        if seconds >= CHUNK_TARGET_SECONDS:
            chunks.append(Chunk(start, length, seconds))
            start, length, seconds = fragment_start, 0, 0.0
        length = fragment_start + fragment_length - start
        seconds += fragment_seconds
    chunks.append(Chunk(start, length, seconds))
    return FragmentIndex(init_length=init_length, chunks=tuple(chunks))


def apple_init(header: bytes) -> bytes:
    """The init header with HEVC labelled the way Apple's players accept.

    ffmpeg copies a camera's H.265 into a ``hev1`` sample entry; Safari and iOS
    play HEVC only from ``hvc1``. The two differ only in whether parameter sets
    may also travel in-band, which every player here handles either way.
    """
    patched = bytearray(header)
    at = patched.find(b"stsd")
    while at != -1:
        # 'stsd', version and flags, entry count, then the first entry's size.
        entry = at + 4 + 4 + 4 + 4
        if patched[entry : entry + 4] == b"hev1":
            patched[entry : entry + 4] = b"hvc1"
        at = patched.find(b"stsd", at + 4)
    return bytes(patched)


@lru_cache(maxsize=4096)
def _cached(path: str, size: int, mtime_ns: int) -> FragmentIndex:
    del size, mtime_ns  # cache key only: a rewritten file gets a fresh index
    return _index(Path(path))


def index_fragments(path: Path) -> FragmentIndex:
    """The init header and byte-range chunks of one recorded segment file."""
    stat = path.stat()
    return _cached(str(path), stat.st_size, stat.st_mtime_ns)
