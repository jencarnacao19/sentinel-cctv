"""Stream URL parsing: credential extraction and the scheme allow-list."""

from __future__ import annotations

import pytest

from app.cameras.rtsp import (
    ALLOWED_SNAPSHOT_SCHEMES,
    ALLOWED_STREAM_SCHEMES,
    InvalidStreamUrlError,
    build_stream_url,
    normalise_stream_url,
    parse_stream_url,
)


class TestParsing:
    def test_plain_url(self) -> None:
        parsed = parse_stream_url("rtsp://192.168.50.50:554/stream1")
        assert parsed.scheme == "rtsp"
        assert parsed.host == "192.168.50.50"
        assert parsed.port == 554
        assert parsed.path == "/stream1"
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.has_credentials is False

    def test_default_port_is_applied(self) -> None:
        assert parse_stream_url("rtsp://192.168.50.50/stream1").port == 554
        assert parse_stream_url("rtsps://192.168.50.50/stream1").port == 322

    def test_credentials_are_extracted_and_stripped(self) -> None:
        parsed = parse_stream_url("rtsp://admin:hunter2@192.168.50.50:554/stream1")
        assert parsed.username == "admin"
        assert parsed.password == "hunter2"
        assert parsed.sanitised_url == "rtsp://192.168.50.50:554/stream1"
        assert "hunter2" not in parsed.sanitised_url

    def test_percent_encoded_credentials_are_decoded(self) -> None:
        parsed = parse_stream_url("rtsp://admin:p%40ss%3Aword@10.0.0.5/main")
        assert parsed.password == "p@ss:word"

    def test_username_without_password(self) -> None:
        parsed = parse_stream_url("rtsp://admin@10.0.0.5/main")
        assert parsed.username == "admin"
        assert parsed.password is None
        assert parsed.has_credentials is True

    def test_query_string_is_preserved(self) -> None:
        parsed = parse_stream_url("rtsp://10.0.0.5:554/cam?channel=2&subtype=1")
        assert parsed.path == "/cam?channel=2&subtype=1"
        assert parsed.sanitised_url.endswith("/cam?channel=2&subtype=1")

    def test_ipv6_host_round_trips(self) -> None:
        parsed = parse_stream_url("rtsp://[fd00::1]:554/stream1")
        assert parsed.host == "fd00::1"
        assert parsed.sanitised_url == "rtsp://[fd00::1]:554/stream1"

    def test_missing_path_becomes_root(self) -> None:
        assert parse_stream_url("rtsp://10.0.0.5:554").path == "/"

    def test_normalise_helper(self) -> None:
        url, user, password = normalise_stream_url("rtsp://a:b@10.0.0.5/main")
        assert (url, user, password) == ("rtsp://10.0.0.5:554/main", "a", "b")


class TestRejectedInput:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "file://C:/Windows/System32/config/sam",
            "http://10.0.0.5/stream",
            "https://10.0.0.5/stream",
            "ftp://10.0.0.5/stream",
            "data:text/plain;base64,SGVsbG8=",
            "gopher://10.0.0.5/",
            "concat:/etc/passwd",
            "/etc/passwd",
            "10.0.0.5/stream1",
        ],
    )
    def test_disallowed_schemes_are_refused(self, url: str) -> None:
        with pytest.raises(InvalidStreamUrlError):
            parse_stream_url(url)

    def test_snapshot_schemes_are_a_separate_allow_list(self) -> None:
        parsed = parse_stream_url(
            "http://10.0.0.5/snapshot.jpg", allowed_schemes=ALLOWED_SNAPSHOT_SCHEMES
        )
        assert parsed.scheme == "http"
        assert parsed.port == 80
        assert "http" not in ALLOWED_STREAM_SCHEMES

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "rtsp://",
            "rtsp:///stream",
            "rtsp://10.0.0.5:0/stream",
            "rtsp://10.0.0.5:99999/stream",
            "rtsp://10.0.0.5:abc/stream",
            "rtsp://10.0.0.5/stream\nX-Injected: 1",
            "rtsp://10.0.0.5/stream with space",
            "rtsp://10.0.0.5/\x00",
        ],
    )
    def test_malformed_urls_are_refused(self, url: str) -> None:
        with pytest.raises(InvalidStreamUrlError):
            parse_stream_url(url)

    def test_overlong_url_is_refused(self) -> None:
        with pytest.raises(InvalidStreamUrlError, match="too long"):
            parse_stream_url("rtsp://10.0.0.5/" + "a" * 4000)

    def test_error_message_never_echoes_the_password(self) -> None:
        with pytest.raises(InvalidStreamUrlError) as exc_info:
            parse_stream_url("ftp://admin:hunter2@10.0.0.5/stream")
        assert "hunter2" not in str(exc_info.value)


class TestBuilding:
    def test_credentials_are_reattached(self) -> None:
        url = build_stream_url("rtsp://10.0.0.5:554/main", "admin", "hunter2")
        assert url == "rtsp://admin:hunter2@10.0.0.5:554/main"

    def test_no_credentials_leaves_the_url_untouched(self) -> None:
        base = "rtsp://10.0.0.5:554/main"
        assert build_stream_url(base, None, None) == base

    def test_special_characters_are_percent_encoded(self) -> None:
        """A password containing '@' must not be able to change the host."""
        url = build_stream_url("rtsp://10.0.0.5:554/main", "admin", "p@ss/w:rd")
        assert url == "rtsp://admin:p%40ss%2Fw%3Ard@10.0.0.5:554/main"
        assert parse_stream_url(url).host == "10.0.0.5"
        assert parse_stream_url(url).password == "p@ss/w:rd"

    def test_username_only(self) -> None:
        assert (
            build_stream_url("rtsp://10.0.0.5:554/main", "admin", None)
            == "rtsp://admin@10.0.0.5:554/main"
        )

    def test_round_trip_through_parse_and_build(self) -> None:
        original = "rtsp://admin:hunter2@192.168.50.50:554/stream1?x=1"
        parsed = parse_stream_url(original)
        rebuilt = build_stream_url(parsed.sanitised_url, parsed.username, parsed.password)
        assert parse_stream_url(rebuilt).password == "hunter2"
        assert parse_stream_url(rebuilt).path == "/stream1?x=1"

    def test_ipv6_stays_bracketed(self) -> None:
        url = build_stream_url("rtsp://[fd00::1]:554/main", "admin", "pw")
        assert url == "rtsp://admin:pw@[fd00::1]:554/main"
        assert parse_stream_url(url).host == "fd00::1"
