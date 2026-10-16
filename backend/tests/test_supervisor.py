"""Process supervision: backoff, restart and graceful stop."""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.supervisor import RESTART_BACKOFF_SECONDS, ProcessState, SupervisedProcess, backoff_delay


def python_command(script: str) -> list[str]:
    return [sys.executable, "-c", script]


class TestBackoff:
    def test_ladder_matches_the_documented_delays(self) -> None:
        delays = [backoff_delay(attempt) for attempt in range(len(RESTART_BACKOFF_SECONDS))]
        assert delays == [2.0, 5.0, 10.0, 30.0, 60.0]

    def test_it_plateaus_rather_than_growing_without_bound(self) -> None:
        """A camera unplugged for a week must still reconnect on its own, so the
        delay stops growing instead of backing off to hours."""
        assert backoff_delay(50) == 60.0
        assert backoff_delay(10_000) == 60.0

    def test_negative_attempt_is_treated_as_the_first(self) -> None:
        assert backoff_delay(-1) == 2.0


class TestLifecycle:
    async def test_a_long_running_process_reaches_running(self) -> None:
        process = SupervisedProcess(
            "sleeper", lambda: python_command("import time; time.sleep(30)")
        )
        await process.start()
        try:
            for _ in range(100):
                if process.state is ProcessState.RUNNING:
                    break
                await asyncio.sleep(0.05)
            assert process.state is ProcessState.RUNNING
            assert process.is_running
            assert process.status()["pid"] is not None
        finally:
            await process.stop()

        assert process.state is ProcessState.STOPPED
        assert not process.is_running

    async def test_stop_is_idempotent(self) -> None:
        process = SupervisedProcess("noop", lambda: python_command("pass"))
        await process.start()
        await process.stop()
        await process.stop()
        assert process.state is ProcessState.STOPPED

    async def test_a_crashing_process_is_restarted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Shrink the ladder so the test does not wait two real seconds.
        monkeypatch.setattr("app.supervisor.process.RESTART_BACKOFF_SECONDS", (0.01,))
        process = SupervisedProcess("crasher", lambda: python_command("import sys; sys.exit(1)"))
        await process.start()
        try:
            for _ in range(200):
                if process.restart_count >= 3:
                    break
                await asyncio.sleep(0.05)
            assert process.restart_count >= 3
            assert process.last_exit_code == 1
            assert process.consecutive_failures >= 1
        finally:
            await process.stop()

    async def test_a_missing_executable_does_not_raise_out_of_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing binary must degrade the subsystem, not crash the app."""
        monkeypatch.setattr("app.supervisor.process.RESTART_BACKOFF_SECONDS", (0.01,))
        process = SupervisedProcess(
            "ghost", lambda: ["definitely-not-a-real-binary-xyz", "--version"]
        )
        await process.start()
        try:
            for _ in range(200):
                if process.last_error:
                    break
                await asyncio.sleep(0.05)
            assert process.last_error is not None
            assert "not found" in process.last_error.lower()
        finally:
            await process.stop()

    async def test_the_command_is_rebuilt_on_every_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restarted process must pick up configuration changes -- a camera
        whose password changed has to reconnect with the new one."""
        monkeypatch.setattr("app.supervisor.process.RESTART_BACKOFF_SECONDS", (0.01,))
        calls = 0

        def factory() -> list[str]:
            nonlocal calls
            calls += 1
            return python_command("import sys; sys.exit(0)")

        process = SupervisedProcess("rebuilder", factory)
        await process.start()
        try:
            for _ in range(200):
                if calls >= 3:
                    break
                await asyncio.sleep(0.05)
            assert calls >= 3
        finally:
            await process.stop()

    async def test_an_unexpected_clean_exit_is_still_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.supervisor.process.RESTART_BACKOFF_SECONDS", (0.01,))
        process = SupervisedProcess("clean", lambda: python_command("pass"))
        await process.start()
        try:
            for _ in range(200):
                if process.restart_count >= 2:
                    break
                await asyncio.sleep(0.05)
            assert process.consecutive_failures >= 1
            assert process.last_exit_expected is False
        finally:
            await process.stop()

    async def test_an_explicit_restart_is_not_a_failure(self) -> None:
        process = SupervisedProcess(
            "planned-restart", lambda: python_command("import time; time.sleep(30)")
        )
        await process.start()
        try:
            for _ in range(100):
                if process.state is ProcessState.RUNNING:
                    break
                await asyncio.sleep(0.05)
            first_pid = process.status()["pid"]
            await process.restart()
            for _ in range(100):
                if process.state is ProcessState.RUNNING and process.status()["pid"] != first_pid:
                    break
                await asyncio.sleep(0.05)
            assert process.status()["pid"] != first_pid
            assert process.restart_count == 0
            assert process.last_exit_expected is True
            assert process.consecutive_failures == 0
        finally:
            await process.stop()

    async def test_stopping_during_backoff_is_immediate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Waiting on the stop event rather than sleeping means shutdown during a
        60-second backoff is instant, not a minute late."""
        monkeypatch.setattr("app.supervisor.process.RESTART_BACKOFF_SECONDS", (30.0,))
        process = SupervisedProcess("slow", lambda: python_command("import sys; sys.exit(1)"))
        await process.start()

        for _ in range(200):
            if process.state is ProcessState.RESTARTING:
                break
            await asyncio.sleep(0.05)
        assert process.state is ProcessState.RESTARTING

        loop = asyncio.get_running_loop()
        before = loop.time()
        await process.stop()
        assert loop.time() - before < 5.0

    async def test_status_reports_the_supervision_state(self) -> None:
        process = SupervisedProcess(
            "reporter", lambda: python_command("import time; time.sleep(5)")
        )
        await process.start()
        try:
            for _ in range(100):
                if process.state is ProcessState.RUNNING:
                    break
                await asyncio.sleep(0.05)
            status = process.status()
            assert status["name"] == "reporter"
            assert status["state"] == "running"
            assert status["started_at"] is not None
        finally:
            await process.stop()
