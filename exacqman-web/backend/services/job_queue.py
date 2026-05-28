"""
Job Queue Service

Owns the serial job runtime for the ExacqMan web backend.

A single worker task drains a FIFO waiting queue. Only one job is running at
any moment (`Job.status == "processing"`). New submissions queue behind the
in-flight job and any waiting predecessors. The waiting backlog is bounded
at ``MAX_WAITING``; submissions past the cap raise ``BacklogFullError`` so
the route layer can surface a 429.

Terminal jobs (completed / failed) are retained in-memory for
``TERMINAL_TTL_SECONDS`` so polling clients have a window to observe the
state transition. ``snapshot(since=...)`` additionally filters terminal
jobs to those whose ``completed_at > since`` -- a fresh page load passes
``since=<now>`` and therefore never sees pre-existing terminal entries,
while active polling passes its last successful poll time and catches
exactly the transitions that landed in between.

Concurrency model: all registry mutations go through a single
``asyncio.Lock``. The worker mutates the running job's progress / message
in place *without* holding the lock (so polls can read live values without
blocking the worker), which is safe because every individual attribute
assignment is atomic under the GIL and snapshot copies fields out under
the lock.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, List, Optional

from api.models import ExtractRequest, Job, JobStatusEnum, JobsSnapshot
from services.exacqman_service import ExtractFailure

logger = logging.getLogger(__name__)


MAX_WAITING = 3
"""Maximum number of jobs that may sit in the waiting queue. The currently
running job does not count toward this cap."""

TERMINAL_TTL_SECONDS = 60
"""How long terminal jobs are retained in the registry. Generously longer
than the client poll interval so no transition is missed in normal use."""

JOB_LOG_DIR = (
    Path(__file__).resolve().parent.parent.parent / "logs"
)
"""Directory where per-job log snippets are written on failure. Lives next
to the backend so it survives across server restarts and is easy to inspect
out-of-band. The directory is shared with any future general server logs;
job-specific files use the ``{job_id}.log`` convention (UUID stems) so they
remain unambiguous next to anything else that lands here. Currently kept
indefinitely -- cleanup is a separate maintenance concern."""

EXPORTS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "exports"
)
"""Directory where the CLI delivers finished extracts (and where the UI's
file browser reads from). Mirrors the path the CLI is told to write into
via ``--output-dir`` in ``services.exacqman_service``; we recompute it
here so the prune step doesn't have to thread a reference through the
JobQueue constructor."""

MAX_EXPORT_FILES = 25
"""Hard cap on the number of finished-extract .mp4 files retained in
``EXPORTS_DIR``. Enforced after each successful job: if the new arrival
pushes the count over this, the oldest files (by mtime) are removed
until the count is back at the cap. The UI surfaces this as a
"most recent 25" qualifier on the Extracted Footage panel header."""

# The user-facing failure message lives on `ExtractFailure` in
# ``services.exacqman_service``:
#   * For CLI-originated failures with a recognised structured type
#     (ConfigError, ExacqvisionError, ...) -- ``exc.friendly_message``
#     drops the failure into the right bucket
#     (configuration / server / processing).
#   * For everything else (web-side spawn errors, contract violations,
#     unrecognised CLI types) -- ``ExtractFailure.DEFAULT_FRIENDLY_MESSAGE``
#     is the catch-all "internal error" bucket.
# The verbose / technical detail still lives in the per-job log file
# accessible via /api/jobs/{id}/log.

_CAPTURED_LOGGER_NAMES = ("services", "api")
"""Logger namespaces whose records are folded into the per-job log capture.
These cover everything the extract pipeline emits (CLI driver, job queue,
route layer) without dragging in unrelated uvicorn / asyncio noise."""


class _JobLogHandler(logging.Handler):
    """Buffers log records for the lifetime of a single job run.

    Attached to the loggers listed in ``_CAPTURED_LOGGER_NAMES`` for the
    duration of ``_run_job`` and detached in a ``finally`` block so a
    crashing job can never leak the handler. Records are formatted on
    ``emit`` (not on flush) so the timestamps reflect when the event
    happened, not when we wrote it to disk.
    """

    _FORMATTER = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(self._FORMATTER)
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


def _attach_log_capture() -> _JobLogHandler:
    """Install a fresh capture handler on the project loggers and return it."""
    handler = _JobLogHandler()
    for name in _CAPTURED_LOGGER_NAMES:
        logging.getLogger(name).addHandler(handler)
    return handler


def _detach_log_capture(handler: _JobLogHandler) -> None:
    """Remove ``handler`` from every logger we attached it to."""
    for name in _CAPTURED_LOGGER_NAMES:
        logging.getLogger(name).removeHandler(handler)


def _write_job_log(job_id: str, handler: _JobLogHandler, exc: BaseException) -> bool:
    """Flush the captured records plus a final exception block to disk.

    Returns True when the file was written successfully so callers can
    set ``Job.log_available`` accordingly. Any IO failure is logged and
    swallowed -- a job that already failed shouldn't fail twice on top.
    """
    try:
        JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = JOB_LOG_DIR / f"{job_id}.log"
        # Footer carries the raw exception so the log is self-contained
        # even if the exception was raised after the last logged record.
        footer = (
            "\n--- exception ---\n"
            f"{type(exc).__name__}: {exc}\n"
        )
        path.write_text("\n".join(handler.records) + footer, encoding="utf-8")
        return True
    except OSError as write_err:
        logger.error("Failed to write job log %s: %s", job_id, write_err)
        return False


def job_log_path(job_id: str) -> Path:
    """Resolve where the on-disk log for ``job_id`` lives. Caller checks existence."""
    return JOB_LOG_DIR / f"{job_id}.log"


def _prune_exports_to_limit(limit: int = MAX_EXPORT_FILES) -> List[str]:
    """Trim ``EXPORTS_DIR`` so it holds at most ``limit`` ``.mp4`` files.

    Sorts the directory's ``.mp4`` files by mtime ascending (oldest
    first) and deletes whatever's needed to bring the count down to
    ``limit``. Returns the basenames of removed files for the caller to
    log. Best-effort -- any unlink failure is logged and the loop
    continues, and a missing directory simply yields an empty result.

    Designed to be called from the job worker's success path, after the
    new artifact has landed but before the lock is taken to publish the
    completed state. Doing it outside the lock keeps file I/O off the
    snapshot-poll hot path; doing it post-success ensures we never
    delete a slot in service of a job that ultimately fails (the file
    that would have replaced it wouldn't exist).
    """
    try:
        if not EXPORTS_DIR.is_dir():
            return []
        files = sorted(
            (
                p for p in EXPORTS_DIR.iterdir()
                if p.is_file() and p.suffix.lower() == ".mp4"
            ),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError as exc:
        logger.warning("Failed to enumerate %s for pruning: %s", EXPORTS_DIR, exc)
        return []

    excess = len(files) - limit
    if excess <= 0:
        return []

    removed: List[str] = []
    for path in files[:excess]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            logger.warning("Failed to prune old export %s: %s", path, exc)
    return removed


class BacklogFullError(Exception):
    """Raised by ``JobQueue.submit`` when the waiting queue is at capacity."""


class JobQueue:
    """Serial FIFO job queue with bounded backlog and terminal TTL.

    Construct once at app startup, call :meth:`start` from the FastAPI
    lifespan, and :meth:`stop` on shutdown.
    """

    def __init__(
        self,
        run_extract: Callable[[ExtractRequest, Callable[..., None]], Awaitable[dict]],
        plan_filename: Optional[Callable[[ExtractRequest], str]] = None,
    ) -> None:
        """
        Args:
            run_extract: Async callable mirroring
                ``ExacqManService.extract_video_with_progress``. The queue
                injects its own progress callback so updates land on the
                shared ``Job`` instance.
            plan_filename: Optional sync callable mirroring
                ``ExacqManService.planned_output_filename``. When provided,
                ``Job.filename`` is populated the moment the worker picks
                up a job so the UI can show the planned output filename
                in the job header during processing.
        """
        self._run_extract = run_extract
        self._plan_filename = plan_filename
        self._waiting: Deque[Job] = deque()
        self._running: Optional[Job] = None
        self._terminal: List[Job] = []
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Spin up the worker task. Idempotent."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(), name="job-queue-worker"
            )
            logger.info("JobQueue worker started")

    async def stop(self) -> None:
        """Cancel the worker task. The in-flight subprocess is left to the
        OS to reap on uvicorn shutdown -- matching the prior behavior."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            logger.info("JobQueue worker stopped")

    async def submit(self, request: ExtractRequest) -> Job:
        """Enqueue a new job at the back of the waiting queue.

        Raises:
            BacklogFullError: when ``len(waiting) >= MAX_WAITING``.
        """
        async with self._lock:
            if len(self._waiting) >= MAX_WAITING:
                raise BacklogFullError(
                    f"Queue is full ({MAX_WAITING} jobs waiting). Try again when one finishes."
                )
            job = Job(
                id=str(uuid.uuid4()),
                status=JobStatusEnum.QUEUED,
                progress=0,
                message="Job queued for processing",
                created_at=datetime.now().isoformat(),
                request=request.dict(),
            )
            self._waiting.append(job)
            self._wakeup.set()
            logger.info(
                f"Job {job.id} queued (position {len(self._waiting)} of {MAX_WAITING})"
            )
            return job

    async def snapshot(self, since: Optional[datetime] = None) -> JobsSnapshot:
        """Capture the current queue state plus terminal jobs newer than ``since``.

        Waiting jobs are decorated with their 1-indexed ``queue_position``
        for client display. The returned ``Job`` instances are independent
        copies; mutating them does not affect the registry.
        """
        async with self._lock:
            self._prune_terminal()

            waiting = [
                job.copy(update={"queue_position": idx})
                for idx, job in enumerate(self._waiting, start=1)
            ]
            running = self._running.copy() if self._running else None

            if since is None:
                terminal = [j.copy() for j in self._terminal]
            else:
                terminal = [
                    j.copy()
                    for j in self._terminal
                    if j.completed_at
                    and _parse_iso(j.completed_at) > since
                ]

            return JobsSnapshot(
                running=running,
                waiting=waiting,
                terminal=terminal,
                server_time=datetime.now().isoformat(),
            )

    def _prune_terminal(self) -> None:
        """Drop terminal jobs older than the TTL. Called lazily on snapshot."""
        cutoff = datetime.now() - timedelta(seconds=TERMINAL_TTL_SECONDS)
        self._terminal = [
            j for j in self._terminal
            if j.completed_at and _parse_iso(j.completed_at) >= cutoff
        ]

    async def _worker_loop(self) -> None:
        """Forever: await a wakeup, pop the next waiting job, run it, repeat."""
        logger.info("JobQueue worker loop entered")
        try:
            while True:
                await self._wakeup.wait()

                async with self._lock:
                    if not self._waiting:
                        # Spurious wakeup (e.g. event left set after the
                        # only waiting job got popped). Clear and re-arm.
                        self._wakeup.clear()
                        continue
                    # Clear before popping. Any submit() that lands while
                    # we're running will set the event again, so we'll
                    # process the next job after this one completes.
                    self._wakeup.clear()
                    job = self._waiting.popleft()
                    job.status = JobStatusEnum.PROCESSING
                    job.started_at = datetime.now().isoformat()
                    job.progress = 0
                    job.message = "Starting video extraction..."
                    self._running = job

                await self._run_job(job)

                async with self._lock:
                    # Re-arm only if more work landed during the run; this
                    # is belt-and-braces -- submit() already set the event
                    # if it ran during processing.
                    if self._waiting:
                        self._wakeup.set()
        except asyncio.CancelledError:
            logger.info("JobQueue worker canceled")
            raise

    async def _run_job(self, job: Job) -> None:
        """Drive a single extraction. Mutates ``job`` in place, then moves
        it to the terminal list under the lock.

        For the duration of the run we attach a log capture handler so
        that, on failure, we can write a self-contained log snippet to
        disk for the user to download. The handler is detached in a
        ``finally`` block so it's safe against any exception path.
        """
        log_handler = _attach_log_capture()
        try:
            request = ExtractRequest(**job.request)

            # Surface the planned filename to the UI immediately. The
            # frontend swaps "Just now" for this in the job header during
            # processing so the user can see what's being produced even
            # before the result lands.
            if self._plan_filename is not None:
                try:
                    job.filename = self._plan_filename(request)
                except Exception:  # pragma: no cover - filename is non-critical
                    logger.exception("plan_filename failed; continuing without filename")

            def progress_callback(
                progress: int,
                message: str,
                rate_label: Optional[str] = None,
            ) -> None:
                # In-place mutation is intentional: the running Job object
                # is the same one ``snapshot`` reads under the lock, so
                # the next poll picks up these updates immediately. Every
                # call is authoritative -- rate_label=None genuinely
                # clears the previous label rather than leaving it stale.
                job.progress = progress
                job.message = message
                job.rate_label = rate_label

            result: Any = await self._run_extract(request, progress_callback)

            # New deliverable has landed in EXPORTS_DIR; enforce the
            # retention cap before we publish the completed status. We
            # do this outside the lock so file I/O can't block a poll,
            # and only on the success path so a failed job never
            # "consumes" a slot. Pruning is best-effort and silent on
            # failure -- a logged warning is enough.
            removed = _prune_exports_to_limit()
            if removed:
                logger.info(
                    "Pruned %d oldest export(s) to keep <= %d in %s: %s",
                    len(removed), MAX_EXPORT_FILES, EXPORTS_DIR, removed,
                )

            async with self._lock:
                job.status = JobStatusEnum.COMPLETED
                job.progress = 100
                job.message = "Footage extraction completed successfully"
                job.completed_at = datetime.now().isoformat()
                job.rate_label = None
                job.result = result if isinstance(result, dict) else {"value": result}
                self._terminal.append(job)
                self._running = None
            logger.info(f"Job {job.id} completed")
        except ExtractFailure as exc:
            # CLI-originated failure with a structured type. The
            # category-specific friendly message (configuration /
            # server / processing / internal) is precomputed on the
            # exception via ``ExtractFailure._CATEGORIES``; surface it
            # directly. The verbose technical detail still lives in the
            # downloadable per-job log snippet.
            error_msg = str(exc)
            log_written = _write_job_log(job.id, log_handler, exc)
            async with self._lock:
                job.status = JobStatusEnum.FAILED
                job.message = exc.friendly_message
                job.completed_at = datetime.now().isoformat()
                job.rate_label = None
                job.error = error_msg
                job.log_available = log_written
                self._terminal.append(job)
                self._running = None
            logger.error(
                "Job %s failed (%s): %s",
                job.id, exc.error_type, error_msg,
            )
        except Exception as exc:
            # Everything else: web-side spawn errors, the cli-2
            # "no done.output" contract violation, an OSError between
            # process events, ... -- all bucket as "internal error"
            # since they don't have a CLI-side structured type to
            # categorize.
            error_msg = str(exc)
            log_written = _write_job_log(job.id, log_handler, exc)
            async with self._lock:
                job.status = JobStatusEnum.FAILED
                job.message = ExtractFailure.DEFAULT_FRIENDLY_MESSAGE
                job.completed_at = datetime.now().isoformat()
                job.rate_label = None
                job.error = error_msg
                job.log_available = log_written
                self._terminal.append(job)
                self._running = None
            logger.error(f"Job {job.id} failed (uncategorized): {error_msg}")
        finally:
            _detach_log_capture(log_handler)


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp, tolerant of a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
