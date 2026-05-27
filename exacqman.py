from dataclasses import dataclass
from moviepy import VideoFileClip
from datetime import timedelta, datetime
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as duparse
from zoneinfo import ZoneInfo
from exacqvision import Exacqvision, ExacqvisionError
from exacqman_naming import default_output_stem
from progress import init_reporter, get_reporter
import argparse
import cv2
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class Settings:
    ''' 
    Class that centralizes the settings for the program.
    Default settings can be set in this class. 
    ArgParse and configParser overwrite the settings in this class (argParse > configParser > defaults)
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

    server: str = None                  # Server name (must match a key under [servers])
    server_ip: str = None               # URL of the chosen Exacqvision server
    camera_alias: str = None            # Camera alias (must match a [servers.<server>.cameras.<alias>] entry)
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
        servers_table = config.get('servers', {})
        settings_table = config.get('settings', {})
        runtime = config.get('runtime', {})

        # Build the flat name->url map and the nested cameras-by-server map.
        # Per-camera crop dimensions are normalized to tuple-of-tuples so the
        # rest of the code can keep treating crop_dimensions as a tuple.
        servers_by_name: dict[str, str] = {}
        cameras_by_server: dict[str, dict[str, dict]] = {}
        for srv_name, srv_data in servers_table.items():
            if not isinstance(srv_data, dict):
                continue
            url = srv_data.get('url')
            if isinstance(url, str) and url.strip():
                servers_by_name[srv_name] = url
            cam_map: dict[str, dict] = {}
            for alias, cam_data in srv_data.get('cameras', {}).items():
                if not isinstance(cam_data, dict):
                    continue
                entry: dict = {'id': cam_data.get('id')}
                cam_crop = cam_data.get('crop_dimensions')
                if cam_crop is not None:
                    entry['crop_dimensions'] = tuple(tuple(pt) for pt in cam_crop)
                # TOML keys are always strings; explicit str() is defensive for
                # cases where a caller passes a raw int alias through args.
                cam_map[str(alias)] = entry
            cameras_by_server[srv_name] = cam_map

        # Resolve the active server and camera (args > runtime config > default).
        server = set_value(
            arg_value='server',
            config_value=runtime.get('server'),
            cls_value=cls.server,
        )
        camera_alias = set_value(
            arg_value='camera_alias',
            config_value=runtime.get('camera_alias'),
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
                config_value=runtime.get('crop'),
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
                config_value=runtime.get('caption'),
                cls_value=cls.caption,
            ),

            server=server,
            server_ip=server_ip,
            camera_alias=camera_alias,
            camera_id=camera_id,
            input_filename=set_value(arg_value='video_filename', cls_value=cls.input_filename),
            output_filename=set_value(
                arg_value='output_name',
                config_value=runtime.get('filename'),
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
            date=set_value(
                arg_value='date',
                config_value=runtime.get('date'),
                cls_value=cls.date,
            ),
            start_time=set_value(
                arg_value='start',
                config_value=runtime.get('start_time'),
                cls_value=cls.start_time,
            ),
            end_time=set_value(
                arg_value='end',
                config_value=runtime.get('end_time'),
                cls_value=cls.end_time,
            ),
            # ISO 8601 pair. Flag-only (no config-file source) because
            # encoding an exact instant in a reusable config file would be
            # an anti-pattern -- if you want a recurring time, use the
            # human-friendly `runtime.date` / `runtime.start_time` pair.
            start_iso_datetime=set_value(
                arg_value='start_iso_datetime',
                cls_value=cls.start_iso_datetime,
            ),
            end_iso_datetime=set_value(
                arg_value='end_iso_datetime',
                cls_value=cls.end_iso_datetime,
            ),
        )


def import_config(config_file: str) -> dict:
    """Load a TOML config file and run structural validation.

    On validation failure, emits a `ConfigError` event through the active
    reporter (see `validate_config`) and exits non-zero so callers --
    including the web service that spawns this CLI -- can surface the
    problem to the user.
    """
    try:
        with open(config_file, "rb") as fp:
            config = tomllib.load(fp)
    except (FileNotFoundError, IsADirectoryError) as e:
        get_reporter().error("ConfigError", f"Config file not found: {config_file} ({e})")
        exit(1)
    except tomllib.TOMLDecodeError as e:
        get_reporter().error("ConfigError", f"Config file is not valid TOML: {config_file}: {e}")
        exit(1)

    if not validate_config(config):
        exit(1)

    return config


def resolve_credentials_path(
    config_file: str,
    config: dict,
    cli_path: str | None = None,
) -> Path:
    """Pick which credentials file to load for this run.

    Priority: CLI ``--credentials`` > ``[settings].credentials_file``. There is
    no implicit fallback -- the caller must supply one source or the other so
    the credentials path is always explicit in the config or on the command
    line. Relative paths in ``credentials_file`` resolve from the config
    file's directory; relative ``--credentials`` paths resolve from CWD.

    Emits a ``CredentialsError`` event and exits non-zero if neither source
    is set.
    """
    if cli_path:
        path = Path(cli_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    settings_table = config.get('settings', {}) if config else {}
    configured = (
        settings_table.get('credentials_file')
        if isinstance(settings_table, dict) else None
    )
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = Path(config_file).resolve().parent / path
        return path.resolve()

    get_reporter().error(
        "CredentialsError",
        (
            "No credentials file specified. Set credentials_file in [settings] "
            "of the config file, or pass --credentials on the command line."
        ),
    )
    exit(1)


def import_credentials(credentials_file: str | Path) -> dict:
    """Load and validate a TOML credentials file; return the ``[auth]`` table."""
    credentials_path = Path(credentials_file)
    try:
        with open(credentials_path, 'rb') as fp:
            credentials = tomllib.load(fp)
    except (FileNotFoundError, IsADirectoryError) as e:
        get_reporter().error(
            "CredentialsError",
            f"Credentials file not found: {credentials_path} ({e})",
        )
        exit(1)
    except tomllib.TOMLDecodeError as e:
        get_reporter().error(
            "CredentialsError",
            f"Credentials file is not valid TOML: {credentials_path}: {e}",
        )
        exit(1)

    if not validate_credentials(credentials):
        exit(1)

    return credentials['auth']


def validate_credentials(credentials: dict) -> bool:
    """Validate a parsed credentials dict (expects ``[auth]`` with username/password)."""
    reporter = get_reporter()
    fatal_errors: list[str] = []

    auth = credentials.get('auth')
    if not isinstance(auth, dict):
        fatal_errors.append('[auth] table is missing from credentials file')
    else:
        username = auth.get('username', '')
        if not isinstance(username, str) or not username.strip():
            fatal_errors.append('auth.username is missing or empty')
        password = auth.get('password', '')
        if not isinstance(password, str) or not password.strip():
            fatal_errors.append('auth.password is missing or empty')

    if fatal_errors:
        reporter.error(
            "CredentialsError",
            "Credentials validation failed:\n" + "\n".join(fatal_errors),
        )
        return False
    return True


def _is_valid_crop(crop) -> bool:
    """Return True iff `crop` is shaped like ``[[x, y], [w, h]]`` with ints."""
    if not isinstance(crop, list) or len(crop) != 2:
        return False
    for pair in crop:
        if not isinstance(pair, list) or len(pair) != 2:
            return False
        for coord in pair:
            # bool is a subclass of int in Python; reject it explicitly so a
            # stray `true`/`false` in TOML doesn't masquerade as a coordinate.
            if not isinstance(coord, int) or isinstance(coord, bool):
                return False
    return True


def validate_config(config: dict) -> bool:
    """Validate a parsed TOML config dict.

    Fatal problems are accumulated and emitted as a single ``reporter.error``
    event; non-fatal nits are emitted as individual ``reporter.warning``
    events. Both routes flow through the active reporter (TTY-pretty in
    human mode, structured JSON for the web service).

    Args:
        config: dict returned by ``tomllib.load``.

    Returns:
        True if the configuration is structurally valid, False otherwise.
    """
    reporter = get_reporter()
    fatal_errors: list[str] = []
    warnings: list[str] = []

    # Top-level tables must exist before any sub-checks; otherwise downstream
    # code raises confusing AttributeError/KeyError.
    required_tables = ['servers', 'settings']
    for name in required_tables:
        if not isinstance(config.get(name), dict):
            fatal_errors.append(f'[{name}] table is missing from config')

    if fatal_errors:
        reporter.error(
            "ConfigError",
            "Config validation failed:\n" + "\n".join(fatal_errors),
        )
        return False

    # [servers] and nested cameras
    servers_table = config['servers']
    if not servers_table:
        fatal_errors.append('At least one server must be defined under [servers.<name>]')

    for srv_name, srv_data in servers_table.items():
        if '.' in srv_name:
            fatal_errors.append(f'Server name "{srv_name}" must not contain "."')
        if not isinstance(srv_data, dict):
            fatal_errors.append(f'[servers.{srv_name}] must be a table')
            continue

        url = srv_data.get('url', '')
        if not isinstance(url, str) or not url.strip():
            fatal_errors.append(f'[servers.{srv_name}].url is missing or empty')

        cameras_table = srv_data.get('cameras', {})
        if not isinstance(cameras_table, dict):
            fatal_errors.append(f'[servers.{srv_name}.cameras] must be a table')
            continue
        if not cameras_table:
            warnings.append(f'Server "{srv_name}" has no cameras defined')
            continue

        for alias, cam_data in cameras_table.items():
            if '.' in str(alias):
                fatal_errors.append(f'Camera alias "{alias}" must not contain "."')
            if not isinstance(cam_data, dict):
                fatal_errors.append(f'[servers.{srv_name}.cameras.{alias}] must be a table')
                continue

            cam_id = cam_data.get('id')
            if cam_id is None:
                fatal_errors.append(f'[servers.{srv_name}.cameras.{alias}].id is required')
            elif not isinstance(cam_id, int) or isinstance(cam_id, bool) or cam_id <= 0:
                fatal_errors.append(
                    f'[servers.{srv_name}.cameras.{alias}].id must be a positive integer'
                )

            cam_crop = cam_data.get('crop_dimensions')
            if cam_crop is not None and not _is_valid_crop(cam_crop):
                fatal_errors.append(
                    f'[servers.{srv_name}.cameras.{alias}].crop_dimensions must be '
                    '[[x, y], [w, h]] with integer values'
                )

    # [settings]
    settings_table = config['settings']

    timezone = settings_table.get('timezone')
    if not isinstance(timezone, str) or not timezone.strip():
        fatal_errors.append('settings.timezone is missing or empty')

    multiplier = settings_table.get('timelapse_multiplier')
    if multiplier is None:
        warnings.append('settings.timelapse_multiplier is missing. Program will default to 10')
    elif not isinstance(multiplier, int) or isinstance(multiplier, bool) or multiplier <= 0:
        fatal_errors.append('settings.timelapse_multiplier must be a positive integer')

    compression_level = settings_table.get('compression_level')
    if compression_level is None or (isinstance(compression_level, str) and not compression_level.strip()):
        warnings.append('settings.compression_level is missing. Program will default to medium')
    elif compression_level not in ('low', 'medium', 'high'):
        fatal_errors.append("settings.compression_level must be one of: 'low', 'medium', 'high'")

    font_weight = settings_table.get('font_weight')
    if font_weight is None:
        warnings.append('settings.font_weight is missing. Program will default to 2')
    elif not isinstance(font_weight, int) or isinstance(font_weight, bool) or font_weight <= 0:
        fatal_errors.append('settings.font_weight must be a positive integer')

    default_crop = settings_table.get('default_crop_dimensions')
    if default_crop is not None and not _is_valid_crop(default_crop):
        fatal_errors.append(
            'settings.default_crop_dimensions must be [[x, y], [w, h]] with integer values'
        )

    # [runtime] is optional, but when present, cross-check its server/camera
    # references against the declared servers/cameras above.
    runtime = config.get('runtime', {})
    if isinstance(runtime, dict):
        runtime_server = runtime.get('server')
        if runtime_server is not None and runtime_server not in servers_table:
            fatal_errors.append(
                f'runtime.server "{runtime_server}" is not defined under [servers]'
            )
        else:
            runtime_alias = runtime.get('camera_alias')
            if runtime_server and runtime_alias is not None:
                srv_cameras = (
                    servers_table.get(runtime_server, {}).get('cameras', {})
                    if isinstance(servers_table.get(runtime_server), dict) else {}
                )
                if str(runtime_alias) not in {str(k) for k in srv_cameras.keys()}:
                    fatal_errors.append(
                        f'runtime.camera_alias "{runtime_alias}" is not defined under '
                        f'[servers.{runtime_server}.cameras]'
                    )

        if 'crop' in runtime and not isinstance(runtime['crop'], bool):
            fatal_errors.append(
                f'runtime.crop must be a boolean (got {type(runtime["crop"]).__name__})'
            )

    credentials_file = settings_table.get('credentials_file')
    if credentials_file is not None and (
        not isinstance(credentials_file, str) or not credentials_file.strip()
    ):
        fatal_errors.append('settings.credentials_file must be a non-empty string')

    for warning in warnings:
        reporter.warning(warning)

    if fatal_errors:
        reporter.error(
            "ConfigError",
            "Config validation failed:\n" + "\n".join(fatal_errors),
        )
        return False
    return True


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


    def select_crop(frame) -> tuple[tuple[int,int], tuple[int,int]]:
        # Create a window to get screen dimensions
        window_name = "Select ROI"
        
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        temp_width, temp_height = (960,540)
        cv2.resizeWindow(window_name, temp_width, temp_height)  # Temporary window size

        # Resize frame to fit screen dimensions
        resized_frame, scale = fit_to_screen(frame, window_name, temp_width, temp_height)

        instructions = "Click and drag to select desired region, then press Enter."

        # Replace 'first_frame' with frame with instructions
        frame_with_text = resized_frame.copy()
        text_size = cv2.getTextSize(instructions, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0]
        text_x = (frame_with_text.shape[1] - text_size[0]) // 2
        text_y = 30  # Position at the top of the frame
        cv2.putText(frame_with_text, instructions, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)

        # Show the resized frame and allow ROI selection
        roi = cv2.selectROI(window_name, frame_with_text, showCrosshair=True, fromCenter=False)
        cv2.destroyAllWindows()  # Close the ROI selection window

        # Scale ROI coordinates back to original resolution
        x, y, w, h = map(int, roi)
        x = int(x / scale)
        y = int(y / scale)
        w = int(w / scale)
        h = int(h / scale)

        coords = ((x, y), (w, h))
        # Render the same value in TOML array syntax so users can paste it
        # straight into either [settings].default_crop_dimensions or a
        # [servers.<server>.cameras.<alias>].crop_dimensions entry.
        toml_coords = f"[[{x}, {y}], [{w}, {h}]]"
        reporter.info(f"Crop coordinates selected: {coords}", crop_dimensions=coords)
        reporter.info(
            "For future use, copy one of these lines into your config file:\n"
            f"  default_crop_dimensions = {toml_coords}   # under [settings]\n"
            f"  crop_dimensions = {toml_coords}            # under [servers.<srv>.cameras.<alias>]"
        )
        return coords


    def calculate_font_scale(video_width: int) -> float:
        # Static timestamp to calculate scale
        timestamp_string = datetime(2025, 3, 28, 6, 43, 20).strftime('%Y-%m-%d %H:%M:%S')

        # Calculate available width for the text (80% of the video width)
        max_text_width = int(video_width * 0.8)

        # Dynamically determine font scale based on text width
        text_size = cv2.getTextSize(timestamp_string, cv2.FONT_HERSHEY_SIMPLEX, 1, settings.font_weight)[0]
        text_width, text_height = text_size

        font_scale = max_text_width / text_width
        
        return font_scale


    def calculate_xy_text_position(video_height: int, video_width: int, timestamp_string: str, font_scale: float) -> tuple[int]:
        # Recalculate text size with the dynamic font scale
        text_size = cv2.getTextSize(timestamp_string, cv2.FONT_HERSHEY_SIMPLEX, font_scale, settings.font_weight)[0]
        text_width, text_height = text_size

        # Calculate position: centered horizontally, with 10% margin at the bottom
        x_position = (video_width - text_width) // 2  # Center horizontally
        y_position = int(video_height - (video_height * 0.1))  # 10% margin from the bottom

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

    font_scale = calculate_font_scale(crop_width)

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
            current_timestamp = timestamps[int(frame_position / total_frames * (number_of_timestamps - 1))]
            timestamp_string = current_timestamp.strftime('%Y-%m-%d %H:%M:%S')
            x_pos, y_pos = calculate_xy_text_position(crop_height, crop_width, timestamp_string, font_scale)
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
    extract_parser.add_argument('camera_alias', nargs='?', default=None, type=str, help='Name of camera wanted')
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
    extract_parser.add_argument('--server', type=str, help='Server name (must match a key under [servers] in the config file)')
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
    extract_parser.add_argument('-c', '--crop', action='store_true', default=None, help='Crop the video. Can also be set via [runtime].crop in the config file. Uses per-camera crop_dimensions, falling back to default_crop_dimensions; prompts if neither is set.')
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
    timelapse_parser.add_argument('-c', '--crop', action='store_true', default=None, help='Crop the video. Can also be set via [runtime].crop in the config file. Uses per-camera crop_dimensions, falling back to default_crop_dimensions; prompts if neither is set.')
    timelapse_parser.add_argument('--caption', type=str, help=f'Add caption below timestamp (max of {Settings.caption_limit} chars)')

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
    # applies uniformly regardless of source (CLI --caption or [Settings] caption).
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
            timezone = ZoneInfo(settings.timezone)

            # Two ways to specify the time range:
            #   1. ISO 8601 flags (--start-iso-datetime / --end-iso-datetime)
            #      -- the programmatic form. Unambiguous: no year/day
            #      fixups, full precision down to seconds, optional
            #      timezone offset.
            #   2. Positional date + start + end (or runtime.{date,start_time,
            #      end_time} in the config) -- the human form. Goes through
            #      `convert_input_to_datetime`, which infers the year only
            #      when the user didn't supply one (e.g. "5/27" without a
            #      year defaults to "the most recent past 5/27") and rolls
            #      the end date forward if it lands before the start.
            # Precedence: ISO flags > positional CLI args > config-file
            # [runtime] values. Mixing ISO flags with positional CLI args
            # is a usage error (the user gave two conflicting intents on
            # the same command line); silently overriding [runtime] config-
            # file defaults with ISO flags is fine -- that's just normal
            # CLI-beats-config precedence, and avoids forcing users to
            # strip leftover `runtime.date`/etc. from configs that are
            # otherwise reusable.
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
                start, end = convert_input_to_datetime(
                    settings.date, settings.start_time, settings.end_time
                )

            # If the user didn't pass -o (and `runtime.filename` wasn't
            # set in the config either), build the canonical default
            # output stem from the same shared helper the web service
            # uses. Without this the exacqvision server picks the
            # filename via Content-Disposition, which is unpredictable
            # and doesn't sort by date the way our convention does.
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

            # Instantiate api class and retrieve video
            exapi = Exacqvision(settings.server_ip, settings.username, settings.password, timezone)

            try:
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
                exapi = Exacqvision(settings.server_ip, settings.username, settings.password, timezone)  # Reinstantiated object because of auth token timeout.
                video_timestamps = exapi.get_timestamps(settings.camera_id, start, end)
            except ExacqvisionError as e:
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
                exapi.logout()

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
