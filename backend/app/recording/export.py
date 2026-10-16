"""Export: a stand-alone clip cut from recorded segments.

An export always stream-copies -- the same reasoning as continuous recording
itself: re-encoding costs CPU a small server does not have, and Sentinel would
rather be honest about a keyframe-accurate start than pretend to a frame-
accurate one it cannot deliver without transcoding.

The job is a row before it is a file. Concatenating even a couple of hours of
footage takes long enough that holding the HTTP request open would fail on any
reverse proxy with a sane timeout (see the ``ExportJob`` docstring), so
``POST /exports`` only ever queues one; :class:`ExportManager` drains the queue
in the background, one job at a time -- a small server has one set of disks and
one CPU core to spare for this, and running exports concurrently would only
make all of them slower.

Two things happen at the FFmpeg boundary that are easy to get wrong and are
called out here on purpose:

* The concat demuxer's list file takes an absolute path per line, wrapped in
  single quotes; a segment path containing one would truncate the line and
  corrupt everything after it, so it is escaped defensively even though
  Sentinel's own filenames never produce one.
* ``-c copy`` cannot start mid-GOP. The concat demuxer's ``inpoint`` seeks to
  the nearest keyframe at or before it, so the clip can start a fraction of a
  second earlier than requested. That is stated in the API response, not
  hidden -- fixing it would mean re-encoding, which Sentinel does not do.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database.models import Camera, ExportJob, ExportStatus, RecordingSegment
from app.database.session import session_scope
from app.logging_config import get_logger
from app.recording import paths, timeline
from app.security.paths import UnsafePathError, resolve_within
from app.storage.root import get_storage_root

logger = get_logger("RECORDING")

#: Sub-directory of the storage root exports live in. Dot-prefixed so it reads
#: as Sentinel-internal next to the per-camera directories, the same idea as
#: the storage marker file.
EXPORTS_DIRNAME = ".exports"

#: How often the worker loop checks for a newly queued job when nothing woke
#: it early. ``notify()`` covers the common case; this is only the ceiling.
EXPORT_POLL_SECONDS = 2.0

#: How often the background loop checks for temporary exports past their
#: expiry. Permanent saved clips are excluded from this sweep.
EXPORT_SWEEP_INTERVAL_SECONDS = 300.0

#: How long a completed *temporary* export stays downloadable before the next
#: sweep expires it. New clips are permanent by default; an administrator may
#: deliberately return one to this policy from the Saved clips page.
EXPORT_RETENTION_HOURS = 24.0

#: Minimum spacing between progress writes to the database. FFmpeg's
#: ``-progress`` pipe can emit dozens of lines a second for a fast stream copy;
#: writing every one would turn a copy operation into a database benchmark.
PROGRESS_MIN_INTERVAL_SECONDS = 1.0

#: Grace given to a terminated FFmpeg process to exit before giving up on it
#: during shutdown or a user-initiated delete.
CANCEL_WAIT_SECONDS = 10.0

KEYFRAME_ACCURACY_NOTE = (
    "Exports are trimmed without re-encoding, so the start of the clip lands on "
    "the nearest keyframe at or before the selected time rather than the exact "
    "frame. Sentinel never re-encodes footage, so frame-accurate cuts are not "
    "offered."
)

_OUT_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:\.(\d+))?$")


class EmptyExportRangeError(timeline.TimelineRangeError):
    """The requested range has no recorded footage in it at all.

    A subclass of the timeline's own range error so a router only needs one
    ``except`` clause to turn any bad-range condition into a 422.
    """


# --- Paths ----------------------------------------------------------------


def exports_root(storage_root: Path) -> Path:
    return storage_root / EXPORTS_DIRNAME


def output_path(storage_root: Path, job_id: str) -> Path:
    return exports_root(storage_root) / f"{job_id}.mp4"


def _list_path(storage_root: Path, job_id: str) -> Path:
    return exports_root(storage_root) / f"{job_id}.concat.txt"


def resolve_output_file(storage_root: Path, job: ExportJob) -> Path:
    """Resolve a completed job's file, proven to stay inside the exports root.

    ``file_path`` came from Sentinel's own writer, but it is still resolved
    defensively -- the same reasoning ``download_segment`` applies to a
    recording's path.
    """
    if not job.file_path:
        raise UnsafePathError("This export has no file.")
    root = exports_root(storage_root)
    return resolve_within(root / job.file_path, root)


def _concat_escape(value: str) -> str:
    """Escape a path for the concat demuxer's single-quoted ``file`` directive.

    The demuxer's own escape for an embedded quote is to close the quote,
    insert an escaped literal quote, and reopen it -- ``'\\''`` -- mirroring
    the convention a POSIX shell uses for the same problem. Sentinel's own
    segment filenames are generated from a fixed pattern and never contain a
    quote, but a path built from a database row is worth escaping regardless:
    an unescaped one would truncate this line and corrupt every line after it.
    """
    return value.replace("'", "'\\''")


# --- Domain functions -------------------------------------------------------


async def reconcile_stale_jobs(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Fail jobs left ``running`` by an unclean shutdown.

    FFmpeg cannot have survived the process dying with it, so whatever partial
    file exists is not usable; the job is marked failed rather than left
    silently claiming progress that stopped applying the moment Sentinel did.
    Call once at start-up, before the worker loop begins pulling from the
    queue.
    """
    moment = now or datetime.now(UTC)
    rows = await session.execute(select(ExportJob).where(ExportJob.status == ExportStatus.RUNNING))
    stale = list(rows.scalars())
    if not stale:
        return 0

    for job in stale:
        job.status = ExportStatus.FAILED
        job.error = "Interrupted by a restart before it finished."
        job.finished_at = moment
    await session.flush()

    logger.warning(
        "Failed export jobs left running by an unclean shutdown",
        extra={"count": len(stale)},
    )
    return len(stale)


