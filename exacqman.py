from dataclasses import dataclass
from moviepy import VideoFileClip
from datetime import timedelta, datetime
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as duparse
from zoneinfo import ZoneInfo
from exacqvision import Exacqvision, ExacqvisionError
from requests.exceptions import RequestException
from exacqman_naming import default_output_stem
from exacqman_config import (
    import_config,
    import_credentials,
    resolve_credentials_path,
    split_servers_and_cameras,
)
from progress import init_reporter, get_reporter
import argparse
import cv2
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional


# Project root (this file lives at the repo root). Scratch files are kept in a
# project-local, gitignored directory rather than the system temp dir so they
# never escape the project tree -- handy on locked-down machines and for
# predictable cleanup.
PROJECT_ROOT = Path(__file__).resolve().parent
TEMP_DIR = PROJECT_ROOT / ".tmp"

# Length of the throwaway clip the `crop` subcommand exports just to grab a
# single frame for the ROI selector. Kept tiny on purpose: the camera framing
# is static, so a couple of seconds is plenty, and exporting the full lookback
# window would download minutes (often gigabytes) of footage we never use.
CROP_PROBE_SECONDS = 2


@dataclass
class Settings:
    ''' 
    Class that centralizes the settings for the program.
    Default settings can be set in this class. 
    CLI args and the TOML config overwrite the settings in this class (CLI args > config > defaults)
    '''
    username: str = None
    password: str = None

    servers: dict = None                # {server_name: url} for all configured servers
    cameras: dict = None                # {server_name: {alias: {id, crop_dimensions?}}} nested map

    timelapse_multiplier: int = 10      # Must be a positive int
    compression_level: str = 'medium'   # Should be 'low', 'medium', or 'high'
    timezone: str = None
    crop: bool = False                  # Does the video need cropped? Crop dimensions only matter if this is True.
    default_crop_dimensions: tuple[tuple[int, int], tuple[int, int]] = None  # Fallback crop applied when the selected camera has no per-camera override
    crop_dimensions: tuple[tuple[int, int], tuple[int, int]] = None  # Effective crop for this run: per-camera override if set, else default
    font_weight: int = 2                # Font thickness
    caption: str = None                 # Optional caption rendered below the timestamp
    caption_limit = 30                  # Max number of characters for caption

    server: str = None                  # Server name (must match a top-level [<server>] table)
    server_ip: str = None               # URL of the chosen Exacqvision server
    camera_alias: str = None            # Camera alias (must match a [<server>.<alias>] entry)
    camera_id: int = None               # Camera ID resolved from the chosen alias on the chosen server
    input_filename: str = None          # Video filename that needs processed
    output_filename: str = None         # Desired name of output file (will always be .mp4)
    output_dir: str = None              # When set, deliver a single clean .mp4 into this directory and remove intermediates
    date: str = None                    # MM/DD (e.g. '3/11') -- used by the positional human form
    start_time: str = None              # Start time of video (e.g. '6 pm', '6:30pm', '18:30')
    end_time: str = None                # End time of video (e.g. '6 pm', '6:30pm', '18:30')
    # ISO 8601 datetime pair, populated only from --start-iso-datetime /
    # --end-iso-datetime. When set, these take precedence over date +
    # start_time + end_time and skip the year/day fixup heuristics in
    # `convert_input_to_datetime`. Programmatic callers (the web service)
    # use these to hand the CLI an unambiguous instant.
    start_iso_datetime: str = None
    end_iso_datetime: str = None

    @classmethod
    def from_args_and_config(
        cls,
        args: argparse.Namespace,
        config: dict = None,
        auth: dict = None,
    ) -> 'Settings':
        """Merge CLI args, parsed TOML config, and class defaults in that priority.

        `config` is the dict returned by ``tomllib.load``. Missing keys at any
        level resolve to ``None`` and fall through to the next priority source.
        """
        config = config or {}

        def set_value(arg_value=None, config_value=None, cls_value=None, required=False):
            """Pick the highest-priority non-empty value: arg > config > default."""
            if arg_value:
                arg_val = getattr(args, arg_value, None)
                if arg_val is not None and str(arg_val).strip():
                    return arg_val
            if config_value is not None and str(config_value).strip():
                return config_value
            if cls_value is not None:
                return cls_value
            if required:
                raise ValueError(f"Required parameter {arg_value or 'config'} is missing")
            return None

        auth = auth or {}
        settings_table = config.get('settings', {})

        # Build the flat name->url map and the nested cameras-by-server map via
        # the shared schema helper, then normalize per-camera crop dimensions to
        # tuple-of-tuples so the rest of the code can keep treating
        # crop_dimensions as a tuple.
        servers_by_name, raw_cameras_by_server = split_servers_and_cameras(config)
        cameras_by_server: dict[str, dict[str, dict]] = {}
        for srv_name, cam_table in raw_cameras_by_server.items():
            cam_map: dict[str, dict] = {}
            for alias, cam_data in cam_table.items():
                entry: dict = {'id': cam_data.get('id')}
                cam_crop = cam_data.get('crop_dimensions')
                if cam_crop is not None:
                    entry['crop_dimensions'] = tuple(tuple(pt) for pt in cam_crop)
                cam_map[str(alias)] = entry
            cameras_by_server[srv_name] = cam_map

        # Resolve the active server and camera. These are per-run values with
        # no config-file source -- they come from CLI args (args > default).
        server = set_value(
            arg_value='server',
            cls_value=cls.server,
        )
        camera_alias = set_value(
            arg_value='camera_alias',
            cls_value=cls.camera_alias,
        )
        if camera_alias is not None:
            camera_alias = str(camera_alias)

        server_ip = servers_by_name.get(server) if server else None
        cam_entry = (
            cameras_by_server.get(server, {}).get(camera_alias)
            if (server and camera_alias) else None
        )
        camera_id = cam_entry.get('id') if cam_entry else None

        # Resolve effective crop: per-camera override beats the global default.
        default_crop = settings_table.get('default_crop_dimensions')
        if default_crop is not None:
            default_crop = tuple(tuple(pt) for pt in default_crop)
        effective_crop = (cam_entry or {}).get('crop_dimensions') or default_crop

        return cls(
            username=auth.get('username', ''),
            password=auth.get('password', ''),
            servers=servers_by_name,
            cameras=cameras_by_server,

            timelapse_multiplier=set_value(
                arg_value='multiplier',
                config_value=settings_table.get('timelapse_multiplier'),
                cls_value=cls.timelapse_multiplier,
            ),
            compression_level=set_value(
                arg_value='quality',
                config_value=settings_table.get('compression_level'),
                cls_value=cls.compression_level,
            ),
            timezone=set_value(
                config_value=settings_table.get('timezone'),
                cls_value=cls.timezone,
            ),
            crop=bool(set_value(
                arg_value='crop',
                config_value=settings_table.get('default_crop'),
                cls_value=cls.crop,
            )),
            default_crop_dimensions=default_crop,
            crop_dimensions=effective_crop,
            font_weight=set_value(
                config_value=settings_table.get('font_weight'),
                cls_value=cls.font_weight,
            ),
            caption=set_value(
                arg_value='caption',
                cls_value=cls.caption,
            ),

            server=server,
            server_ip=server_ip,
            camera_alias=camera_alias,
            camera_id=camera_id,
            input_filename=set_value(arg_value='video_filename', cls_value=cls.input_filename),
            # No config-file source: when -o is omitted, main() builds the
            # canonical default stem via default_output_stem (same convention
            # the web service uses).
            output_filename=set_value(
                arg_value='output_name',
                cls_value=cls.output_filename,
            ),
            # Flag-only, since "deliver here" is a per-invocation choice
            # rather than a stable config-file default. Programmatic
            # callers (the web service) always pass it; humans typically
            # cd into their target directory and omit this.
            output_dir=set_value(
                arg_value='output_dir',
                cls_value=cls.output_dir,
            ),
            # Per-run time range comes from CLI args only (positional
            # date/start/end or the ISO flag pair below) -- no config source.
            date=set_value(
                arg_value='date',
                cls_value=cls.date,
            ),
            start_time=set_value(
                arg_value='start',
                cls_value=cls.start_time,
            ),
            end_time=set_value(
                arg_value='end',
                cls_value=cls.end_time,
            ),
            # ISO 8601 pair, flag-only. Programmatic callers (the web service)
            # use these to hand the CLI an unambiguous instant; humans can use
            # either these or the positional date/start/end form.
            start_iso_datetime=set_value(
                arg_value='start_iso_datetime',
                cls_value=cls.start_iso_datetime,
            ),
            end_iso_datetime=set_value(
                arg_value='end_iso_datetime',
                cls_value=cls.end_iso_datetime,
            ),
        )


