"""
ExacqMan Service

Handles interaction with the ExacqMan CLI tool for video processing operations.
"""

import asyncio
import json
import subprocess
import logging
from typing import Dict, Any, Callable, Optional
from pathlib import Path
import shutil

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

class ExacqManService:
    """Service for interacting with ExacqMan CLI tool."""
    
    def __init__(self):
        """Initialize the ExacqMan service."""
        # exacqman.py is always at the same level as exacqman-web directory
        # From backend/services/exacqman_service.py, go up 3 levels to reach ExacqMan root
        self.exacqman_path = str(Path(__file__).parent.parent.parent.parent / "exacqman.py")
        self.working_directory = Path(__file__).parent.parent.parent.parent  # ExacqMan root directory
    
    async def extract_video_with_progress(self, request: ExtractRequest, progress_callback: Callable[[int, str], None]) -> Dict[str, Any]:
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

            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=self.working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # Combine stderr with stdout
            )

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

        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            logger.error(f"Error in extract_video_with_progress: {error_type}: {error_message}")
            if cmd_args:
                logger.error(f"Command that failed: {' '.join(cmd_args)}")
            logger.error(f"Working directory: {self.working_directory}")
            progress_callback(0, f"Error: {str(e)}")
            raise

    async def _consume_cli_events(
        self,
        process: asyncio.subprocess.Process,
        progress_callback: Callable[[int, str], None],
    ) -> Optional[Dict[str, Any]]:
        """Read JSON events from the CLI subprocess and drive progress_callback.

        Returns the last `error` event payload (if any), so the caller can use
        its message when the subprocess exits non-zero. Non-JSON lines (e.g.
        Python tracebacks, stray prints) are logged and otherwise ignored.
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
                        progress_callback(low, f"{message}…")
                elif kind == "progress":
                    stage = event.get("stage") or current_stage
                    if stage != current_stage:
                        current_stage = stage
                    rng = _STAGE_RANGES.get(stage)
                    total = event.get("total") or 0
                    current = event.get("current") or 0
                    if rng and total > 0:
                        low, high = rng
                        ratio = max(0.0, min(1.0, current / total))
                        pct = int(round(low + (high - low) * ratio))
                        message = _STAGE_MESSAGES.get(stage, stage)
                        progress_callback(pct, f"{message}… ({int(round(ratio * 100))}%)")
                elif kind == "done":
                    output = event.get("output")
                    msg = "Footage extraction completed successfully"
                    if output:
                        msg = f"{msg}: {Path(output).name}"
                    progress_callback(100, msg)
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