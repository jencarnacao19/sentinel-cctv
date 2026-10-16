"""Structured logging: categories, redaction and reserved-key safety."""

from __future__ import annotations

import logging

import pytest

from app.logging_config import (
    CATEGORIES,
    ConsoleFormatter,
    JsonFormatter,
    RedactionFilter,
    category_for_logger,
    configure_logging,
    get_logger,
)


class TestCategories:
    def test_every_documented_category_has_a_logger(self) -> None:
        for category in CATEGORIES:
            assert get_logger(category).logger.name.startswith("sentinel.")

    def test_logger_names_map_back_to_their_category(self) -> None:
        assert category_for_logger("sentinel.recording") == "RECORDING"
        assert category_for_logger("sentinel.recording.worker") == "RECORDING"
        assert category_for_logger("uvicorn.error") == "EXTERNAL"


class TestReservedKeys:
    """``logging`` raises KeyError when an ``extra`` key shadows a built-in
    LogRecord attribute. Inside a supervisor loop that turns a log line into a
    crash, and the supervised process never starts."""

    @pytest.mark.parametrize(
        "key", ["process", "module", "name", "message", "filename", "args", "levelname"]
    )
    def test_a_reserved_key_does_not_raise(
        self, key: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = get_logger("SYSTEM")
        with caplog.at_level(logging.INFO):
            logger.info("Starting", extra={key: "value"})
        assert "Starting" in caplog.text

    def test_the_context_is_preserved_under_a_renamed_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = get_logger("GO2RTC")
        with caplog.at_level(logging.INFO):
            logger.info("Starting", extra={"process": "go2rtc"})
        assert caplog.records[0].ctx_process == "go2rtc"  # type: ignore[attr-defined]

    def test_a_safe_key_is_left_alone(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_logger("CAMERA")
        with caplog.at_level(logging.INFO):
            logger.info("Camera added", extra={"camera": "Porch"})
        assert caplog.records[0].camera == "Porch"  # type: ignore[attr-defined]

    def test_exception_logging_also_survives(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_logger("SYSTEM")
        with caplog.at_level(logging.ERROR):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception("Failed", extra={"process": "recorder"})
        assert "Failed" in caplog.text
        assert "boom" in caplog.text


class TestRedactionInLogs:
    def _record(self, message: str, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            "sentinel.camera", logging.INFO, __file__, 1, message, None, None
        )
        record.__dict__.update(extra)
        RedactionFilter().filter(record)
        return record

    def test_credentials_in_the_message_are_removed(self) -> None:
        record = self._record("connecting to rtsp://admin:hunter2@10.0.0.5/main")
        assert "hunter2" not in record.getMessage()
        assert "***" in record.getMessage()

    def test_credentials_in_an_extra_field_are_removed(self) -> None:
        record = self._record("probe", url="rtsp://admin:hunter2@10.0.0.5/main")
        assert "hunter2" not in record.url  # type: ignore[attr-defined]

    def test_credentials_in_format_arguments_are_removed(self) -> None:
        record = logging.LogRecord(
            "sentinel.camera",
            logging.INFO,
            __file__,
            1,
            "%s failed",
            ("rtsp://admin:hunter2@10.0.0.5/main",),
            None,
        )
        RedactionFilter().filter(record)
        assert "hunter2" not in record.getMessage()

    def test_console_output_is_redacted(self) -> None:
        record = logging.LogRecord(
            "sentinel.camera",
            logging.INFO,
            __file__,
            1,
            "opening rtsp://admin:hunter2@10.0.0.5/main",
            None,
            None,
        )
        assert "hunter2" not in ConsoleFormatter().format(record)

    def test_json_output_is_redacted_and_carries_the_category(self) -> None:
        import json

        record = logging.LogRecord(
            "sentinel.recording",
            logging.WARNING,
            __file__,
            1,
            "token=sctv_abcdefghijkl",
            None,
            None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert payload["category"] == "RECORDING"
        assert "sctv_abcdefghijkl" not in payload["message"]


class TestConfiguration:
    def test_configure_logging_is_idempotent(self) -> None:
        configure_logging("WARNING")
        first = len(logging.getLogger().handlers)
        configure_logging("WARNING")
        assert len(logging.getLogger().handlers) == first == 1

    def test_the_redaction_filter_is_installed_on_the_handler(self) -> None:
        configure_logging("WARNING")
        handler = logging.getLogger().handlers[0]
        assert any(isinstance(f, RedactionFilter) for f in handler.filters)
