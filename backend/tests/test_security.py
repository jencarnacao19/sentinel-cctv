"""Security primitives: redaction, credential encryption, passwords, paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.security.crypto import (
    DecryptionError,
    decrypt_secret,
    encrypt_secret,
    try_decrypt_secret,
)
from app.security.passwords import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.security.paths import (
    UnsafePathError,
    is_within,
    resolve_within,
    safe_relative_segments,
)
from app.security.redaction import redact_mapping, redact_text, redact_url


class TestRedaction:
    def test_rtsp_credentials_are_removed(self) -> None:
        assert (
            redact_url("rtsp://admin:hunter2@192.168.1.50:554/stream1")
            == "rtsp://***:***@192.168.1.50:554/stream1"
        )

    def test_username_only_userinfo_is_removed(self) -> None:
        assert redact_url("rtsp://admin@192.168.1.50/stream1") == "rtsp://***@192.168.1.50/stream1"

    def test_url_without_credentials_is_untouched(self) -> None:
        url = "rtsp://192.168.1.50:554/stream1"
        assert redact_url(url) == url

    def test_multiple_urls_in_one_line(self) -> None:
        line = "failover rtsp://a:b@10.0.0.1/main to rtsps://c:d@10.0.0.2/sub"
        assert "a:b" not in redact_url(line)
        assert "c:d" not in redact_url(line)

    @pytest.mark.parametrize(
        "text",
        [
            "password=hunter2",
            "password: hunter2",
            'token="abc123"',
            "api_key = zzz",
            "Authorization: Bearer sctv_secretvalue",
        ],
    )
    def test_key_value_secrets_are_removed(self, text: str) -> None:
        redacted = redact_text(text)
        assert "hunter2" not in redacted
        assert "abc123" not in redacted
        assert "zzz" not in redacted
        assert "sctv_secretvalue" not in redacted

    def test_bearer_credential_is_removed_not_just_the_scheme(self) -> None:
        redacted = redact_text("Authorization: Bearer sctv_abcdefghijkl")
        assert "sctv_abcdefghijkl" not in redacted
        assert "Bearer" not in redacted

    def test_bare_sentinel_token_is_removed_anywhere(self) -> None:
        redacted = redact_text("go2rtc failed for stream sctv_AbCd1234efgh in worker 3")
        assert "sctv_AbCd1234efgh" not in redacted
        assert "worker 3" in redacted

    def test_mapping_redaction_is_recursive(self) -> None:
        payload = {
            "camera": {
                "name": "Entrada",
                "password": "hunter2",
                "main_stream_url": "rtsp://admin:hunter2@10.0.0.5/main",
            },
            "tokens": [{"token": "sctv_abc"}],
        }
        redacted = redact_mapping(payload)
        assert redacted["camera"]["password"] == "***"
        assert redacted["camera"]["name"] == "Entrada"
        assert "hunter2" not in redacted["camera"]["main_stream_url"]
        assert redacted["tokens"][0]["token"] == "***"

    def test_mapping_redaction_preserves_none(self) -> None:
        assert redact_mapping({"password": None}) == {"password": None}


class TestCredentialEncryption:
    def test_round_trip(self, settings: Settings) -> None:
        del settings  # fixture activates the temporary install secret
        token = encrypt_secret("hunter2")
        assert token != "hunter2"
        assert decrypt_secret(token) == "hunter2"

    def test_ciphertext_differs_between_calls(self, settings: Settings) -> None:
        del settings
        assert encrypt_secret("hunter2") != encrypt_secret("hunter2")

    def test_unicode_password(self, settings: Settings) -> None:
        del settings
        secret = "palavra-pássaro-ção"
        assert decrypt_secret(encrypt_secret(secret)) == secret

    def test_tampered_token_is_rejected(self, settings: Settings) -> None:
        del settings
        token = encrypt_secret("hunter2")
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(DecryptionError):
            decrypt_secret(tampered)

    def test_try_decrypt_returns_none_instead_of_raising(self, settings: Settings) -> None:
        del settings
        assert try_decrypt_secret("not-a-valid-token") is None
        assert try_decrypt_secret(None) is None

    def test_secret_file_is_created_once(self, settings: Settings) -> None:
        first = settings.load_or_create_secret()
        second = settings.load_or_create_secret()
        assert first == second
        assert settings.secret_key_path.exists()


class TestPasswords:
    def test_hash_and_verify(self) -> None:
        stored = hash_password("correct-horse-battery")
        assert stored != "correct-horse-battery"
        assert stored.startswith("$argon2id$")
        assert verify_password(stored, "correct-horse-battery")

    def test_wrong_password_returns_false(self) -> None:
        stored = hash_password("correct-horse-battery")
        assert not verify_password(stored, "wrong")

    def test_malformed_hash_returns_false(self) -> None:
        assert not verify_password("not-a-hash", "anything")

    def test_salt_makes_hashes_unique(self) -> None:
        assert hash_password("same") != hash_password("same")

    @pytest.mark.parametrize(
        "password,expect_problem",
        [
            ("short", True),
            ("password", True),
            ("  leading-space-pass", True),
            ("a-perfectly-fine-password", False),
        ],
    )
    def test_strength_validation(self, password: str, expect_problem: bool) -> None:
        assert bool(validate_password_strength(password)) is expect_problem


class TestPathSafety:
    def test_resolves_inside_root(self, tmp_path: Path) -> None:
        target = tmp_path / "recordings" / "clip.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        assert resolve_within(target, tmp_path) == target.resolve()

    def test_relative_path_is_joined_to_root(self, tmp_path: Path) -> None:
        assert resolve_within("a/b.mp4", tmp_path) == (tmp_path / "a" / "b.mp4").resolve()

    @pytest.mark.parametrize(
        "candidate",
        ["../outside.mp4", "../../etc/passwd", "a/../../outside.mp4"],
    )
    def test_traversal_is_rejected(self, tmp_path: Path, candidate: str) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(UnsafePathError):
            resolve_within(candidate, root)

    def test_is_within_helper(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        assert is_within(root / "a.mp4", root)
        assert not is_within(tmp_path / "b.mp4", root)

    def test_safe_relative_segments(self) -> None:
        assert safe_relative_segments("2026/08/13/seg.mp4") == ["2026", "08", "13", "seg.mp4"]

    @pytest.mark.parametrize(
        "value", ["/absolute", "..", "a/../../b", "C:/windows", "", "a\\..\\..\\b"]
    )
    def test_safe_relative_segments_rejects_bad_input(self, value: str) -> None:
        with pytest.raises(UnsafePathError):
            safe_relative_segments(value)
