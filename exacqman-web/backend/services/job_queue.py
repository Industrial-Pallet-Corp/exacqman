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
from typing import Any, Awaitable, Callable, Deque, List, Optional

from api.models import ExtractRequest, Job, JobStatusEnum, JobsSnapshot

logger = logging.getLogger(__name__)


MAX_WAITING = 3
"""Maximum number of jobs that may sit in the waiting queue. The currently
running job does not count toward this cap."""

TERMINAL_TTL_SECONDS = 60
"""How long terminal jobs are retained in the registry. Generously longer
than the client poll interval so no transition is missed in normal use."""


class BacklogFullError(Exception):
    """Raised by ``JobQueue.submit`` when the waiting queue is at capacity."""


class JobQueue:
    """Serial FIFO job queue with bounded backlog and terminal TTL.

    Construct once at app startup, call :meth:`start` from the FastAPI
    lifespan, and :meth:`stop` on shutdown.
    """

    def __init__(
        self,
        run_extract: Callable[[ExtractRequest, Callable[[int, str], None]], Awaitable[dict]],
    ) -> None:
        """
        Args:
            run_extract: Async callable mirroring
                ``ExacqManService.extract_video_with_progress``. The queue
                injects its own progress callback so updates land on the
                shared ``Job`` instance.
        """
        self._run_extract = run_extract
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
            logger.info("JobQueue worker cancelled")
            raise

    async def _run_job(self, job: Job) -> None:
        """Drive a single extraction. Mutates ``job`` in place, then moves
        it to the terminal list under the lock."""
        try:
            request = ExtractRequest(**job.request)

            def progress_callback(progress: int, message: str) -> None:
                # In-place mutation is intentional: the running Job object
                # is the same one ``snapshot`` reads under the lock, so
                # the next poll picks up these updates immediately.
                job.progress = progress
                job.message = message

            result: Any = await self._run_extract(request, progress_callback)

            async with self._lock:
                job.status = JobStatusEnum.COMPLETED
                job.progress = 100
                job.message = "Footage extraction completed successfully"
                job.completed_at = datetime.now().isoformat()
                job.result = result if isinstance(result, dict) else {"value": result}
                self._terminal.append(job)
                self._running = None
            logger.info(f"Job {job.id} completed")
        except Exception as exc:
            error_msg = str(exc)
            async with self._lock:
                job.status = JobStatusEnum.FAILED
                job.message = f"Video extraction failed: {error_msg}"
                job.completed_at = datetime.now().isoformat()
                job.error = error_msg
                self._terminal.append(job)
                self._running = None
            logger.error(f"Job {job.id} failed: {error_msg}")


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp, tolerant of a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
