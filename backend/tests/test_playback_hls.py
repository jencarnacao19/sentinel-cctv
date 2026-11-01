"""Recorded footage as an HLS playlist of byte ranges into the files on disk."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from httpx import AsyncClient

from app.config import Settings
from app.recording.fragments import CHUNK_TARGET_SECONDS, index_fragments
from tests import test_export
from tests.test_export import requires_ffmpeg

# Imported as a module: a Test* name in this namespace would run its tests twice.
_pipeline = test_export.TestRealExportPipeline


def _files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.mp4"))


@requires_ffmpeg
class TestRecordedPlayback:
    """The export pipeline's real recorder output: 7 s cut into 2 s files."""

    CAMERA_ID = _pipeline.CAMERA_ID
    recorded_segments = _pipeline.recorded_segments
    _index = _pipeline._index

    def test_a_recorded_file_splits_into_keyframe_chunks_that_cover_it(
        self, recorded_segments: Path
    ) -> None:
        for file in _files(recorded_segments):
            index = index_fragments(file)
            data = file.read_bytes()
            assert data[index.init_length + 4 : index.init_length + 8] == b"moof"
            position = index.init_length
            for chunk in index.chunks:
                assert chunk.offset == position, "chunks must be contiguous"
                assert data[chunk.offset + 4 : chunk.offset + 8] == b"moof"
                position += chunk.length
            assert all(c.seconds >= CHUNK_TARGET_SECONDS for c in index.chunks[:-1])
            assert 0.5 <= index.seconds <= 3.5

    def test_a_file_still_being_written_indexes_its_complete_fragments(
        self, recorded_segments: Path, tmp_path: Path
    ) -> None:
        source = max(_files(recorded_segments), key=lambda f: f.stat().st_size)
        whole = index_fragments(source)
        partial = tmp_path / "partial.mp4"
        last = whole.chunks[-1]
        # Cut inside the last mdat, as a recorder mid-write leaves it.
        partial.write_bytes(source.read_bytes()[: last.offset + last.length - 200])
        cut = index_fragments(partial)
        assert cut.init_length == whole.init_length
        assert cut.seconds < whole.seconds

    async def test_a_session_serves_a_playlist_and_byte_ranges(
        self, settings: Settings, recorded_segments: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        camera_id, segments = await self._index(admin_client, recorded_segments)
        at = segments[0].started_at + timedelta(seconds=0.5)

        opened = await admin_client.post(
            f"/api/v1/cameras/{camera_id}/playback/session", params={"at": at.isoformat()}
        )
        assert opened.status_code == 200
        body = opened.json()
        assert body["url"].endswith("/index.m3u8")
        assert 0 <= body["start_position"] < 3
        assert body["items"][0]["position"] == 0
        assert len(body["items"]) >= 2

        playlist = await admin_client.get(body["url"])
        assert playlist.status_code == 200
        text = playlist.text
        assert text.startswith("#EXTM3U") and text.rstrip().endswith("#EXT-X-ENDLIST")
        assert text.count("#EXT-X-MAP:") == len(body["items"])
        assert text.count("#EXT-X-DISCONTINUITY") == len(body["items"]) - 1

        # The first chunk, exactly as a player asks for it.
        lines = text.splitlines()
        byterange = next(line for line in lines if line.startswith("#EXT-X-BYTERANGE:"))
        length, offset = (int(n) for n in byterange.split(":", 1)[1].split("@"))
        uri = lines[lines.index(byterange) + 1]
        base = body["url"].rsplit("/", 1)[0]
        chunk = await admin_client.get(
            f"{base}/{uri}", headers={"Range": f"bytes={offset}-{offset + length - 1}"}
        )
        assert chunk.status_code == 206
        assert len(chunk.content) == length
        assert chunk.content[4:8] == b"moof"

        # The grant serves only its own camera and the files its playlist names.
        token = body["url"].split("/")[-2]
        wrong = await admin_client.get(f"/api/v1/streams/other/recordings/{token}/index.m3u8")
        assert wrong.status_code == 404
        unknown = await admin_client.get(f"{base}/not-in-this-playlist.mp4")
        assert unknown.status_code == 404

    async def test_an_hour_without_footage_points_at_the_next_recording(
        self, settings: Settings, recorded_segments: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        camera_id, segments = await self._index(admin_client, recorded_segments)
        early = segments[0].started_at - timedelta(hours=3)

        opened = await admin_client.post(
            f"/api/v1/cameras/{camera_id}/playback/session", params={"at": early.isoformat()}
        )
        assert opened.status_code == 200
        body = opened.json()
        assert body["url"] is None
        assert body["next_at"] is not None