# Overlay layout constants for `process_video`. Module-level so they're a
# single, greppable knob and don't get re-bound on every frame. The "max
# text width" fractions cap how much of the frame the timestamp/caption
# block is allowed to occupy: landscape footage gets the middle third
# (text stays a tidy band centered in the frame, doesn't compete with the
# camera image), portrait/square footage keeps the historical 80% (a third
# of an already-narrow frame would be unreadably small). The bottom margin
# is the gap between the BOTTOM-MOST text line's baseline and the floor of
# the frame -- preserved whether the bottom line is the timestamp (no
# caption) or the caption (timestamp moves up to keep this invariant).
LANDSCAPE_MAX_TEXT_WIDTH_FRACTION = 1 / 3
DEFAULT_MAX_TEXT_WIDTH_FRACTION = 0.8
BOTTOM_MARGIN_FRACTION = 0.10


def fit_to_screen(frame, window_name, screen_width, screen_height):
    """Resize a frame to fit within the screen dimensions."""
    original_height, original_width = frame.shape[:2]

    # Determine scaling factor to fit frame within screen dimensions
    scale_width = screen_width / original_width
    scale_height = screen_height / original_height
    scale = min(scale_width, scale_height)  # Use the smaller scale factor

    resized_width = int(original_width * scale)
    resized_height = int(original_height * scale)
    resized_frame = cv2.resize(frame, (resized_width, resized_height))

    return resized_frame, scale


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort copy of `text` to the OS clipboard; returns True on success.

    Shells out to a platform-native tool so no extra dependency is needed:
    ``pbcopy`` (macOS), ``clip`` (Windows), or ``xclip``/``xsel`` (Linux/X11).
    Returns False if none are available or the copy fails -- the clipboard is
    a convenience, never a hard requirement.
    """
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform.startswith("win"):
        candidates = [["clip"]]
    else:
        candidates = [
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for cmd in candidates:
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
    return False


def select_crop(frame) -> tuple[tuple[int, int], tuple[int, int]]:
    """Open the interactive ROI selector on `frame` and return crop dimensions.

    Used by both ``process_video`` (when ``extract -c`` is run without
    preconfigured crop dimensions) and the standalone ``crop`` subcommand.
    Emits the selected coordinates through the active reporter -- both a
    structured ``crop_dimensions`` event and two TOML-paste-ready lines --
    so a human can copy the result straight into their config file.

    Requires a display: ``cv2.selectROI`` opens a GUI window.
    """
    reporter = get_reporter()

    # Create a window to get screen dimensions
    window_name = "Select ROI"

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    temp_width, temp_height = (960, 540)
    cv2.resizeWindow(window_name, temp_width, temp_height)  # Temporary window size

    # Resize frame to fit screen dimensions
    resized_frame, scale = fit_to_screen(frame, window_name, temp_width, temp_height)

    instructions = "Click and drag to select desired region, then press Enter."

    # Replace 'first_frame' with frame with instructions
    frame_with_text = resized_frame.copy()
    text_size = cv2.getTextSize(instructions, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0]
    text_x = (frame_with_text.shape[1] - text_size[0]) // 2
    text_y = 30  # Position at the top of the frame
    cv2.putText(frame_with_text, instructions, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

    # Show the resized frame and allow ROI selection
    roi = cv2.selectROI(window_name, frame_with_text, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()  # Close the ROI selection window
    # On macOS the window is only actually torn down once the GUI event loop
    # is pumped. Without this the leftover window beachballs whenever the main
    # thread next blocks (e.g. the crop subcommand's input() prompt), since
    # nothing is servicing its events. A few waitKey ticks flush the teardown.
    for _ in range(5):
        cv2.waitKey(1)

    # Scale ROI coordinates back to original resolution
    x, y, w, h = map(int, roi)
    x = int(x / scale)
    y = int(y / scale)
    w = int(w / scale)
    h = int(h / scale)

    coords = ((x, y), (w, h))
    # Render the same value in TOML array syntax so users can paste it
    # straight into either [settings].default_crop_dimensions or a
    # [<server>.<alias>].crop_dimensions entry.
    toml_coords = f"[[{x}, {y}], [{w}, {h}]]"

    # Auto-copy the per-camera crop_dimensions line so the common path
    # (paste into [<server>.<alias>]) is a single cmd-V away.
    clip_line = f"crop_dimensions = {toml_coords}"
    copied_suffix = "   (copied to clipboard)" if _copy_to_clipboard(clip_line) else ""

    # Pad the labels to a common width so the values (everything after the
    # "= ") line up vertically on their first character despite the differing
    # label lengths.
    label_settings = "Under [settings]: default_crop_dimensions"
    label_camera = "Under [<server>.<alias>]: crop_dimensions"
    label_width = max(len(label_settings), len(label_camera))

    # Leading/trailing newlines frame the result so it stands out from the
    # ROI-window teardown noise; the future-use block ends with a blank line.
    reporter.info(f"\nCrop coordinates selected: {coords}\n", crop_dimensions=coords)
    reporter.info(
        "For future use, copy one of these lines into your config file:\n"
        f"  {label_settings.ljust(label_width)} = {toml_coords}\n"
        f"  {label_camera.ljust(label_width)} = {toml_coords}{copied_suffix}\n"
    )
    return coords


def _write_crop_to_config(
    config_file: str,
    server: str,
    camera_alias: str,
    toml_coords: str,
) -> tuple[bool, str]:
    """Insert or update crop_dimensions under [<server>.<alias>].

    Edits the config file textually (tomllib is read-only) and validates that
    the result still parses as TOML *before* writing -- on any problem the file
    is left untouched and a message is returned for the caller to surface.
    Supports the canonical explicit-table form used by default.config:

        [<server>.<alias>]
        id = ...
        crop_dimensions = [[x, y], [w, h]]

    Returns ``(success, message)``.
    """
    path = Path(config_file)
    try:
        original = path.read_text()
    except OSError as e:
        return False, f"Could not read {path.name}: {e}"

    lines = original.splitlines(keepends=True)
    # The alias is a TOML key; it may appear bare or quoted in the header.
    candidate_headers = {
        f"[{server}.{camera_alias}]",
        f'[{server}."{camera_alias}"]',
    }
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() in candidate_headers),
        None,
    )
    if header_idx is None:
        return False, (
            f"Could not locate [{server}.{camera_alias}] in "
            f"{path.name}; left it unchanged (the line is on your clipboard to paste)."
        )

    # The section runs until the next table header or end of file.
    section_end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        if lines[j].lstrip().startswith('['):
            section_end = j
            break

    new_line = f"crop_dimensions = {toml_coords}\n"
    existing_idx = None
    last_kv_idx = header_idx
    for j in range(header_idx + 1, section_end):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith('#'):
            continue
        last_kv_idx = j
        if '=' in stripped and stripped.split('=', 1)[0].strip() == 'crop_dimensions':
            existing_idx = j

    if existing_idx is not None:
        lines[existing_idx] = new_line
    else:
        # Guard against a missing trailing newline on the line we insert after
        # (e.g. the section is the last thing in the file with no final \n).
        if lines[last_kv_idx] and not lines[last_kv_idx].endswith('\n'):
            lines[last_kv_idx] = lines[last_kv_idx] + '\n'
        lines.insert(last_kv_idx + 1, new_line)

    new_content = "".join(lines)
    # Safety net: never write something that won't parse back.
    try:
        tomllib.loads(new_content)
    except tomllib.TOMLDecodeError as e:
        return False, f"Aborted: the edit would produce invalid TOML ({e}); {path.name} unchanged."

    try:
        path.write_text(new_content)
    except OSError as e:
        return False, f"Could not write {path.name}: {e}"

    verb = "Updated" if existing_idx is not None else "Added"
    return True, (
        f"{verb} crop_dimensions in [{server}.{camera_alias}] of {path.name}."
    )


def process_video(original_video_path: str, output_video_path: str = None, timestamps: list[datetime] = None) -> str:
    """
    Processes a video by cropping, timelapsing, and timestamping it based on attributes of the settings object.

    If timestamps are provided, they are added to the video.

    Args:
        original_video_path (str):                  The filepath of the original video.
        output_video_path (str, optional):          The filepath for the output video. 
        timestamps (list of datetime, optional):    A list of timestamps to be added to the video. 
                                                    If provided, each frame's timestamp will be added to the video.

    Returns:
        str: The filepath of the processed video.

    Raises:
        SystemExit: If the original video file cannot be opened.
        TypeError: If the timelapse multiplier is invalid.
    """
    reporter = get_reporter()

    def calculate_font_scale(video_width: int, video_height: int, caption: Optional[str]) -> float:
        # Static timestamp string: the format is fixed-width so its rendered
        # width never changes frame-to-frame.
        timestamp_string = datetime(2025, 3, 28, 6, 43, 20).strftime('%Y-%m-%d %H:%M:%S')

        # Landscape footage (width > height) caps to the middle third so
        # the overlay doesn't dominate wide frames; portrait/square keeps
        # the historical 80% cap.
        is_landscape = video_width > video_height
        fraction = (LANDSCAPE_MAX_TEXT_WIDTH_FRACTION if is_landscape
                    else DEFAULT_MAX_TEXT_WIDTH_FRACTION)
        max_text_width = int(video_width * fraction)

        # Pick a font scale that fits whichever line (timestamp or caption)
        # is widest at scale=1. The caption is rendered at 0.8x the
        # timestamp's scale (preserved from the historical 0.8 ratio so a
        # plain timestamp looks identical to before -- the cap binds the
        # output, the ratio governs visual hierarchy), so its effective
        # width at the chosen scale is `caption_w_at_1 * scale * 0.8`.
        # Comparing widths AT SCALE=1 lets us solve for `scale` in one step.
        ts_w_at_1 = cv2.getTextSize(timestamp_string, cv2.FONT_HERSHEY_SIMPLEX, 1, settings.font_weight)[0][0]
        widest_at_1 = ts_w_at_1
        if caption:
            caption_w_at_1 = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 1, settings.font_weight)[0][0]
            widest_at_1 = max(widest_at_1, caption_w_at_1 * 0.8)

        return max_text_width / widest_at_1


    def calculate_xy_text_position(
        video_height: int,
        video_width: int,
        timestamp_string: str,
        font_scale: float,
        caption_y_offset: Optional[int] = None,
    ) -> tuple[int, int]:
        text_size = cv2.getTextSize(timestamp_string, cv2.FONT_HERSHEY_SIMPLEX, font_scale, settings.font_weight)[0]
        text_width, _ = text_size
        x_position = (video_width - text_width) // 2  # Center horizontally

        # Anchor the BOTTOM-MOST text line's baseline at a fixed margin
        # above the floor. Without a caption that's the timestamp itself
        # (identical to the legacy behavior). With a caption, the caption
        # sits below the timestamp by exactly `caption_y_offset`, so the
        # timestamp moves up by that offset -- which keeps the caption's
        # baseline at the same 10% margin and prevents the bleed-off-the-
        # bottom symptom that motivated this change.
        bottom_anchor_y = int(video_height * (1 - BOTTOM_MARGIN_FRACTION))
        y_position = bottom_anchor_y - (caption_y_offset or 0)

        return x_position, y_position

    
    multiplier = settings.timelapse_multiplier

    # Ensure the input file has the correct extension
    if not original_video_path.endswith('.mp4'):
        original_video_path = original_video_path + '.mp4'

    # Multiplier must be a positive integer; this is the final guard after
    # Settings has already applied its arg/config/default priority.
    if multiplier <= 0 or not isinstance(multiplier, int):
        raise TypeError("Timelapse multiplier must be a positive integer.")

    # If not specified, rename the output file to the same as input with speed appended to it (e.g. video_4x.mp4)
    if output_video_path is None:
        output_video_path=f'_{multiplier}x.'.join(original_video_path.split('.'))

    vid = cv2.VideoCapture(original_video_path)
    if not vid.isOpened():
        reporter.error("VideoOpenError", f"Could not open video file: {original_video_path}")
        exit(1)

    fps = vid.get(cv2.CAP_PROP_FPS)
    success, frame = vid.read()
    if not success or frame is None:
        vid.release()
        reporter.error(
            "VideoReadError",
            f"Could not read any frames from video file: {original_video_path}",
        )
        exit(1)
    height, width = frame.shape[:2]

    # Handle cropping setup
    if settings.crop:
        if settings.crop_dimensions is None:
            settings.crop_dimensions = select_crop(frame)

        (x, y), (crop_width, crop_height) = settings.crop_dimensions

        # Validate crop dimensions
        if x + crop_width > width or y + crop_height > height:
            reporter.warning(
                f"Crop dimensions ({x}, {y}, {crop_width}, {crop_height}) "
                f"exceed frame size ({width}, {height})"
            )
            crop_width = min(crop_width, width - x)
            crop_height = min(crop_height, height - y)
            reporter.info(f"Adjusted crop to: ({x}, {y}, {crop_width}, {crop_height})")
    else:
        crop_width, crop_height = width, height
        x, y = 0, 0

    total_frames = int(vid.get(cv2.CAP_PROP_FRAME_COUNT))
    if timestamps:
        number_of_timestamps = len(timestamps)

    font_scale = calculate_font_scale(crop_width, crop_height, settings.caption)

    # Pre-compute caption layout once. The caption text and font scale don't
    # change frame-to-frame, so measuring inside the render loop would be
    # wasteful. We anchor the caption directly below the timestamp using a
    # natural single-line leading (~25% of the timestamp's text height).
    caption_font_scale = font_scale * 0.8 if settings.caption else None
    caption_x = None
    caption_y_offset = None
    if settings.caption:
        (caption_w, caption_h), _ = cv2.getTextSize(
            settings.caption,
            cv2.FONT_HERSHEY_SIMPLEX,
            caption_font_scale,
            settings.font_weight,
        )
        # Use a representative timestamp string to derive the line gap. Real
        # per-frame timestamps differ only in their digit values so their
        # vertical metrics are stable.
        sample_ts = datetime(2025, 1, 1, 12, 0, 0).strftime('%Y-%m-%d %H:%M:%S')
        (_, sample_ts_h), sample_ts_baseline = cv2.getTextSize(
            sample_ts,
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            settings.font_weight,
        )
        line_gap = max(2, int(sample_ts_h * 0.25))
        caption_x = (crop_width - caption_w) // 2
        # Offset from the timestamp baseline (cv2 putText's y arg) down to the
        # caption's own baseline: descender of the timestamp + gap + caption height.
        caption_y_offset = sample_ts_baseline + line_gap + caption_h

    reporter.stage(
        "timelapsing",
        "Timelapsing footage",
        output=output_video_path,
        total_frames=total_frames,
    )
    # Use crop dimensions for output video
    writer = cv2.VideoWriter(output_video_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (crop_width, crop_height))
    count = 0

    while success:
        if settings.crop:
            finished_frame = frame[y:y+crop_height, x:x+crop_width]
            if finished_frame.shape[:2] != (crop_height, crop_width):
                reporter.warning(
                    f"Cropped frame size {finished_frame.shape[:2]} doesn't match "
                    f"expected ({crop_height}, {crop_width})"
                )
        else:
            finished_frame = frame

        if timestamps:
            frame_position = vid.get(cv2.CAP_PROP_POS_FRAMES)
            # Some containers report an unreliable frame count (0 or negative)
            # even when frames decode fine; fall back to the running count so
            # the mapping never divides by zero, and clamp the resulting index
            # so a slightly-off frame count can't index past the timestamps.
            denominator = total_frames if total_frames > 0 else max(count + 1, 1)
            ts_index = int(frame_position / denominator * (number_of_timestamps - 1))
            ts_index = max(0, min(ts_index, number_of_timestamps - 1))
            current_timestamp = timestamps[ts_index]
            timestamp_string = current_timestamp.strftime('%Y-%m-%d %H:%M:%S')
            x_pos, y_pos = calculate_xy_text_position(
                crop_height, crop_width, timestamp_string, font_scale,
                caption_y_offset=caption_y_offset,
            )
            cv2.putText(finished_frame, timestamp_string, (x_pos, y_pos), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), settings.font_weight, cv2.LINE_AA)
            if settings.caption:
                cv2.putText(
                    finished_frame,
                    settings.caption,
                    (caption_x, y_pos + caption_y_offset),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    caption_font_scale,
                    (255, 255, 255),
                    settings.font_weight,
                    cv2.LINE_AA,
                )

        if count % multiplier == 0:
            writer.write(finished_frame)

        success, frame = vid.read()
        count += 1
        reporter.update("timelapsing", count, total_frames, unit="frames")

    writer.release()
    vid.release()

    return output_video_path


def compress_video(original_video_path: str, compressed_video_path: str = None, codec: str = "libx264") -> str:
    """
    Compresses a video file to a specified quality set by the settings object.

    Args:
        original_video_path (str): The filepath of the original video.
        compressed_video_path (str, optional): The desired file path for the compressed video. Defaults to None.
        codec (str): Compression codec (default: 'libx264').

    Returns:
        str: The filepath of the compressed video.

    Raises:
        ValueError: If the quality is not 'low', 'medium', or 'high'.
    """

    # Ensure the input file has the correct extension
    if not original_video_path.endswith('.mp4'):
        original_video_path += '.mp4'

    quality = settings.compression_level

    # If not specified, rename the output file to the same as input with codec and bitrate appended to it (e.g. video_libx264_500K.mp4)
    if compressed_video_path is None:
        compressed_video_path = f'_{codec}_{quality}.'.join(original_video_path.split('.'))

    if quality == 'low':
        bitrate = '250K'
        resolution = (1280, 720)
    elif quality == 'medium':
        bitrate = '500K'
        resolution = (1920, 1080)
    elif quality == 'high':
        bitrate = '1M'
        resolution = (1920, 1080)
    else:
        raise ValueError("Compression quality must be one of: 'low', 'medium', 'high'")

    if settings.crop:
        resolution = settings.crop_dimensions[1] # crop_dimensions[1] gives (width,height)

    reporter = get_reporter()
    reporter.stage("compression", "Compressing video", output=compressed_video_path)

    with VideoFileClip(original_video_path, target_resolution=resolution) as video:
        # Funnel MoviePy's proglog progress into our reporter so it drives a
        # single, real per-frame compression progress bar (and JSON events).
        video.write_videofile(
            compressed_video_path,
            bitrate=bitrate,
            codec=codec,
            logger=reporter.moviepy_logger("compression"),
        )

    return compressed_video_path


_BOOL_FLAG_TRUE = {"true", "yes", "1"}
_BOOL_FLAG_FALSE = {"false", "no", "0"}


def _parse_bool_flag(value: str) -> bool:
    """argparse ``type`` for value-taking boolean flags (e.g. ``--crop true``).

    Accepts ``true/false`` (case-insensitive) plus the common ``yes/no`` and
    ``1/0`` synonyms. Raises ``argparse.ArgumentTypeError`` on anything else so
    argparse reports a clean usage error rather than silently coercing.
    """
    normalized = str(value).strip().lower()
    if normalized in _BOOL_FLAG_TRUE:
        return True
    if normalized in _BOOL_FLAG_FALSE:
        return False
    raise argparse.ArgumentTypeError(
        f"expected true or false (got {value!r})"
    )


def parse_arguments():
    """
    Parses command-line arguments for video processing tasks.

    Supports three subcommands: 'extract' (retrieve and process video from Exacqvision),
    'compress' (compress an existing video), and 'timelapse' (apply timelapse effect).
    A subcommand is required; argparse prints usage and exits if one isn't provided.

    Also accepts global options that apply to every subcommand:
    --progress-format {auto,human,json} and -q/--quiet.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """

    arg_parser = argparse.ArgumentParser()

    # Global progress-tracking options apply to every subcommand.
    arg_parser.add_argument(
        '--progress-format',
        choices=['auto', 'human', 'json'],
        default='auto',
        help=(
            'Progress output format. "auto" picks json when stdout is not a TTY '
            'or when EXACQMAN_PROGRESS_FORMAT=json, otherwise human.'
        ),
    )
    arg_parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='Suppress informational messages in human output mode.',
    )

    subparsers = arg_parser.add_subparsers(dest='command', required=True)

    # Extract mode subcommand
    extract_parser = subparsers.add_parser('extract', help='Extract, timelapse, and compress a video file')
    extract_parser.add_argument('camera_alias', type=str, help='Name of camera wanted (required)')
    extract_parser.add_argument('date', nargs='?', default=None, type=str, help='Date of the requested video. If the footage spans past midnight, provide the date on which the footage starts. (e.g. 3/11)')
    extract_parser.add_argument('start', nargs='?', default=None, type=str, help='Starting timestamp of video requested (e.g. 11am)')
    extract_parser.add_argument('end', nargs='?', default=None, type=str, help='Ending timestamp of video requested (e.g. 5pm)')
    # `config_file` is `nargs='?'` so callers can use the equivalent `--config`
    # flag instead -- handy for programmatic callers (the web service) that
    # don't want to compose 5 positionals in the right order. main() validates
    # that exactly one of the two sources is set.
    extract_parser.add_argument('config_file', nargs='?', default=None, type=str, help='Filepath of local TOML config file (or use --config)')
    extract_parser.add_argument(
        '--config',
        type=str,
        default=None,
        dest='config_flag',
        help=(
            'Filepath of local TOML config file. Flag-form alternative to the '
            'positional config_file; one of the two must be set.'
        ),
    )
    # ISO 8601 flag pair (e.g. 2026-05-27T09:30:00 or with offset/TZ).
    # When both are given, they take precedence over the positional
    # date/start/end form and skip the year/day fixup heuristics in
    # `convert_input_to_datetime`. Programmatic callers (the web service)
    # use these so the timestamp travels unambiguously instead of being
    # round-tripped through a lossy `%m/%d` + `%I:%M%p` representation.
    # The "iso" in the flag name is deliberate -- it makes the expected
    # format obvious at the call site and distinguishes these from the
    # human-friendly positional `date` / `start` / `end` arguments.
    extract_parser.add_argument(
        '--start-iso-datetime',
        type=str,
        default=None,
        dest='start_iso_datetime',
        help=(
            'ISO 8601 start datetime, e.g. 2026-05-27T09:30:00 (optionally '
            'with timezone offset, e.g. 2026-05-27T09:30:00-04:00). When set, '
            '--end-iso-datetime must also be set and the positional '
            'date/start/end arguments must be omitted.'
        ),
    )
    extract_parser.add_argument(
        '--end-iso-datetime',
        type=str,
        default=None,
        dest='end_iso_datetime',
        help=(
            'ISO 8601 end datetime, e.g. 2026-05-27T09:45:00 (optionally with '
            'timezone offset). When set, --start-iso-datetime must also be set '
            'and the positional date/start/end arguments must be omitted.'
        ),
    )
    extract_parser.add_argument(
        '--credentials',
        type=str,
        default=None,
        help=(
            'Path to TOML credentials file. Overrides settings.credentials_file '
            'in the config. One of the two must be set.'
        ),
    )
    extract_parser.add_argument('--server', type=str, help='Server name (must match a top-level [<server>] table in the config file)')
    extract_parser.add_argument('-o', '--output_name', type=str, help='Desired filepath')
    extract_parser.add_argument(
        '--output-dir',
        type=str,
        default=None,
        dest='output_dir',
        help=(
            'Directory to deliver the final extracted video into. When set, '
            'the directory is created if missing, the pipeline writes the '
            'raw download, timelapsed, and final compressed files inside it, '
            'and on successful completion the intermediates are removed and '
            'the compressed file is renamed to a bare `{name}.mp4` -- so the '
            'directory ends up holding exactly one user-facing deliverable. '
            'When omitted, behavior is unchanged: all three files land in '
            'the current working directory with their stem-based names.'
        ),
    )
    extract_parser.add_argument('--quality', type=str, choices=['low', 'medium', 'high'], help='Desired video quality')
    extract_parser.add_argument('--multiplier', type=int, help='Desired timelapse multiplier (must be a positive integer)')
    extract_parser.add_argument('-c', '--crop', type=_parse_bool_flag, default=None, metavar='{true,false}', help='Crop the video (true/false). When unset, defers to [settings].default_crop in the config. Uses per-camera crop_dimensions, falling back to default_crop_dimensions; prompts if neither is set.')
    extract_parser.add_argument('--caption', type=str, help=f'Add caption below timestamp (max of {Settings.caption_limit} chars)')

    # Compress subcommand
    compress_parser = subparsers.add_parser('compress', help='Compress a video file')
    compress_parser.add_argument('video_filename', type=str, help='Video file to compress')
    compress_parser.add_argument('quality', default=None, type=str, choices=['low', 'medium', 'high'], help='Desired compression quality')
    compress_parser.add_argument('-o', '--output_name', type=str, help='Desired filepath')

    # Timelapse subcommand
    timelapse_parser = subparsers.add_parser('timelapse', help='Create a timelapse video')
    timelapse_parser.add_argument('video_filename', type=str, help='Video file for timelapse')
    timelapse_parser.add_argument('multiplier', default=None, type=int, help='Desired timelapse multiplier (must be a positive integer)')
    timelapse_parser.add_argument('-o', '--output_name', default=None, type=str, help='Desired filepath')
    timelapse_parser.add_argument('-c', '--crop', type=_parse_bool_flag, default=None, metavar='{true,false}', help='Crop the video (true/false). When unset, defers to [settings].default_crop in the config. Uses per-camera crop_dimensions, falling back to default_crop_dimensions; prompts if neither is set.')
    timelapse_parser.add_argument('--caption', type=str, help=f'Add caption below timestamp (max of {Settings.caption_limit} chars)')

    # Crop subcommand: grab a recent frame from a camera and open the
    # interactive ROI selector to capture crop dimensions, without running
    # any extract/timelapse/compress steps. CLI-only utility for quickly
    # populating per-camera crop_dimensions in the config. Mirrors extract's
    # config/credentials/server resolution.
    crop_parser = subparsers.add_parser(
        'crop',
        help='Grab a recent frame and pick crop dimensions for a camera (CLI-only).',
    )
    crop_parser.add_argument('--camera', type=str, required=True, dest='camera_alias', help='Camera alias (required; must match a [<server>.<alias>] entry).')
    crop_parser.add_argument('config_file', nargs='?', default=None, type=str, help='Filepath of local TOML config file (or use --config).')
    crop_parser.add_argument(
        '--config',
        type=str,
        default=None,
        dest='config_flag',
        help='Filepath of local TOML config file. Flag-form alternative to the positional config_file; one of the two must be set.',
    )
    crop_parser.add_argument(
        '--credentials',
        type=str,
        default=None,
        help='Path to TOML credentials file. Overrides settings.credentials_file in the config.',
    )
    crop_parser.add_argument('--server', type=str, help='Server name (must match a top-level [<server>] table in the config file).')
    crop_parser.add_argument(
        '--lookback-minutes',
        type=int,
        default=15,
        dest='lookback_minutes',
        help='How far back from now to request the probe clip, in minutes (default: 15). Increase if the camera is motion-triggered and has no recent footage.',
    )

    return arg_parser.parse_args()


EXACQMAN_METADATA_VERSION = 1
# Schema version for the JSON payload stored in the mp4 `comment` tag.
# Bump when the on-disk shape changes in a way that downstream readers
# (e.g. exacqman-web's FileService) must be aware of. Readers should
# accept any version <= the latest they understand; missing keys are
# fine, extra keys are fine, version mismatches should be logged and
# the payload treated as best-effort.


def _locate_ffmpeg() -> Optional[str]:
    """Locate the ffmpeg executable, preferring the same binary MoviePy uses.

    MoviePy resolves ffmpeg via, in order:
      1. The ``FFMPEG_BINARY`` env var (if set to anything but the sentinel
         ``"ffmpeg-imageio"``).
      2. The binary bundled with ``imageio-ffmpeg`` (always installed as a
         transitive dep).
      3. A system ``ffmpeg`` on ``PATH`` (last resort).

    Mirroring that order here means we embed metadata with the same binary
    that just compressed the file, avoiding "works in moviepy, breaks in
    metadata-write" version-skew surprises.

    Returns:
        Absolute path to an ffmpeg binary, or ``None`` if none can be found.
    """
    env_binary = os.environ.get("FFMPEG_BINARY")
    if env_binary and env_binary != "ffmpeg-imageio":
        return env_binary
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _embed_extract_metadata(file_path: Path, metadata: Dict[str, Any]) -> None:
    """Embed exacqman extract metadata into an mp4 in place.

    Writes a JSON-encoded blob (schema versioned via
    ``EXACQMAN_METADATA_VERSION``) into the mp4 container's ``comment``
    tag using a no-re-encode ffmpeg pass (``-codec copy``), so the operation
    is fast and lossless. We also set the standard ``title`` tag to the
    file stem so generic media players surface a recognisable name.

    The point of this is provenance: long after the config file rotates
    or the camera gets renamed, the file still knows what it is. The
    web's FileService reads this back to populate the file-browser's
    camera column, replacing the brittle filename-parsing path that
    failed for any custom ``-o`` filename.

    Failures here never raise: if ffmpeg isn't locatable, the metadata
    write fails, or the rename races, we log a warning via the reporter
    and leave the original file untouched. The compressed video is the
    user-facing deliverable; metadata is a bonus, not a precondition.

    Args:
        file_path: Path to the final compressed mp4 to tag in place.
        metadata: Dict of payload fields; serialised verbatim into JSON.
                  Keys with ``None`` / empty-string values are dropped so
                  optional fields (e.g. ``caption``) don't muddy the blob.
    """
    reporter = get_reporter()
    ffmpeg_bin = _locate_ffmpeg()
    if not ffmpeg_bin:
        reporter.warning(
            "ffmpeg binary not found; skipping metadata embed "
            f"for {file_path.name}"
        )
        return

    payload = {
        "exacqman_metadata_version": EXACQMAN_METADATA_VERSION,
    }
    for key, value in metadata.items():
        if value is None or value == "":
            continue
        payload[key] = value
    json_blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    # Write to a sibling temp file then atomically swap. Writing in place
    # isn't supported by ffmpeg (it would truncate the input mid-read);
    # the temp + replace pattern handles that and gives us a clean rollback
    # path on failure. Keep the `.mp4` suffix on the temp name so ffmpeg
    # can infer the output muxer from the extension (using `.tmp` makes
    # ffmpeg refuse with "Unable to choose an output format"); `-f mp4`
    # is set explicitly too for belt-and-suspenders.
    tmp_path = file_path.with_name(file_path.stem + ".tagging.mp4")
    args = [
        ffmpeg_bin,
        "-y",                       # overwrite tmp if it exists
        "-loglevel", "error",       # silence the per-frame chatter; surface real errors
        "-i", str(file_path),
        "-map", "0",                # carry every stream over (video, audio if any)
        "-codec", "copy",           # no re-encode -- pure container rewrite
        "-f", "mp4",                # force the output muxer regardless of temp name
        "-metadata", f"comment={json_blob}",
        "-metadata", f"title={file_path.stem}",
        str(tmp_path),
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            reporter.warning(
                f"ffmpeg metadata embed failed for {file_path.name} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return
        tmp_path.replace(file_path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        reporter.warning(
            f"Failed to embed metadata in {file_path.name}: {exc}"
        )
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _finalize_extract_output_dir(
    output_dir: Path,
    output_stem: str,
    multiplier: int,
    compressed_path: Path,
) -> str:
    """Collapse the extract pipeline's three outputs into one deliverable.

    The extract pipeline writes three files inside ``output_dir``:

      1. ``{stem}.mp4``                              -- raw download
      2. ``{stem}_{multiplier}x.mp4``                -- timelapsed
      3. ``{stem}_{multiplier}x_libx264_{quality}.mp4`` -- final compressed
         (received here as ``compressed_path``)

    This deletes (1) and (2), then renames (3) to ``{output_dir}/{stem}.mp4``
    so the directory contains exactly one user-facing file -- the
    finished artifact. The web service depends on this contract: when
    it spawns the CLI with ``--output-dir=<exports/>``, the file it
    finds at the reported ``done.output`` path is the only one left
    behind, with a clean stem-based name and no codec suffix to strip.

    Cleanup failures (unlink races, permission glitches) are logged as
    warnings but don't fail the job -- the user already has the final
    file, which is what matters.

    Returns:
        Absolute path to the renamed deliverable, as a string (matches
        the type ``reporter.done(output=...)`` expects).
    """
    raw = output_dir / f"{output_stem}.mp4"
    timelapsed = output_dir / f"{output_stem}_{multiplier}x.mp4"
    deliverable = output_dir / f"{output_stem}.mp4"  # same path as `raw`; raw must be deleted first

    reporter = get_reporter()
    for path in (raw, timelapsed):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            reporter.warning(
                f"Failed to remove intermediate {path}: {exc}",
            )

    # `Path.replace` is the cross-platform atomic-on-same-filesystem
    # rename; it overwrites the destination if it somehow still exists
    # (we just deleted `raw`, which IS the destination, so the slot
    # should be empty -- but defensive code never hurts here).
    compressed_path.replace(deliverable)
    return str(deliverable)


def _date_string_has_year(date: str) -> bool:
    """Detect whether `date` carries an explicit year component.

    `dateutil.parser.parse` fills in missing fields from its `default`
    argument (default-of-the-default is `datetime.now()`). If we parse
    the same string twice with two wildly different defaults and get
    back the same year, the year must have come from the input itself
    rather than the default -- i.e. it was explicit.

    This lets `convert_input_to_datetime` distinguish "5/27" (year
    inferred) from "5/27/26" (year explicit) without baking in a
    fragile regex for every accepted date format.
    """
    if not date:
        return False
    try:
        with_low = duparse(date, default=datetime(2000, 1, 1))
        with_high = duparse(date, default=datetime(2100, 1, 1))
    except (ValueError, OverflowError, TypeError):
        # Malformed input gets re-parsed (and erred) by the main path
        # in `convert_input_to_datetime`; here we just fall back to
        # "no explicit year" so the heuristic stays conservative.
        return False
    return with_low.year == with_high.year


def convert_input_to_datetime(date: str, start: str, end: str) -> tuple[datetime, datetime]:
    """Convert date and time strings to datetime objects for video extraction.

    Args:
        date: Date string. Accepts year-less MM/DD ("5/27") or year-bearing
            variants ("5/27/26", "5/27/2026", "2026-05-27", ...). Anything
            dateutil can parse works.
        start: Start time (e.g. "6 pm", "18:30").
        end: End time (e.g. "6 pm", "18:30").

    Returns:
        (start, end) as naive datetime objects. Two convenience fixups
        are applied for the human input shorthand:

        * If the user didn't supply a year and the parsed start lands in
          the future, both timestamps are shifted back one year (the
          user almost certainly meant "the most recent past 5/27", not
          "11 months from now").
        * If the end time falls before the start time on the same day,
          the end date is rolled forward 24h (handles 11pm -> 1am spans).

        When the user supplies an explicit year (e.g. "5/27/26 6pm"), the
        year-shift is suppressed so the input is honored verbatim.
    """

    start_datetime = duparse(f'{date} {start}')
    end_datetime = duparse(f'{date} {end}')

    if not _date_string_has_year(date) and start_datetime > datetime.now():
        # Year was inferred from "current year" and we're aimed at the
        # future -- shift back so the user's "5/27" means "the most
        # recent past 5/27". An explicit year ("5/27/26") bypasses this
        # so future-dated extracts stay future-dated.
        start_datetime = start_datetime - relativedelta(years=1)
        end_datetime = end_datetime - relativedelta(years=1)

    if end_datetime < start_datetime:
        end_datetime = end_datetime + timedelta(days=1)

    return start_datetime, end_datetime


settings = None

def main():
    """
    Main entry point for video processing script.

    Handles three modes based on command-line arguments:
    - 'extract': Retrieves video from an Exacqvision server, applies timelapse, and compresses it.
    - 'compress': Compresses an existing video file.
    - 'timelapse': Applies a timelapse effect to an existing video file.

    Uses a configuration file and command-line arguments to set parameters.
    """
    global settings

    args = parse_arguments()

    # Initialize the global progress reporter as early as possible so any
    # downstream code path (including config errors) can use it.
    reporter = init_reporter(format=args.progress_format, quiet=args.quiet)

    config = None

    # Resolve the config file from either the positional `config_file` or the
    # equivalent `--config` flag. The flag form exists so programmatic callers
    # (the web service) can compose a command using flags only, without having
    # to fill in placeholder positionals for date/start/end. Humans typing the
    # canonical 5-positional form still hit the positional slot directly.
    # When both are given they must agree; mismatched values are a usage error.
    positional_config = getattr(args, 'config_file', None)
    flag_config = getattr(args, 'config_flag', None)
    if positional_config and flag_config and positional_config != flag_config:
        get_reporter().error(
            "ConfigError",
            (
                "Both positional config_file and --config were given with "
                f"different values ({positional_config!r} vs {flag_config!r}); "
                "specify only one."
            ),
        )
        exit(1)
    config_file = positional_config or flag_config
    # Mirror the resolved value back onto args so the rest of main() sees a
    # consistent `args.config_file` regardless of which form the caller used.
    args.config_file = config_file
    auth = None
    if config_file:
        config = import_config(config_file)
        credentials_path = resolve_credentials_path(
            config_file,
            config,
            getattr(args, 'credentials', None),
        )
        auth = import_credentials(credentials_path)

    settings = (
        Settings.from_args_and_config(args, config, auth)
        if config
        else Settings.from_args_and_config(args)
    )

    # Enforce caption length on the effective post-merge value so the rule
    # applies uniformly regardless of source (CLI --caption or [settings] caption).
    if settings.caption and len(settings.caption) > Settings.caption_limit:
        reporter.error(
            "CaptionTooLong",
            (
                f"Caption is {len(settings.caption)} characters; the maximum "
                f"allowed is {Settings.caption_limit}."
            ),
        )
        exit(1)

    try:
        if args.command == 'extract':
            # extract needs a config: servers, cameras, timezone, and the
            # credentials path all live there. Fail clearly before anything
            # tries to read settings.timezone (ZoneInfo(None) would crash).
            if not config:
                reporter.error(
                    "ConfigError",
                    "extract requires a config file. Provide it positionally or with --config.",
                )
                exit(1)

            # Resolve server/camera up front so a missing or typo'd value
            # fails fast with a clear message before any network I/O.
            if not settings.server:
                reporter.error(
                    "ConfigError",
                    "No server selected. Pass --server <name> (must match a top-level [<server>] table in the config).",
                )
                exit(1)
            if not settings.server_ip:
                reporter.error(
                    "ConfigError",
                    f'Server "{settings.server}" is not defined as a top-level [<server>] table in the config.',
                )
                exit(1)
            if not settings.camera_id:
                reporter.error(
                    "ConfigError",
                    (
                        f'Camera alias "{settings.camera_alias}" is not defined as a '
                        f'[{settings.server}.{settings.camera_alias}] table in the config.'
                    ),
                )
                exit(1)

            timezone = ZoneInfo(settings.timezone)

            # Two ways to specify the time range:
            #   1. ISO 8601 flags (--start-iso-datetime / --end-iso-datetime)
            #      -- the programmatic form. Unambiguous: no year/day
            #      fixups, full precision down to seconds, optional
            #      timezone offset.
            #   2. Positional date + start + end -- the human form. Goes
            #      through `convert_input_to_datetime`, which infers the year
            #      only when the user didn't supply one (e.g. "5/27" without a
            #      year defaults to "the most recent past 5/27") and rolls
            #      the end date forward if it lands before the start.
            # Precedence: ISO flags > positional CLI args. Mixing the two on
            # the same command is a usage error (two conflicting intents).
            iso_start = settings.start_iso_datetime
            iso_end = settings.end_iso_datetime
            has_iso = bool(iso_start or iso_end)
            # Check the *raw args namespace* (what the user typed on this
            # invocation) rather than the merged `settings`, so config-file
            # values don't trip the mixing guard.
            has_positional_cli_time = bool(
                getattr(args, 'date', None)
                or getattr(args, 'start', None)
                or getattr(args, 'end', None)
            )

            if has_iso and (bool(iso_start) ^ bool(iso_end)):
                reporter.error(
                    "ConfigError",
                    "--start-iso-datetime and --end-iso-datetime must be provided together.",
                )
                exit(1)
            if has_iso and has_positional_cli_time:
                reporter.error(
                    "ConfigError",
                    (
                        "Cannot combine --start-iso-datetime/--end-iso-datetime "
                        "with the positional date/start/end arguments on the "
                        "same command. Pick one form."
                    ),
                )
                exit(1)

            if has_iso:
                try:
                    start = datetime.fromisoformat(iso_start)
                    end = datetime.fromisoformat(iso_end)
                except ValueError as exc:
                    reporter.error(
                        "ConfigError",
                        f"Invalid ISO 8601 datetime: {exc}",
                    )
                    exit(1)
                if end < start:
                    reporter.error(
                        "ConfigError",
                        "--end-iso-datetime must be at or after --start-iso-datetime.",
                    )
                    exit(1)
            else:
                if not (settings.date and settings.start_time and settings.end_time):
                    reporter.error(
                        "ConfigError",
                        (
                            "extract needs a time range. Provide the positional "
                            "date, start, and end (e.g. `5/27 6pm 7pm`), or the "
                            "--start-iso-datetime / --end-iso-datetime pair."
                        ),
                    )
                    exit(1)
                try:
                    start, end = convert_input_to_datetime(
                        settings.date, settings.start_time, settings.end_time
                    )
                except (ValueError, OverflowError) as exc:
                    reporter.error(
                        "ConfigError",
                        f"Could not parse the date/time range: {exc}",
                    )
                    exit(1)

            # If the user didn't pass -o, build the canonical default output
            # stem from the same shared helper the web service uses. Without
            # this the exacqvision server picks the filename via
            # Content-Disposition, which is unpredictable and doesn't sort by
            # date the way our convention does.
            #
            # Mutating settings here (rather than threading a local
            # through) keeps the rest of the extract pipeline -- which
            # already reads settings.output_filename in multiple places
            # -- working without modification.
            if not settings.output_filename:
                settings.output_filename = default_output_stem(
                    start,
                    settings.server,
                    settings.camera_alias,
                    settings.timelapse_multiplier,
                )

            # Normalize --output-dir into an absolute Path. Resolving early
            # means subsequent path math (the intermediate cleanup at the
            # end of the pipeline) works regardless of whether the caller
            # passed a relative or absolute argument, or whether the CWD
            # changes mid-run for any reason.
            extract_output_dir: "Path | None" = None
            if settings.output_dir:
                extract_output_dir = Path(settings.output_dir).resolve()
                extract_output_dir.mkdir(parents=True, exist_ok=True)

            # Instantiate api class and retrieve video. The constructor logs
            # in, so keep it inside the try where connection/auth failures are
            # reported cleanly instead of surfacing as a raw traceback.
            exapi = None
            try:
                exapi = Exacqvision(settings.server_ip, settings.username, settings.password, timezone)
                # When `extract_output_dir` is set, the raw download lands
                # in that directory; `process_video` and `compress_video`
                # both default to writing next to their input, so the
                # whole pipeline naturally flows into the same directory
                # without any further threading.
                extracted_video_name = exapi.get_video(
                    settings.camera_id,
                    start,
                    end,
                    video_filename=settings.output_filename,
                    output_dir=extract_output_dir,
                )
                # The auth token can expire during a long download, so we
                # re-authenticate with a fresh client for the timestamp
                # query. Close the first session before swapping the
                # reference so it is never leaked.
                try:
                    exapi.logout()
                except Exception:
                    pass
                exapi = Exacqvision(settings.server_ip, settings.username, settings.password, timezone)
                video_timestamps = exapi.get_timestamps(settings.camera_id, start, end)
            except (ExacqvisionError, RequestException) as e:
                reporter.error(
                    "ExacqvisionError",
                    (
                        f"Failed to get video. Make sure selected camera: "
                        f"{settings.camera_alias} is part of selected server: "
                        f"{settings.server}. {e}"
                    ),
                )
                exit(1)
            finally:
                # Guarded so a logout network error can't mask the real
                # outcome (including the exit(1) above). `exapi` may still be
                # None if the very first login failed.
                if exapi is not None:
                    try:
                        exapi.logout()
                    except Exception:
                        pass

            processed_video_path = process_video(extracted_video_name, timestamps=video_timestamps)
            final_path = compress_video(processed_video_path)

            # When --output-dir is set, collapse the three pipeline files
            # down to a single deliverable: delete the raw download and
            # the timelapsed intermediate, then rename the compressed
            # file to a bare `{stem}.mp4`. This is the programmatic-
            # delivery contract -- the web service relies on this so it
            # can spawn the CLI with --output-dir=<exports/> and treat
            # the resulting file as the finished artifact, with no
            # follow-up move/cleanup work.
            if extract_output_dir is not None:
                final_path = _finalize_extract_output_dir(
                    extract_output_dir,
                    output_stem=settings.output_filename,
                    multiplier=settings.timelapse_multiplier,
                    compressed_path=Path(final_path),
                )

            # Embed provenance metadata into the final mp4 so the camera
            # alias, server, time range, multiplier, and caption travel
            # with the file even if the config file is later renamed or
            # rewritten. The web service reads this back to populate the
            # file-browser's "Camera" column, replacing the brittle
            # filename-parsing fallback. Done as a final step, AFTER any
            # rename, so the metadata lands in the user-facing file.
            _embed_extract_metadata(
                Path(final_path),
                {
                    "server": settings.server,
                    "camera_alias": settings.camera_alias,
                    "camera_id": settings.camera_id,
                    "multiplier": settings.timelapse_multiplier,
                    "start_iso": start.isoformat(),
                    "end_iso": end.isoformat(),
                    "timezone": settings.timezone,
                    "caption": settings.caption,
                },
            )

            reporter.done(output=final_path)

        elif args.command == 'crop':
            # Standalone crop-dimension capture: pull a short recent clip,
            # open the ROI selector on its first frame, print the chosen
            # dimensions. No timelapse / compress / metadata / output file.

            # Pre-flight resolution checks -- fail fast with a clear message
            # before any network I/O, reusing the same error taxonomy as
            # extract. `Settings` resolves server/camera from CLI args.
            if not settings.server:
                reporter.error(
                    "ConfigError",
                    "No server selected. Pass --server <name> (must match a top-level [<server>] table in the config).",
                )
                exit(1)
            if not settings.server_ip:
                reporter.error(
                    "ConfigError",
                    f'Server "{settings.server}" is not defined as a top-level [<server>] table in the config.',
                )
                exit(1)
            if not settings.camera_id:
                reporter.error(
                    "ConfigError",
                    (
                        f'Camera alias "{settings.camera_alias}" is not defined as a '
                        f'[{settings.server}.{settings.camera_alias}] table in the config.'
                    ),
                )
                exit(1)

            timezone = ZoneInfo(settings.timezone)
            lookback_arg = getattr(args, 'lookback_minutes', 15)
            lookback = lookback_arg if lookback_arg and lookback_arg > 0 else 15

            # End at "now", look back a modest window for *recorded footage*.
            # We only need one frame; the camera framing is static, so any
            # recent frame works.
            end = datetime.now()
            start = end - timedelta(minutes=lookback)

            reporter.stage(
                "crop_probe",
                f"Searching the last {lookback} min for footage from {settings.camera_alias}",
            )

            # Probe clip lives in a project-local scratch dir that's cleaned
            # automatically -- never the system temp dir, so nothing lands
            # outside the project. It exists only to hand one frame to the selector.
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="exacqman_crop_", dir=TEMP_DIR) as tmp_dir:
                # Construct inside the try so a login/connection failure
                # surfaces as the same friendly ExacqvisionError message
                # (the Exacqvision constructor logs in eagerly). RequestException
                # covers a server that's unreachable or refusing connections.
                exapi = None
                try:
                    exapi = Exacqvision(
                        settings.server_ip, settings.username, settings.password, timezone
                    )

                    # Search the window for seconds that actually have footage,
                    # then export just a ~2s slice around the most recent one.
                    # Exporting the entire lookback window would download
                    # minutes (often gigabytes) of video to read a single frame.
                    try:
                        footage_seconds = exapi.get_timestamps(
                            settings.camera_id, start, end
                        )
                    except (ExacqvisionError, RequestException):
                        raise
                    except Exception:
                        # No videoInfo/clips in the response, etc. -- treat as
                        # "no footage" rather than leaking a parse traceback.
                        footage_seconds = []

                    if not footage_seconds:
                        reporter.error(
                            "ExacqvisionError",
                            (
                                f"No footage was available for {settings.camera_alias} in the "
                                f"last {lookback} minutes. This camera may be motion-triggered "
                                f"with no recent activity -- retry with a larger "
                                f"--lookback-minutes (e.g. --lookback-minutes {lookback * 4})."
                            ),
                        )
                        exit(1)

                    probe_start = footage_seconds[-1]
                    probe_end = probe_start + timedelta(seconds=CROP_PROBE_SECONDS)
                    probe_path = exapi.get_video(
                        settings.camera_id,
                        probe_start,
                        probe_end,
                        video_filename="_crop_probe",
                        output_dir=Path(tmp_dir),
                    )
                except SystemExit:
                    raise
                except (ExacqvisionError, RequestException) as e:
                    reporter.error(
                        "ExacqvisionError",
                        (
                            f"Failed to get a probe clip from camera "
                            f"{settings.camera_alias} on server {settings.server}. "
                            f"Check the server is reachable and the alias is correct. {e}"
                        ),
                    )
                    exit(1)
                finally:
                    if exapi is not None:
                        exapi.logout()

                vid = cv2.VideoCapture(probe_path)
                success, frame = (vid.read() if vid.isOpened() else (False, None))
                vid.release()

                if not success or frame is None:
                    reporter.error(
                        "ExacqvisionError",
                        (
                            f"Found footage for {settings.camera_alias} but couldn't decode a "
                            f"frame from the probe clip. Try again, or widen the window with a "
                            f"larger --lookback-minutes (e.g. --lookback-minutes {lookback * 4})."
                        ),
                    )
                    exit(1)

                # The download is done, but the crop path has no following
                # stage to retire its progress bar (unlike extract). Close it
                # now so its leave=False line is cleared -- otherwise the open
                # bar refreshes under every later print and sits on top of the
                # input() prompt, which reads as a frozen "re-download".
                reporter.close()

                coords = select_crop(frame)

            # No reporter.done() here: select_crop already prints the result
            # block, and a trailing "Done." line would be noise for this
            # interactive utility.

            # Offer to write the selection straight into the config. Only when
            # we have a real interactive terminal -- in piped/JSON contexts an
            # input() prompt would hang, and the clipboard/print already covers
            # the manual path. The prompt follows the blank line that closes the
            # "for future use" block above.
            if sys.stdin.isatty():
                (cx, cy), (cw, ch) = coords
                toml_coords = f"[[{cx}, {cy}], [{cw}, {ch}]]"
                config_name = Path(args.config_file).name
                answer = input(
                    f"Automatically add crop_dimensions to "
                    f"{settings.server}.{settings.camera_alias} in {config_name}? (y/n) "
                ).strip().lower()
                if answer in ("y", "yes"):
                    ok, message = _write_crop_to_config(
                        args.config_file,
                        settings.server,
                        settings.camera_alias,
                        toml_coords,
                    )
                    (reporter.info if ok else reporter.warning)(message)

        elif args.command == 'compress':
            final_path = compress_video(settings.input_filename, settings.output_filename)
            reporter.done(output=final_path)

        elif args.command == 'timelapse':
            final_path = process_video(settings.input_filename, settings.output_filename)
            reporter.done(output=final_path)

    except SystemExit:
        # exit() was called intentionally; let it propagate without an extra error event.
        raise
    except Exception as e:
        reporter.error(type(e).__name__, str(e))
        raise
    finally:
        reporter.close()


if __name__ == "__main__":
    
    main()
