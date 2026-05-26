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
from typing import Dict, Any, Callable, Optional, Tuple

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
                cli_error: Optional[Dict[str, Any]] = await self._consume_cli_events(
                    process, progress_callback
                )

                await process.wait()

                if process.returncode != 0:
                    error_msg = (
                        cli_error["message"] if cli_error
                        else f"Extract command failed with return code {process.returncode}"
                    )
                    logger.error(error_msg)
                    progress_callback(0, f"Error: {error_msg}")
                    raise subprocess.CalledProcessError(
                        process.returncode, cmd_args, error_msg
                    )

                final_path = await self._move_to_exports(output_filename)
                await self._cleanup_intermediate_files(output_filename)

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
    ) -> Optional[Dict[str, Any]]:
        """Read JSON events from the CLI subprocess and drive progress_callback.

        Returns the last `error` event payload (if any), so the caller can use
        its message when the subprocess exits non-zero. Non-JSON lines (e.g.
        Python tracebacks, stray prints) are logged and otherwise ignored.

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
                    output = event.get("output")
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

        return last_error

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
    
    async def _cleanup_intermediate_files(self, base_filename: str = None):
        """
        Clean up intermediate files created during video processing.
        
        This removes temporary files that ExacqMan creates during processing
        but keeps only the final compressed output.
        
        Args:
            base_filename: Base filename to clean up specific intermediate files
        """
        try:
            cleaned_files = []
            
            # Clean up specific intermediate files if base_filename is provided
            if base_filename:
                base_name = base_filename.replace('.mp4', '')
                
                # Patterns for intermediate files specific to this extraction
                specific_patterns = [
                    f"{base_name}.mp4",  # Raw export
                    f"{base_name}_*.mp4",  # Timelapsed version (before compression)
                ]
                
                for pattern in specific_patterns:
                    for file_path in self.working_directory.glob(pattern):
                        if file_path.is_file():
                            # Skip the final compressed file
                            if not any(compression in file_path.name for compression in ['_libx264_', '_high', '_medium', '_low']):
                                file_path.unlink()
                                cleaned_files.append(file_path.name)
                                logger.info(f"Cleaned up intermediate file: {file_path.name}")
            
            # Clean up general temporary files
            general_patterns = [
                "*.tmp",
                "*_temp.*",
                "*_intermediate.*",
                "temp_*",
                "*.log"
            ]
            
            for pattern in general_patterns:
                for file_path in self.working_directory.glob(pattern):
                    if file_path.is_file():
                        file_path.unlink()
                        cleaned_files.append(file_path.name)
            
            if cleaned_files:
                logger.info(f"Cleaned up {len(cleaned_files)} intermediate files: {cleaned_files}")
            else:
                logger.info("No intermediate files found to clean up")
                
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")
            # Don't raise the exception as cleanup failure shouldn't fail the job
    
    async def _move_to_exports(self, filename: str) -> str:
        """
        Move the final compressed file to the exports directory.
        
        Args:
            filename: Base name of the file to move (exacqman.py may create variations)
            
        Returns:
            Path to the file in exports directory
        """
        try:
            # Create exports directory if it doesn't exist
            exports_dir = self.working_directory / "exacqman-web" / "exports"
            exports_dir.mkdir(parents=True, exist_ok=True)
            
            # Look for the final compressed file with any compression level
            base_name = filename.replace('.mp4', '')  # Remove .mp4 if present
            # Sanitize base_name to match what CLI creates (spaces become underscores)
            base_name = base_name.replace(" ", "_")
            source_path = None
            
            # Try to find the final compressed file with libx264 pattern
            for file_path in self.working_directory.glob(f"{base_name}_*_libx264_*.mp4"):
                if file_path.is_file():
                    source_path = file_path
                    break
            
            # If not found, try the specific high compression pattern
            if not source_path:
                final_filename = f"{base_name}_libx264_high.mp4"
                source_path = self.working_directory / final_filename
                if not source_path.exists():
                    source_path = None
            
            # Fallback to original filename if no compressed version found
            if not source_path:
                source_path = self.working_directory / filename
                if not source_path.exists():
                    source_path = self.working_directory / f"{filename}.mp4"
            
            if not source_path or not source_path.exists():
                raise FileNotFoundError(f"Final compressed file not found for: {filename}")
            
            # Move to exports directory with clean filename
            clean_filename = f"{base_name}.mp4"
            dest_path = exports_dir / clean_filename
            shutil.move(str(source_path), str(dest_path))
            
            logger.info(f"Moved final compressed file {source_path.name} to exports directory as {clean_filename}")
            return str(dest_path)
            
        except Exception as e:
            logger.error(f"Error moving file to exports: {str(e)}")
            raise
    
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