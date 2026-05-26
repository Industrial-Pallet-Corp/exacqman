"""
ExacqMan Service

Handles interaction with the ExacqMan CLI tool for video processing operations.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Callable, NamedTuple, Optional, Tuple

from api.models import ExtractRequest

logger = logging.getLogger(__name__)


# Maps CLI stage names to (low, high) web progress percentages. Within a stage,
# CLI `progress` events are scaled linearly into [low, high). On `stage` entry,
# web progress jumps to `low`. On `done`, web progress is 100.
_STAGE_RANGES = {
    "request":         (0,  1),
    "export_wait":     (1,  10),
    "export_download": (10, 25),
    "timelapsing":     (25, 75),
    "compression":     (75, 99),
}

# Human-readable messages per stage shown in the web UI.
_STAGE_MESSAGES = {
    "request":         "Requesting export from server",
    "export_wait":     "Server preparing export",
    "export_download": "Downloading footage",
    "timelapsing":     "Timelapsing footage",
    "compression":     "Compressing video",
}

# EMA smoothing factor for the live rate label. tqdm uses 0.3 as its default
# for the same purpose; the higher the value, the more responsive but jumpier.
_RATE_SMOOTHING = 0.3


class _CliRunResult(NamedTuple):
    """Structured outcome of consuming the CLI's JSON event stream.

    ``output_path`` is the value of the CLI's ``done.output`` event (the
    on-disk path of the file the CLI produced), or ``None`` if the CLI
    never emitted a ``done`` event (typically because it exited with an
    error). Treated as the authoritative location of the produced
    file -- the web service no longer scans the workspace to discover it.

    ``error`` is the last ``error`` event the CLI emitted, or ``None``
    if no error was reported. Used by the caller to build a useful
    failure message when the subprocess exits non-zero.
    """
    output_path: Optional[str]
    error: Optional[Dict[str, Any]]


class ExacqManService:
    """Service for interacting with ExacqMan CLI tool."""
    
    def __init__(self):
        """Initialize the ExacqMan service."""
        # exacqman.py is always at the same level as exacqman-web directory
        # From backend/services/exacqman_service.py, go up 3 levels to reach ExacqMan root
        self.exacqman_path = str(Path(__file__).parent.parent.parent.parent / "exacqman.py")
        self.working_directory = Path(__file__).parent.parent.parent.parent  # ExacqMan root directory
    
    async def extract_video_with_progress(self, request: ExtractRequest, progress_callback: Callable[..., None]) -> Dict[str, Any]:
        """
        Extract video with real-time progress tracking.
        
        Args:
            request: ExtractRequest containing all necessary parameters
            progress_callback: Function to call with progress updates (progress_percent, message)
            
        Returns:
            Dict containing result information
        """
        cmd_args = []
        try:
            # Convert datetime objects to the format expected by ExacqMan CLI
            start_date = request.start_datetime.strftime("%m/%d")
            start_time = request.start_datetime.strftime("%I:%M%p").lstrip('0')
            end_time = request.end_datetime.strftime("%I:%M%p").lstrip('0')

            # Generate output filename
            output_filename = self._generate_output_filename(request)

            # Build command arguments.
            # -u runs Python unbuffered so events stream in real time.
            # --progress-format=json makes the CLI emit one JSON event per line.
            cmd_args = [
                "python3", "-u", self.exacqman_path,
                "--progress-format=json",
                "extract",
                request.camera_alias,
                start_date,
                start_time,
                end_time,
                request.config_file,
                "--multiplier", str(request.timelapse_multiplier),
                "-c",  # Enable cropping to apply crop_dimensions and font_weight settings
                "-o", output_filename,
            ]

            if request.server:
                cmd_args.extend(["--server", request.server])

            if request.caption:
                cmd_args.extend(["--caption", request.caption])

            logger.info(f"Running extract command: {' '.join(cmd_args)}")
            logger.info(f"Working directory: {self.working_directory}")
            logger.info(f"Config file: {request.config_file}")

            progress_callback(0, "Starting video extraction...")

            # `start_new_session=True` makes the child call setsid() before
            # execvp, so it becomes the leader of a fresh process group.
            # Every descendant the CLI fork-execs (ffmpeg, ffprobe, the
            # exacqvision client, ...) inherits that pgid by default. When
            # we cancel a job we can then kill the entire group with one
            # killpg() call -- this is how we guarantee no orphaned ffmpeg
            # processes survive a server shutdown or job cancellation.
            # See `_terminate_subprocess_if_alive` for the teardown half.
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=self.working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # Combine stderr with stdout
                start_new_session=True,
            )

            # The finally block guarantees the child is reaped even when
            # this coroutine is cancelled mid-extract (e.g. during server
            # shutdown). Without it, SIGTERM to the web server would leave
            # an orphan ffmpeg / download running in the background.
            try:
                cli_result = await self._consume_cli_events(
                    process, progress_callback
                )

                await process.wait()

                if process.returncode != 0:
                    error_msg = (
                        cli_result.error["message"] if cli_result.error
                        else f"Extract command failed with return code {process.returncode}"
                    )
                    logger.error(error_msg)
                    progress_callback(0, f"Error: {error_msg}")
                    raise subprocess.CalledProcessError(
                        process.returncode, cmd_args, error_msg
                    )

                # The CLI exited successfully; we now trust its
                # ``done.output`` event as the authoritative location of
                # the produced file. If we somehow got here without a
                # ``done`` event (CLI regression), fail loudly rather
                # than silently glob-scanning the workspace as we used
                # to -- silent rescue would mask the contract violation.
                if not cli_result.output_path:
                    raise RuntimeError(
                        "CLI exited 0 but did not emit a `done.output` event. "
                        "The web service cannot determine which file was produced. "
                        "This is a CLI integration regression."
                    )

                # The CLI's path may be absolute or relative to its cwd
                # (our `working_directory`). `Path.resolve()` against the
                # cwd handles both cases.
                source_path = (self.working_directory / cli_result.output_path).resolve()
                dest_filename = f"{output_filename}.mp4"
                final_path = await self._move_to_exports(source_path, dest_filename)
                await self._cleanup_intermediate_files(
                    output_filename, request.timelapse_multiplier
                )

                return {
                    "operation": "extract",
                    "output_file": final_path,
                    "filename": Path(final_path).name,
                    "success": True,
                }
            finally:
                await self._terminate_subprocess_if_alive(process)

        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            logger.error(f"Error in extract_video_with_progress: {error_type}: {error_message}")
            if cmd_args:
                logger.error(f"Command that failed: {' '.join(cmd_args)}")
            logger.error(f"Working directory: {self.working_directory}")
            progress_callback(0, f"Error: {str(e)}")
            raise

    @staticmethod
    async def _terminate_subprocess_if_alive(
        process: asyncio.subprocess.Process,
    ) -> None:
        """Make sure a CLI subprocess (and all of its descendants) are dead
        before we release control.

        Normal happy-path completion has ``returncode`` already set and this
        is a no-op. The interesting case is when our coroutine is being
        cancelled (server shutting down, request aborted) -- the CLI is
        still running, *and* the CLI itself has typically forked an ffmpeg
        and/or an exacqvision download in progress. Just signalling the
        CLI alone leaks those grandchildren as orphans reparented to init.

        On POSIX we use the standard "kill the whole process group" trick:
        the CLI was spawned with ``start_new_session=True`` so it sits
        atop its own pgid; ``os.killpg(pgid, sig)`` then takes the whole
        family down at once. SIGTERM is delivered first with a 3s grace
        window, escalating to SIGKILL if anything in the group hasn't
        exited (a slow ffmpeg trying to flush, a wedged network read).

        On Windows process groups work differently and ``os.killpg`` /
        ``os.getpgid`` don't exist, so we fall back to signalling the CLI
        process directly via ``process.terminate()`` / ``process.kill()``.
        Web service operations on Windows won't get the orphan-cleanup
        benefit, but the module still imports and the immediate process
        still dies cleanly.

        Cleanup never raises: any failure to signal a target that's
        already exited (``ProcessLookupError``) is treated as success.
        """
        if process.returncode is not None:
            return

        # Prefer the POSIX group-kill path; fall back to single-process on
        # platforms without ``os.killpg`` (i.e. Windows).
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            await ExacqManService._terminate_process_group(process)
        else:
            await ExacqManService._terminate_single_process(process)

    @staticmethod
    async def _terminate_process_group(
        process: asyncio.subprocess.Process,
    ) -> None:
        """POSIX teardown: SIGTERM the pgid, wait 3s, escalate to SIGKILL.

        Resolves the pgid from the child's pid. If the child has already
        exited between our ``returncode`` check and this call,
        ``getpgid`` raises ``ProcessLookupError`` and we exit silently.
        """
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            return  # child raced to exit; nothing to signal.

        def _killpg(sig: int) -> bool:
            """Best-effort group signal; returns False if the group is gone."""
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return False
            except PermissionError:
                # Shouldn't happen for our own child, but never raise from cleanup.
                logger.warning(
                    "Lacked permission to signal pgid %s; leaving subprocess alone",
                    pgid,
                )
                return False
            return True

        if not _killpg(signal.SIGTERM):
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
            logger.info(
                "CLI subprocess %s (pgid %s) and descendants terminated cleanly",
                process.pid, pgid,
            )
            return
        except asyncio.TimeoutError:
            logger.warning(
                "CLI subprocess group %s did not exit within 3s of SIGTERM; sending SIGKILL",
                pgid,
            )

        _killpg(signal.SIGKILL)
        try:
            await process.wait()
        except Exception:  # pragma: no cover - defensive; wait() shouldn't raise here
            pass

    @staticmethod
    async def _terminate_single_process(
        process: asyncio.subprocess.Process,
    ) -> None:
        """Windows / no-killpg fallback: SIGTERM-equivalent, then kill.

        Doesn't reach grandchildren, but at least the immediate CLI
        process dies and the module stays portable.
        """
        try:
            process.terminate()
        except ProcessLookupError:
            return  # Child raced to exit on its own; nothing to do.

        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
            logger.info(
                "CLI subprocess %s terminated cleanly on shutdown",
                process.pid,
            )
            return
        except asyncio.TimeoutError:
            logger.warning(
                "CLI subprocess %s did not exit within 3s of SIGTERM; sending SIGKILL",
                process.pid,
            )

        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await process.wait()
        except Exception:  # pragma: no cover - defensive; wait() shouldn't raise here
            pass

    async def _consume_cli_events(
        self,
        process: asyncio.subprocess.Process,
        progress_callback: Callable[..., None],
    ) -> _CliRunResult:
        """Read JSON events from the CLI subprocess and drive progress_callback.

        Returns a `_CliRunResult` carrying:
          * `output_path` -- the value of the CLI's ``done.output`` event,
            i.e. the on-disk path of the file the CLI produced. ``None`` if
            no ``done`` event ever arrived (the CLI either errored or
            exited abnormally). This is the authoritative location of the
            produced file; the web service no longer scans the workspace
            to find it.
          * `error` -- the last ``error`` event payload (if any), so the
            caller can include its message in the failure surface when
            the subprocess exits non-zero.

        Non-JSON lines (Python tracebacks, stray prints, ffmpeg output
        that leaks past the JSON reporter) are logged and otherwise
        ignored.

        The callback is invoked as
        ``progress_callback(pct, message, rate_label=...)`` where
        ``rate_label`` is an already-formatted string ("12.4 MB/s",
        "140 FPS") for stages that have a meaningful rate, or ``None`` to
        clear any stale rate from the previous stage. Each call is the
        authoritative snapshot -- callers should write whatever they get
        directly to their state object.
        """
        # `process.stdout` is typed Optional[StreamReader] because asyncio only
        # attaches a reader when stdout=PIPE. We always pass PIPE, so this is a
        # programmer error if ever None; assert eagerly to keep type checkers
        # happy and catch a misconfigured subprocess immediately.
        stdout = process.stdout
        assert stdout is not None, "subprocess must be started with stdout=PIPE"

        buffer = b""
        current_stage: Optional[str] = None
        last_error: Optional[Dict[str, Any]] = None
        # The CLI emits exactly one ``done`` event on success; capture its
        # ``output`` payload here so the caller can move the file by path
        # rather than by glob.
        done_output: Optional[str] = None

        # Per-stage rate state: stage -> (prev_current, prev_ts, ema_rate).
        # Reset implicitly when the stage advances (we only ever look up the
        # active stage). Local to this call so each job starts clean.
        rate_state: Dict[str, Tuple[int, float, Optional[float]]] = {}

        while True:
            chunk = await stdout.read(8192)
            if not chunk:
                if buffer:
                    trailing = buffer.decode("utf-8", errors="replace").strip()
                    if trailing:
                        logger.info("CLI Output (trailing, no newline): %s", trailing)
                break
            buffer += chunk
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                event = self._parse_event_line(line)
                if event is None:
                    continue
                kind = event.get("event")
                if kind == "stage":
                    current_stage = event.get("stage") or current_stage
                    if current_stage and current_stage not in _STAGE_RANGES:
                        logger.warning(
                            "Unknown CLI stage '%s' (no _STAGE_RANGES entry); "
                            "progress for this stage will not advance the web UI.",
                            current_stage,
                        )
                    low, _ = _STAGE_RANGES.get(current_stage, (None, None))
                    message = event.get("message") or _STAGE_MESSAGES.get(
                        current_stage, current_stage or "Working"
                    )
                    if low is not None:
                        # rate_label=None explicitly clears any rate left over
                        # from the previous stage.
                        progress_callback(low, f"{message}…", rate_label=None)
                elif kind == "progress":
                    stage = event.get("stage") or current_stage
                    if stage != current_stage:
                        current_stage = stage
                    rng = _STAGE_RANGES.get(stage)
                    total = event.get("total") or 0
                    current = event.get("current") or 0
                    unit = event.get("unit") or ""
                    ts = event.get("ts") or time.time()
                    rate_label = self._compute_rate_label(
                        rate_state, stage, unit, current, ts
                    )
                    if rng and total > 0:
                        low, high = rng
                        ratio = max(0.0, min(1.0, current / total))
                        pct = int(round(low + (high - low) * ratio))
                        message = _STAGE_MESSAGES.get(stage, stage)
                        progress_callback(
                            pct,
                            f"{message}… ({int(round(ratio * 100))}%)",
                            rate_label=rate_label,
                        )
                elif kind == "done":
                    # Capture the path so the caller can move the actual
                    # file the CLI produced, rather than glob-scanning the
                    # workspace for "the most likely candidate".
                    output = event.get("output")
                    if isinstance(output, str) and output:
                        done_output = output
                    msg = "Footage extraction completed successfully"
                    if output:
                        msg = f"{msg}: {Path(output).name}"
                    progress_callback(100, msg, rate_label=None)
                elif kind == "error":
                    last_error = {
                        "type": event.get("type", "Error"),
                        "message": event.get("message", "Unknown error"),
                    }
                    logger.error(
                        "CLI error: %s: %s",
                        last_error["type"], last_error["message"],
                    )
                elif kind == "info":
                    logger.info("CLI info: %s", event.get("message", ""))
                elif kind == "warning":
                    logger.warning("CLI warning: %s", event.get("message", ""))

        return _CliRunResult(output_path=done_output, error=last_error)

    @staticmethod
    def _compute_rate_label(
        rate_state: Dict[str, Tuple[int, float, Optional[float]]],
        stage: Optional[str],
        unit: str,
        current: int,
        ts: float,
    ) -> Optional[str]:
        """Update per-stage rate state and return a formatted label, or None.

        Only the two rate-meaningful units are tracked:
          * ``bytes`` -> formatted as ``"<x.x> MB/s"`` (decimal MB, matching
            disk-throughput convention rather than tqdm's binary MiB).
          * ``frames`` -> formatted as ``"<n> FPS"``.

        Other units (percent, empty, etc.) return None. The first progress
        event in a stage always returns None because we need two samples to
        compute a delta. Subsequent samples are smoothed with an EMA so the
        displayed value stays readable instead of jittering with every
        200ms throttled update from the CLI.
        """
        if not stage or unit not in ("bytes", "frames"):
            return None
        prev = rate_state.get(stage)
        if prev is None:
            # First sample in this stage; seed and emit no rate yet.
            rate_state[stage] = (current, ts, None)
            return None
        prev_current, prev_ts, prev_ema = prev
        delta_current = current - prev_current
        delta_ts = ts - prev_ts
        if delta_ts <= 0 or delta_current < 0:
            # Same instant or non-monotonic progress (rare, defensive).
            return None
        instant = delta_current / delta_ts
        ema = instant if prev_ema is None else (
            _RATE_SMOOTHING * instant + (1.0 - _RATE_SMOOTHING) * prev_ema
        )
        rate_state[stage] = (current, ts, ema)
        if unit == "bytes":
            return f"{ema / 1_000_000:.1f} MB/s"
        # unit == "frames"
        return f"{int(round(ema))} FPS"

    def _parse_event_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse a single CLI output line as a JSON event.

        Returns the parsed dict for valid event objects; logs and returns None
        for anything else so unexpected lines (tracebacks, ffmpeg output, etc.)
        never break progress tracking.
        """
        if not (line.startswith("{") and line.endswith("}")):
            logger.info("CLI Output (non-event): %s", line)
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.info("CLI Output (non-event): %s", line)
            return None
        if not isinstance(event, dict) or "event" not in event:
            logger.info("CLI Output (non-event): %s", line)
            return None
        return event

    def _generate_output_filename(self, request: ExtractRequest) -> str:
        """
        Generate the output filename stem for the extract operation.

        When ``request.filename`` is supplied, it is sanitized and used
        verbatim. Otherwise the service generates a stem of the form
        ``{date}_{time}_{server}_{camera}_{multiplier}x`` so filenames
        remain unambiguous across servers (the same camera alias can exist
        under multiple servers).

        Returns:
            Filename stem without an extension; exacqman.py adds ``.mp4``.
        """
        if request.filename:
            return self._sanitize_filename_component(request.filename)

        date_str = request.start_datetime.strftime("%Y-%m-%d")
        time_str = self._format_time_for_filename(request.start_datetime)
        server = self._sanitize_filename_component(request.server) if request.server else "unknown"
        camera = self._sanitize_filename_component(request.camera_alias)
        return f"{date_str}_{time_str}_{server}_{camera}_{request.timelapse_multiplier}x"

    def planned_output_filename(self, request: ExtractRequest) -> str:
        """
        Public form of the output filename including extension.

        The JobQueue calls this when picking up a job so the eventual
        on-disk filename can be shown in the UI from the moment the job
        starts processing -- well before the subprocess finishes and we
        populate ``job.result.filename``.
        """
        return f"{self._generate_output_filename(request)}.mp4"

    @staticmethod
    def _sanitize_filename_component(value: str) -> str:
        """Lowercase and replace whitespace with hyphens for filesystem safety."""
        return value.lower().replace(" ", "-")
    
    def _format_time_for_filename(self, datetime_obj) -> str:
        """
        Format datetime as 24-hour HHMM for use in filename stems.

        e.g. 9:15 am becomes ``0915``; 3:42 pm becomes ``1542``. 24-hour
        avoids the am/pm suffix entirely and sorts lexically by clock time.

        Args:
            datetime_obj: datetime object

        Returns:
            Formatted time string for filename
        """
        return f"{datetime_obj.hour:02d}{datetime_obj.minute:02d}"
    
    async def _cleanup_intermediate_files(
        self,
        output_stem: str,
        multiplier: int,
    ) -> None:
        """Remove the known intermediates this extract left behind.

        The CLI's extract pipeline writes three files in the working
        directory, each derived deterministically from the
        ``--output`` argument (here ``output_stem``):

          1. ``{stem}.mp4``                       -- raw download from
                                                     the exacqvision server
          2. ``{stem}_{multiplier}x.mp4``         -- timelapsed
                                                     pre-compression version
          3. ``{stem}_{multiplier}x_libx264_*.mp4`` -- final compressed,
                                                     already moved to
                                                     ``exports/`` by
                                                     ``_move_to_exports``

        This method removes (1) and (2) by exact path; (3) was already
        moved by the time we get here so there's nothing to do for it.

        We deliberately do *not* glob the workspace for ``*.tmp`` /
        ``*.log`` / ``temp_*`` / similar as the previous implementation
        did -- those globs scanned the entire ExacqMan repo root and
        could destroy unrelated files (per-job logs, hand-edited
        scratch files, the git internals on some setups). If a future
        intermediate gets added to the pipeline, it should be named
        deterministically from ``output_stem`` and removed here by
        exact path.

        Failures to unlink are logged but never raised; cleanup must
        not fail an otherwise successful job.
        """
        intermediates = (
            self.working_directory / f"{output_stem}.mp4",
            self.working_directory / f"{output_stem}_{multiplier}x.mp4",
        )

        removed: list[str] = []
        for path in intermediates:
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(path.name)
            except OSError as e:
                logger.warning(
                    "Failed to remove intermediate file %s: %s",
                    path, e,
                )

        if removed:
            logger.info("Cleaned up intermediate files: %s", removed)
        else:
            logger.info(
                "No intermediate files found to clean up "
                "(stem=%s, multiplier=%s)",
                output_stem, multiplier,
            )
    
    async def _move_to_exports(
        self,
        source_path: Path,
        dest_filename: str,
    ) -> str:
        """Move the CLI's reported output file to ``exports/`` under a clean name.

        Args:
            source_path: Absolute path to the file the CLI produced (the
                value of ``done.output`` from the CLI's JSON event stream,
                resolved against the CLI's working directory). Must exist.
            dest_filename: The on-disk name to use under ``exports/``,
                including extension (e.g. ``"name.mp4"``). The rename
                deliberately hides CLI-internal naming details
                (compression suffix, codec tag, etc.) from end users.

        Returns:
            Absolute path to the moved file under ``exports/``.

        Raises:
            FileNotFoundError: ``source_path`` does not exist. This
                indicates either a CLI regression (``done.output``
                pointed nowhere) or a race with another job; we
                deliberately do not glob-rescue here because that's the
                very behavior this refactor eliminated.
        """
        if not source_path.is_file():
            raise FileNotFoundError(
                f"CLI reported output file does not exist: {source_path}"
            )

        exports_dir = self.working_directory / "exacqman-web" / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)

        dest_path = exports_dir / dest_filename
        # `shutil.move` handles cross-filesystem moves (rename when
        # possible, copy + unlink otherwise) and overwrites an existing
        # destination, which is what we want when the user re-runs an
        # extract with the same filename.
        shutil.move(str(source_path), str(dest_path))
        logger.info(
            "Moved %s to exports as %s",
            source_path.name,
            dest_filename,
        )
        return str(dest_path)
    
    def validate_config_file(self, config_path: str) -> bool:
        """
        Validate that a config file exists and is readable.
        
        Args:
            config_path: Path to the configuration file
            
        Returns:
            True if valid, False otherwise
        """
        try:
            if not config_path.startswith('/'):
                config_path = str(self.working_directory / config_path)
            
            return Path(config_path).exists()
        except Exception as e:
            logger.error(f"Error validating config file {config_path}: {str(e)}")
            return False
    
    def get_available_configs(self) -> list:
        """
        Get list of available configuration files.
        
        Returns:
            List of configuration file paths
        """
        try:
            config_files = []
            for file_path in self.working_directory.glob("*.config"):
                config_files.append(str(file_path))
            return config_files
        except Exception as e:
            logger.error(f"Error getting available configs: {str(e)}")
            return []