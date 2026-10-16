"""A restart-on-exit wrapper around an external process.

Design notes that matter for reliability:

* **Backoff, not a tight loop.** A camera that is genuinely gone would otherwise
  have Sentinel spawning FFmpeg hundreds of times a minute. Delays step
  2s → 5s → 10s → 30s → 60s and then stay at 60s forever, because a camera
  unplugged for a week must still reconnect by itself when it comes back.
* **No give-up.** There is no maximum retry count. Giving up would mean a
  camera silently stops recording, which is the failure this product exists to
  prevent.
* **Reset on success.** A process that stayed up long enough to be considered
  healthy resets the backoff, so one bad night does not leave a camera on a
  60-second retry interval indefinitely.
* **Graceful stop.** SIGTERM first so FFmpeg can finalise the segment it is
  writing, then SIGKILL after a grace period.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Any

from app.database.base import utcnow
from app.logging_config import Category, get_logger
from app.security.redaction import redact_text

#: Delay before each restart attempt. The final value repeats indefinitely.
RESTART_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)

#: A process that survives this long is considered to have started successfully,
#: which resets the backoff ladder.
HEALTHY_UPTIME_SECONDS = 30.0

#: How long a process gets to exit after SIGTERM before it is killed.
TERMINATE_GRACE_SECONDS = 10.0


def backoff_delay(attempt: int) -> float:
    """Delay before restart attempt ``attempt`` (0-based)."""
    if attempt < 0:
        return RESTART_BACKOFF_SECONDS[0]
    index = min(attempt, len(RESTART_BACKOFF_SECONDS) - 1)
    return RESTART_BACKOFF_SECONDS[index]


class ProcessState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"


class SupervisedProcess:
    """Keeps one external process alive until it is explicitly stopped."""

    def __init__(
        self,
        name: str,
        command_factory: Callable[[], Sequence[str]],
        *,
        category: Category = "SYSTEM",
        on_start: Callable[[], Awaitable[None]] | None = None,
        on_exit: Callable[[int | None], Awaitable[None]] | None = None,
        on_output: Callable[[str], bool] | None = None,
        capture_output: bool = True,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        #: Called on every (re)start, so a restarted process picks up config
        #: changes -- a camera whose password changed reconnects with the new one.
        self._command_factory = command_factory
        self._logger = get_logger(category)
        self._on_start = on_start
        self._on_exit = on_exit
        #: Given each already-redacted output line. Returning True means the
        #: owner has accounted for it and this process must not log it
        #: individually -- the escape hatch that turns a camera emitting one
        #: corrupt-packet warning every 0.3 s into a periodic counted summary
        #: instead of tens of thousands of identical log lines.
        self._on_output = on_output
        self._capture_output = capture_output
        self._env = env
        self._cwd = cwd

        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = asyncio.Event()
        self._restart_requested = False

        self.state: ProcessState = ProcessState.STOPPED
        self.restart_count = 0
        self.consecutive_failures = 0
        self.last_exit_code: int | None = None
        self.last_uptime_seconds: float | None = None
        self.last_exit_expected = False
        self.last_error: str | None = None
        self.started_at: Any = None

    # --- Lifecycle -------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Begin supervising. Returns immediately; the process starts in the
        background so one slow camera cannot delay application start-up."""
        if self._task is not None and not self._task.done():
            return
        self._stop_requested.clear()
        self.consecutive_failures = 0
        self._task = asyncio.create_task(self._supervise(), name=f"supervise:{self.name}")

    async def stop(self) -> None:
        """Stop the process and the supervision loop."""
        self._stop_requested.set()
        await self._terminate()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.state = ProcessState.STOPPED

    async def restart(self) -> None:
        """Stop the current process; the supervision loop starts a new one.

        Used when configuration changed and the process must pick it up.
        """
        if not self.is_running:
            await self.start()
            return
        self._logger.info("Restarting process", extra={"service": self.name})
        self._restart_requested = True
        await self._terminate()

    # --- Internals -------------------------------------------------------

    async def _supervise(self) -> None:
        while not self._stop_requested.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = redact_text(str(exc))
                self.state = ProcessState.FAILED
                self._logger.exception("Supervisor iteration failed", extra={"service": self.name})

            if self._stop_requested.is_set():
                break

            delay = backoff_delay(self.consecutive_failures - 1)
            self.state = ProcessState.RESTARTING
            self._logger.info(
                "Restarting after exit",
                extra={
                    "service": self.name,
                    "delay_seconds": delay,
                    "consecutive_failures": self.consecutive_failures,
                },
            )
            # Waiting on the stop event rather than sleeping means a shutdown
            # during a 60-second backoff is immediate, not 60 seconds late.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_requested.wait(), timeout=delay)

        self.state = ProcessState.STOPPED

    async def _run_once(self) -> None:
        command = list(self._command_factory())
        self.state = ProcessState.STARTING
        self._logger.info(
            "Starting process",
            extra={"service": self.name, "argv0": command[0] if command else "?"},
        )

        pipe = asyncio.subprocess.PIPE if self._capture_output else asyncio.subprocess.DEVNULL
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=pipe,
                stderr=pipe,
                env={**os.environ, **self._env} if self._env else None,
                cwd=self._cwd,
            )
        except FileNotFoundError as exc:
            self.consecutive_failures += 1
            self.state = ProcessState.FAILED
            self.last_error = f"Executable not found: {command[0] if command else '?'}"
            raise RuntimeError(self.last_error) from exc

        self.state = ProcessState.RUNNING
        self.started_at = utcnow()
        started_monotonic = asyncio.get_running_loop().time()
        if self._on_start is not None:
            await self._on_start()

        stderr_task = (
            asyncio.create_task(self._drain(self._process.stderr), name=f"stderr:{self.name}")
            if self._capture_output and self._process.stderr is not None
            else None
        )
        if self._capture_output and self._process.stdout is not None:
            # Never left unread: a full pipe buffer would block the child.
            asyncio.create_task(  # noqa: RUF006 - fire-and-forget drain
                self._drain(self._process.stdout, level="debug"), name=f"stdout:{self.name}"
            )

        return_code = await self._process.wait()
        self.last_exit_code = return_code
        uptime = asyncio.get_running_loop().time() - started_monotonic
        self.last_uptime_seconds = uptime
        self.last_exit_expected = self._stop_requested.is_set() or self._restart_requested
        self._restart_requested = False

        if stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(stderr_task, timeout=2)

        if self.last_exit_expected:
            self.consecutive_failures = 0
        elif uptime >= HEALTHY_UPTIME_SECONDS:
            # It ran fine and then died: treat it as the first failure of a new
            # incident rather than continuing an old backoff ladder.
            self.consecutive_failures = 1
        else:
            self.consecutive_failures += 1

        if not self.last_exit_expected:
            self.restart_count += 1
            self._logger.warning(
                "Process exited",
                extra={
                    "service": self.name,
                    "returncode": return_code,
                    "uptime_seconds": round(uptime, 1),
                },
            )

        if self._on_exit is not None:
            await self._on_exit(return_code)

    async def _drain(self, stream: asyncio.StreamReader | None, *, level: str = "warning") -> None:
        """Forward child output into Sentinel's logs, redacted."""
        if stream is None:
            return
        log = getattr(self._logger, level)
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                redacted = redact_text(text)
                self.last_error = redacted
                if self._on_output is not None:
                    try:
                        if self._on_output(redacted):
                            continue
                    except Exception:  # noqa: BLE001 - accounting must not stop draining
                        self._logger.debug("Output accounting failed", extra={"service": self.name})
                log("%s: %s", self.name, redacted)
        except (asyncio.CancelledError, ValueError):
            raise
        except Exception:  # noqa: BLE001 - draining must never kill supervision
            self._logger.debug("Output drain stopped", extra={"service": self.name})

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            # SIGTERM lets FFmpeg finalise the MP4 it is writing. A killed FFmpeg
            # leaves a segment with no moov atom, which is an unplayable file.
            process.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            self._logger.warning(
                "Process ignored termination; killing", extra={"service": self.name}
            )
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)

    # --- Reporting -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "pid": self._process.pid if self.is_running and self._process else None,
            "restart_count": self.restart_count,
            "consecutive_failures": self.consecutive_failures,
            "last_exit_code": self.last_exit_code,
            "last_uptime_seconds": self.last_uptime_seconds,
            "last_exit_expected": self.last_exit_expected,
            "last_error": self.last_error,
            "started_at": self.started_at,
        }