async def expire_old_jobs(
    session: AsyncSession, storage_root: Path, *, now: datetime | None = None
) -> int:
    """Delete temporary export files past their retention window.

    Permanent saved clips never match this query. Source recordings remain
    governed by their own retention policy independently of either outcome.
    """
    moment = now or datetime.now(UTC)
    rows = await session.execute(
        select(ExportJob).where(
            ExportJob.status == ExportStatus.COMPLETE,
            ExportJob.protected.is_(False),
            ExportJob.expires_at.is_not(None),
            ExportJob.expires_at <= moment,
        )
    )
    stale = list(rows.scalars())
    if not stale:
        return 0

    for job in stale:
        if job.file_path:
            absolute = exports_root(storage_root) / job.file_path
            with contextlib.suppress(FileNotFoundError, OSError):
                await asyncio.to_thread(absolute.unlink)
        job.status = ExportStatus.EXPIRED
        job.file_path = None
        job.size_bytes = None
    await session.flush()

    logger.info("Expired old export files", extra={"count": len(stale)})
    return len(stale)


async def create_job(
    session: AsyncSession,
    camera_id: str,
    start: datetime,
    end: datetime,
    *,
    protected: bool = False,
) -> ExportJob:
    """Validate the range and queue an export.

    Builds the same plan the worker will build again later, purely to reject
    an empty range up front -- better a clear message now than a job that
    fails a few seconds later, or worse, a zero-byte file nobody asked for.
    """
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise LookupError(f"Camera {camera_id} does not exist.")

    plan = await timeline.build_export_plan(session, camera_id, start, end)
    if not plan.parts:
        raise EmptyExportRangeError("There is no recorded footage in that range to export.")

    job = ExportJob(
        camera_id=camera_id,
        start_at=plan.start,
        end_at=plan.end,
        status=ExportStatus.QUEUED,
        protected=protected,
    )
    session.add(job)
    await session.flush()
    logger.info(
        "Export queued",
        extra={
            "job_id": job.id,
            "camera_id": camera_id,
            "requested_seconds": round(plan.total_seconds, 1),
            "parts": len(plan.parts),
            "complete": plan.complete,
        },
    )
    return job


def set_protected(
    job: ExportJob,
    protected: bool,
    *,
    now: datetime | None = None,
) -> ExportJob:
    """Save a clip permanently, or return it to the 24-hour expiry policy."""
    job.protected = protected
    if protected:
        job.expires_at = None
    elif job.status is ExportStatus.COMPLETE and job.file_path is not None:
        job.expires_at = (now or datetime.now(UTC)) + timedelta(hours=EXPORT_RETENTION_HOURS)
    return job


