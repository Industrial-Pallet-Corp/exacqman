"""
ExacqMan Service

Handles interaction with the ExacqMan CLI tool for video processing operations.
"""

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Dict, Any, Callable, NamedTuple, Optional

from api.models import ExtractRequest
from exacqman_naming import default_output_stem, sanitize_filename_component

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


class ExtractFailure(Exception):
    """A CLI extract job that ended in an error we can categorize.

    ``extract_video_with_progress`` raises this whenever the subprocess
    exits non-zero. ``error_type`` is the structured type the CLI
    emitted via its ``error`` event (e.g. ``"ConfigError"``,
    ``"ExacqvisionError"``), or ``"InternalError"`` for the synthesized
    case where the subprocess exited non-zero without emitting any
    ``error`` event at all. ``error_message`` is the CLI's detailed
    string and is what ends up in ``job.error`` / the per-job log.

    ``friendly_message`` is the *user-facing* string -- the one we
    surface in the UI as ``job.message``. It's derived from
    ``error_type`` via ``_CATEGORIES`` so all the user-visible text
    lives in one place. Unrecognized types fall through to
    ``DEFAULT_FRIENDLY_MESSAGE`` ("internal error"), which is also the
    string ``_run_job`` uses for any non-CLI failure (web-side spawn
    errors, contract violations, etc.) -- a single, unsurprising
    "something else went wrong" bucket.

    Notes on the bucket choices:
      * `server` here means *the camera server* (the exacqvision
        device), not our backend. The previous generic
        "server error" wording conflated those; the new mapping
        reserves "the camera server" for ExacqvisionError and uses
        "internal error" for our-backend issues.
      * Caption-too-long lives in `configuration` (it's bad input,
        from the same family as a malformed config / credentials
        file) -- in practice the web UI blocks this at form level
        so the bucket exists mainly to handle direct CLI / API
        callers.
    """

    DEFAULT_FRIENDLY_MESSAGE = "Video extraction failed: internal error"

    _CATEGORIES = {
        # configuration bucket -- something the user can fix
        "ConfigError":      "Video extraction failed: configuration problem",
        "CredentialsError": "Video extraction failed: authentication problem",
        "CaptionTooLong":   "Video extraction failed: caption too long",
        # server bucket -- the camera (exacqvision) server itself.
        "ExacqvisionError": "Video extraction failed: couldn't reach the camera server",
        # processing bucket -- local video decode / transform failure.
        "VideoOpenError":   "Video extraction failed: video processing error",
    }

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.error_message = message
        self.friendly_message = self._CATEGORIES.get(
            error_type, self.DEFAULT_FRIENDLY_MESSAGE
        )
        # Make str(exc) carry the unambiguous "<type>: <message>" form so
        # backend logs and the per-job log snippet keep their existing
        # readability; the user-facing summary is `friendly_message`.
        super().__init__(f"{error_type}: {message}")


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
            output_filename = self._generate_output_filename(request)
            exports_dir = (self.working_directory / "exports").resolve()

            # Build command arguments. We use flag forms exclusively (except for
            # the leading `camera_alias` positional) so the call is unambiguous
            # and order-independent:
            #
            #   * `--config` instead of the positional `config_file` -- the
            #     positional `extract` form would normally also require `date`,
            #     `start`, and `end` positionals to land `config_file` in the
            #     right slot; the flag form lets us skip those entirely.
            #   * `--start-iso-datetime` / `--end-iso-datetime` (ISO 8601)
            #     instead of the positional `date` / `start` / `end` strings.
            #     This carries the exact instant -- including year and
            #     timezone -- with no intermediate lossy `%m/%d` + `%I:%M%p`
            #     round-trip and no year/day fixup heuristics on the CLI
            #     side. The "iso" prefix on the flag name makes the
            #     expected format obvious at the call site.
            #   * `--output-dir` so the CLI delivers the final file
            #     directly into `exports/` and cleans up its own
            #     intermediates. Eliminates the post-pipeline move +
            #     intermediate-cleanup work the web service used to do
            #     (`_move_to_exports` / `_cleanup_intermediate_files`),
            #     and -- crucially -- removes the only path by which a
            #     web-side bug could touch files outside `exports/`.
            #
            # -u runs Python unbuffered so events stream in real time.
            # --progress-format=json makes the CLI emit one JSON event per line.
            cmd_args = [
                "python3", "-u", self.exacqman_path,
                "--progress-format=json",
                "extract",
                request.camera_alias,
                "--config", request.config_file,
                "--start-iso-datetime", request.start_datetime.isoformat(),
                "--end-iso-datetime", request.end_datetime.isoformat(),
                "--multiplier", str(request.timelapse_multiplier),
                "-c", "true",  # Enable cropping to apply crop_dimensions and font_weight settings
                "-o", output_filename,
                "--output-dir", str(exports_dir),
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
            # this coroutine is canceled mid-extract (e.g. during server
            # shutdown). Without it, SIGTERM to the web server would leave
            # an orphan ffmpeg / download running in the background.
            try:
                cli_result = await self._consume_cli_events(
                    process, progress_callback
                )

                await process.wait()

                if process.returncode != 0:
                    # Two sub-cases:
                    #   1. CLI emitted a structured `error` event -- use
                    #      its type + message so the friendly mapping
                    #      lands in the right bucket (configuration /
                    #      server / processing).
                    #   2. CLI exited non-zero without any `error` event
                    #      (killed by signal, crashed before reaching
                    #      the reporter, ...). Synthesize an
                    #      `InternalError` so the mapping falls through
                    #      to the "internal error" bucket.
                    if cli_result.error:
                        error_type = cli_result.error["type"]
                        error_msg = cli_result.error["message"]
                    else:
                        error_type = "InternalError"
                        error_msg = (
                            f"Extract command failed with return code "
                            f"{process.returncode}"
                        )
                    logger.error("CLI failure %s: %s", error_type, error_msg)
                    progress_callback(0, f"Error: {error_msg}")
                    raise ExtractFailure(error_type, error_msg)

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

                # With `--output-dir` set, the CLI delivers the final
                # file directly into `exports/` (renamed to the bare
                # stem with no codec suffix, intermediates already
                # removed by `_finalize_extract_output_dir` in the
                # CLI). `done.output` is therefore already the
                # canonical artifact path; we just resolve it (it may
                # be absolute or relative to the CLI's cwd) and pass
                # it through.
                final_path = str(
                    (self.working_directory / cli_result.output_path).resolve()
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
        canceled (server shutting down, request aborted) -- the CLI is
        still running, *and* the CLI itself has typically forked an ffmpeg
        and/or an exacqvision download in progress. Just signaling the
        CLI alone leaks those grandchildren as orphans reparented to init.

        On POSIX we use the standard "kill the whole process group" trick:
        the CLI was spawned with ``start_new_session=True`` so it sits
        atop its own pgid; ``os.killpg(pgid, sig)`` then takes the whole
        family down at once. SIGTERM is delivered first with a 3s grace
        window, escalating to SIGKILL if anything in the group hasn't
        exited (a slow ffmpeg trying to flush, a wedged network read).

        On Windows process groups work differently and ``os.killpg`` /
        ``os.getpgid`` don't exist, so we fall back to signaling the CLI
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
                    # ``rate_label`` is computed at the source (by
                    # ``progress.JsonReporter._compute_rate_label``) and
                    # may be absent on the first sample of a stage or on
                    # non-rate units like ``percent``. The web service is
                    # a pure renderer here -- whatever the CLI sends is
                    # what we surface.
                    rate_label = event.get("rate_label")
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
        """Resolve the output filename stem for this extract request.

        Thin wrapper around the shared naming helpers in
        ``exacqman_naming``:

          * If the user typed a custom filename in the UI, normalize it
            via ``sanitize_filename_component`` (lowercase + hyphenate).
            Length and path-separator validation already happened on the
            API model layer (``ExtractRequest``).
          * Otherwise build the canonical default stem via
            ``default_output_stem``.

        Returns:
            Filename stem without an extension; exacqman.py / our
            ``planned_output_filename`` adds ``.mp4``.
        """
        if request.filename:
            return sanitize_filename_component(request.filename)
        return default_output_stem(
            request.start_datetime,
            request.server,
            request.camera_alias,
            request.timelapse_multiplier,
        )

    def planned_output_filename(self, request: ExtractRequest) -> str:
        """
        Public form of the output filename including extension.

        The JobQueue calls this when picking up a job so the eventual
        on-disk filename can be shown in the UI from the moment the job
        starts processing -- well before the subprocess finishes and we
        populate ``job.result.filename``.
        """
        return f"{self._generate_output_filename(request)}.mp4"
    
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