async def list_jobs(
    session: AsyncSession,
    *,
    camera_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExportJob], int]:
    filters = []
    if camera_id:
        filters.append(ExportJob.camera_id == camera_id)

    total = await session.scalar(select(func.count()).select_from(ExportJob).where(*filters))
    rows = await session.execute(
        select(ExportJob)
        .where(*filters)
        .order_by(ExportJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars()), int(total or 0)


async def _segment_paths(session: AsyncSession, segment_ids: list[str]) -> dict[str, str]:
    if not segment_ids:
        return {}
    rows = await session.execute(
        select(RecordingSegment.id, RecordingSegment.path).where(
            RecordingSegment.id.in_(segment_ids)
        )
    )
    return dict(rows.all())  # type: ignore[arg-type]


def _write_concat_list(
    list_path: Path,
    parts: list[timeline.ExportPart],
    segment_paths: dict[str, str],
    storage_root: Path,
) -> list[str]:
    """Write the concat demuxer's list file. Returns ids that had to be skipped
    because their file vanished between planning and writing -- the caller
    treats those as gaps rather than failing the whole export."""
    lines = ["ffconcat version 1.0"]
    missing: list[str] = []
    for part in parts:
        relative = segment_paths.get(part.segment_id)
        if relative is None:
            missing.append(part.segment_id)
            continue
        # Forward slashes even on Windows: they are unambiguous inside the
        # demuxer's quoted directive, whereas a literal backslash risks being
        # read as the start of an escape sequence.
        absolute = paths.absolute_path(storage_root, relative).as_posix()
        lines.append(f"file '{_concat_escape(absolute)}'")
        if part.trim_start_seconds > 0:
            lines.append(f"inpoint {part.trim_start_seconds:.3f}")
        if part.trim_end_seconds > 0:
            lines.append(f"outpoint {part.trim_start_seconds + part.duration_seconds:.3f}")

    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return missing


def build_export_command(ffmpeg_binary: str, list_file: Path, destination: Path) -> list[str]:
    """FFmpeg arguments for a concat-demuxer, stream-copy export.

    Built as a list and never joined into a shell string, the same rule every
    other FFmpeg call in Sentinel follows.
    """
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        # Without this the demuxer refuses an absolute path (or one holding a
        # character it treats as special) -- which is exactly what every entry
        # in the list file is.
        "-safe",
        "0",
        "-i",
        str(list_file),
        # Mirrors the recorder's own reasoning: the explicit map keeps a
        # multi-stream segment from picking the wrong video track, and the
        # '?' lets a camera with no audio export without one.
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        # Moves the moov atom to the front so a browser or media player can
        # start playing a downloaded export before the whole file has arrived.
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(destination),
    ]


def _parse_out_time(value: str) -> float | None:
    """Parse FFmpeg's ``-progress`` ``out_time=HH:MM:SS.ffffff`` value."""
    match = _OUT_TIME_RE.match(value.strip())
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    if fraction:
        total += int(fraction) / (10 ** len(fraction))
    return float(total)


async def _update_progress(job_id: str, fraction: float) -> None:
    async with session_scope() as session:
        job = await session.get(ExportJob, job_id)
        # Only while still running: a cancel or a completion that raced ahead
        # of this update must not be overwritten by a stale fraction.
        if job is not None and job.status is ExportStatus.RUNNING:
            job.progress = round(max(0.0, min(1.0, fraction)), 4)


async def _pump_progress(job_id: str, stdout: asyncio.StreamReader, total_seconds: float) -> None:
    """Translate FFmpeg's progress pipe into periodic database updates."""
    last_write = 0.0
    while True:
        raw = await stdout.readline()
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue

        if key == "out_time" and total_seconds > 0:
            elapsed = _parse_out_time(value)
            if elapsed is None:
                continue
            now = time.monotonic()
            if now - last_write >= PROGRESS_MIN_INTERVAL_SECONDS:
                last_write = now
                await _update_progress(job_id, elapsed / total_seconds)
        elif key == "progress" and value == "end":
            await _update_progress(job_id, 1.0)
            return


@dataclass(slots=True)
class _RunningJob:
    job_id: str
    process: asyncio.subprocess.Process | None


class ExportManager:
    """Owns the export queue: one job running at a time, drained in the
    background so ``POST /exports`` never blocks on FFmpeg."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._loop_task: asyncio.Task[None] | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._current: _RunningJob | None = None
        self._cancelled: set[str] = set()

    # --- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        async with session_scope() as session:
            await reconcile_stale_jobs(session)

        if self._settings.testing:
            # Tests drive run_pending_once() directly, the same way recording
            # tests drive the indexer directly rather than the manager's loop.
            return
        self._loop_task = asyncio.create_task(self._worker_loop(), name="export-worker")
        self._expiry_task = asyncio.create_task(self._expiry_loop(), name="export-expiry")

    async def stop(self) -> None:
        if self._current is not None:
            self.request_cancel(self._current.job_id)
            # Give the in-flight job a chance to notice the terminated process
            # and commit its own final state before the loop task is cancelled
            # out from under it -- cancelling mid-write would race that commit.
            waited = 0.0
            while self._current is not None and waited < CANCEL_WAIT_SECONDS:
                await asyncio.sleep(0.1)
                waited += 0.1

        for task in (self._loop_task, self._expiry_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._loop_task = None
        self._expiry_task = None

    def notify(self) -> None:
        """Wake the worker loop promptly after a job is queued, rather than
        waiting for the next poll tick."""
        self._wake.set()

    # --- Worker loop ---------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            processed = await self.run_pending_once()
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=EXPORT_POLL_SECONDS)
                self._wake.clear()

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(EXPORT_SWEEP_INTERVAL_SECONDS)
            try:
                async with session_scope() as session:
                    root = await get_storage_root(session, self._settings)
                    await expire_old_jobs(session, root)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Export expiry sweep failed", exc_info=True)

    async def run_pending_once(self) -> bool:
        """Process the oldest queued job, if one exists. Returns whether one
        was found. Exposed directly so tests can drive a real FFmpeg run
        without waiting on the background loop's own timing."""
        async with session_scope() as session:
            job_id = await session.scalar(
                select(ExportJob.id)
                .where(ExportJob.status == ExportStatus.QUEUED)
                .order_by(ExportJob.created_at.asc())
                .limit(1)
            )
        if job_id is None:
            return False
        await self._execute(job_id)
        return True

    def request_cancel(self, job_id: str) -> bool:
        """Ask a running export to stop. Returns whether one was actually
        running -- a queued job is cancelled by the router directly instead,
        since there is no process yet to terminate."""
        if self._current is None or self._current.job_id != job_id:
            return False
        self._cancelled.add(job_id)
        if self._current.process is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current.process.terminate()
        return True

    def is_running(self, job_id: str) -> bool:
        return self._current is not None and self._current.job_id == job_id

    # --- Execution -----------------------------------------------------------

    async def _execute(self, job_id: str) -> None:
        settings = self._settings
        async with session_scope() as session:
            job = await session.get(ExportJob, job_id)
            if job is None or job.status is not ExportStatus.QUEUED:
                return
            job.status = ExportStatus.RUNNING
            job.started_at = datetime.now(UTC)
            camera_id, start, end = job.camera_id, job.start_at, job.end_at
            root = await get_storage_root(session, settings)

        self._current = _RunningJob(job_id=job_id, process=None)
        destination = output_path(root, job_id)
        list_file = _list_path(root, job_id)
        try:
            await self._run_ffmpeg(job_id, camera_id, start, end, root, destination, list_file)
        finally:
            self._cancelled.discard(job_id)
            self._current = None
            with contextlib.suppress(FileNotFoundError, OSError):
                list_file.unlink()

    async def _run_ffmpeg(
        self,
        job_id: str,
        camera_id: str,
        start: datetime,
        end: datetime,
        root: Path,
        destination: Path,
        list_file: Path,
    ) -> None:
        async with session_scope() as session:
            plan = await timeline.build_export_plan(session, camera_id, start, end)
            segment_paths = await _segment_paths(session, [part.segment_id for part in plan.parts])

        if not plan.parts:
            await self._finish(
                job_id,
                ExportStatus.FAILED,
                error="The recorded footage in that range is no longer available.",
                destination=destination,
                keep_file=False,
            )
            return

        await asyncio.to_thread(_write_concat_list, list_file, plan.parts, segment_paths, root)

        settings = self._settings
        command = build_export_command(settings.ffmpeg_binary, list_file, destination)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            await self._finish(
                job_id,
                ExportStatus.FAILED,
                error="FFmpeg was not found on this system.",
                destination=destination,
                keep_file=False,
            )
            return
        except OSError as exc:
            await self._finish(
                job_id,
                ExportStatus.FAILED,
                error=f"Could not start FFmpeg: {exc}",
                destination=destination,
                keep_file=False,
            )
            return

        if self._current is not None:
            self._current.process = process
        assert process.stdout is not None  # noqa: S101 - requested PIPE above
        assert process.stderr is not None  # noqa: S101 - requested PIPE above

        stderr_task = asyncio.create_task(process.stderr.read())
        progress_task = asyncio.create_task(
            _pump_progress(job_id, process.stdout, plan.total_seconds)
        )
        returncode = await process.wait()
        stderr_bytes = await stderr_task
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task

        if job_id in self._cancelled:
            await self._finish(
                job_id,
                ExportStatus.CANCELLED,
                error="Cancelled.",
                destination=destination,
                keep_file=False,
            )
            return

        # Both the existence check and the size are one blocking stat each; on a
        # network mount that is not free, and this runs on the event loop.
        size = await asyncio.to_thread(
            lambda: destination.stat().st_size if destination.exists() else None
        )
        if returncode == 0 and size is not None:
            await self._finish(
                job_id,
                ExportStatus.COMPLETE,
                size_bytes=size,
                file_name=destination.name,
                expires_at=datetime.now(UTC) + timedelta(hours=EXPORT_RETENTION_HOURS),
            )
            logger.info(
                "Export completed",
                extra={
                    "job_id": job_id,
                    "size_bytes": size,
                    "seconds": round(plan.total_seconds, 1),
                },
            )
            return

        detail_lines = stderr_bytes.decode("utf-8", errors="replace").strip().splitlines()
        message = detail_lines[-1] if detail_lines else f"FFmpeg exited with status {returncode}."
        logger.warning("Export failed", extra={"job_id": job_id, "detail": message})
        await self._finish(
            job_id, ExportStatus.FAILED, error=message, destination=destination, keep_file=False
        )

    async def _finish(
        self,
        job_id: str,
        status: ExportStatus,
        *,
        error: str | None = None,
        size_bytes: int | None = None,
        file_name: str | None = None,
        expires_at: datetime | None = None,
        destination: Path | None = None,
        keep_file: bool = True,
    ) -> None:
        async with session_scope() as session:
            job = await session.get(ExportJob, job_id)
            if job is None:
                # Deleted out from under us (the router's DELETE handler does
                # this for a running job). Nobody is left to read a status, but
                # whatever FFmpeg produced is now an orphan and still needs
                # removing.
                if destination is not None:
                    with contextlib.suppress(FileNotFoundError, OSError):
                        await asyncio.to_thread(destination.unlink)
                return

            job.status = status
            job.error = error
            job.finished_at = datetime.now(UTC)
            if status is ExportStatus.COMPLETE:
                job.progress = 1.0
                job.size_bytes = size_bytes
                job.file_path = file_name
                # Protection may have been toggled while FFmpeg was running.
                # The final write must honour the latest database value.
                job.expires_at = None if job.protected else expires_at

        if not keep_file and destination is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                await asyncio.to_thread(destination.unlink)

    # --- Deletion --------------------------------------------------------

    async def delete_job(self, session: AsyncSession, job_id: str) -> bool:
        """Cancel (if running) and remove a job, its row and its file.

        Deliberately non-blocking even for a running job: signalling
        cancellation and terminating the process is enough to guarantee
        FFmpeg stops writing soon, and ``_finish`` cleans up whatever it had
        written so far once the row it would have updated turns out to be
        gone. Waiting here for that to happen would make the delete button
        hang on a slow disk for no benefit to the caller.
        """
        job = await session.get(ExportJob, job_id)
        if job is None:
            return False

        if job.status is ExportStatus.RUNNING:
            self.request_cancel(job_id)
        elif job.file_path:
            # Nothing can still be writing to this file in any other state;
            # safe to remove immediately rather than leaving it for the
            # expiry sweep.
            root = await get_storage_root(session, self._settings)
            with contextlib.suppress(FileNotFoundError, OSError, UnsafePathError):
                resolve_output_file(root, job).unlink()

        await session.delete(job)
        return True


_manager: ExportManager | None = None


def get_manager() -> ExportManager:
    global _manager
    if _manager is None:
        _manager = ExportManager()
    return _manager


def set_manager(manager: ExportManager | None) -> None:
    """Test helper."""
    global _manager
    _manager = manager
