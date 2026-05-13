from dataclasses import dataclass
from configparser import ConfigParser
from moviepy import VideoFileClip
from datetime import timedelta, datetime
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as duparse
from zoneinfo import ZoneInfo
from ast import literal_eval
from exacqvision import Exacqvision, ExacqvisionError
from progress import init_reporter, get_reporter
import cv2
import argparse


@dataclass
class Settings:
    ''' 
    Class that centralizes the settings for the program.
    Default settings can be set in this class. 
    ArgParse and configParser overwrite the settings in this class (argParse > configParser > defaults)
    '''
    user: str = None
    password: str = None

    servers: dict = None                # Dictionary of servers -> server ip's
    cameras: dict = None                # Dictionary of camera aliases -> camera id's

    timelapse_multiplier: int = 10      # Must be a positive int
    compression_level: str = 'medium'   # Should be 'low', 'medium', or 'high'
    timezone: str = None
    crop: bool = False                  # Does the video need cropped? Crop_dimensions only matter if this is True.
    crop_dimensions: tuple[tuple[int,int],tuple[int,int]] = None # (x,y)(width,height) where (x,y) = top left of rectangle
    font_weight: int = 2                # Font thickness
    caption: str = None                 # Caption above the timestamp
    caption_limit = 40                  # Max number of characters for caption

    server: str = None                  # Server name (Should match to one of the servers in config file under [Network])
    server_ip: str = None               # IP address of the Exacqman server
    camera_alias: str = None            # Camera name (Should match to one of the cameras in config file under [Cameras])
    camera_id: str = None               # Camera Id for Exacqman server
    input_filename: str = None          # Video filename that needs processed
    output_filename: str = None         # Desired name of output file (will always be .mp4)
    date: str = None                    # MM/DD (e.g. '3/11')
    start_time: str = None              # Start time of video (e.g. '6 pm', '6:30pm', '18:30')
    end_time: str = None                # End time of video (e.g. '6 pm', '6:30pm', '18:30')

    @classmethod
    def from_args_and_config(cls, args: argparse.Namespace, config: ConfigParser = ConfigParser()) -> 'Settings':
        """Merge argparse, config file, and defaults in that priority."""
        
        def set_value(arg_value = None, config_value = None, cls_value = None, required = False):
            """
            Sets correct value respecting priority: command-line args > [Runtime] config > defaults
            Args:
                arg_value: Name of command-line argument
                config_value: Value from [Runtime] config section  
                cls_value: Default value from class
                required: If True, raise error if no value found
            """
            # First priority: command-line argument
            if arg_value:
                arg_val = getattr(args, arg_value, None)
                if arg_val is not None and str(arg_val).strip():
                    return arg_val
            
            # Second priority: [Runtime] config value (only if not empty)
            if config_value is not None and str(config_value).strip():
                return config_value
            
            # Third priority: class default
            if cls_value is not None:
                return cls_value
            
            # Required parameter missing
            if required:
                raise ValueError(f"Required parameter {arg_value or 'config'} is missing")
            
            return None

        # Calculate server and camera_alias first for use in server_ip and camera_id
        server = set_value(arg_value='server', config_value=config.get('Runtime', 'server', fallback='') if config.has_section('Runtime') else None, cls_value=cls.server)
        camera_alias = set_value(arg_value='camera_alias', config_value=config.get('Runtime','camera_alias',fallback='') if config.has_section('Runtime') else None, cls_value=cls.camera_alias)

        # Build settings with priority: args > config > default
        return cls(
            # User, password, server_ip, and cameras are exclusively from the config file so there is no set_value call.
            user=config.get('Auth','user',fallback=''),
            password=config.get('Auth','password',fallback=''),
            cameras=config['Cameras'] if 'Cameras' in config else None,

            timelapse_multiplier=int(set_value(arg_value='multiplier', config_value=config.get('Settings','timelapse_multiplier',fallback=''), cls_value=cls.timelapse_multiplier)),
            compression_level=set_value(arg_value='quality', config_value=config.get('Settings','compression_level',fallback=''), cls_value=cls.compression_level),
            timezone=set_value(config_value=config.get('Settings', 'timezone', fallback=''),cls_value=cls.timezone),
            crop=bool(set_value(arg_value='crop', cls_value=cls.crop)),
            crop_dimensions=literal_eval(config.get('Settings','crop_dimensions',fallback='')) if config.get('Settings', 'crop_dimensions', fallback='') else None,
            font_weight=int(set_value(config_value=config.get('Settings','font_weight',fallback=''), cls_value=cls.font_weight)),
            caption=set_value(arg_value='caption', config_value=config.get('Settings', 'caption', fallback='').upper(),cls_value=cls.caption),

            server=server,
            server_ip=config['Network'].get(server) if 'Network' in config and server else None,
            camera_alias=camera_alias,
            camera_id=config['Cameras'].get(camera_alias) if 'Cameras' in config and camera_alias else None,
            input_filename=set_value(arg_value='video_filename', cls_value=cls.input_filename),
            output_filename=set_value(arg_value='output_name', config_value=config.get('Runtime','filename',fallback='') if config.has_section('Runtime') else None, cls_value=cls.output_filename),
            date=set_value(arg_value='date', config_value=config.get('Runtime','date',fallback='') if config.has_section('Runtime') else None, cls_value=cls.date),
            start_time=set_value(arg_value='start', config_value=config.get('Runtime','start_time',fallback='') if config.has_section('Runtime') else None, cls_value=cls.start_time),
            end_time=set_value(arg_value='end', config_value=config.get('Runtime','end_time',fallback='') if config.has_section('Runtime') else None, cls_value=cls.end_time)
        )


def import_config(config_file: str) -> ConfigParser:
    config = ConfigParser()
    config.read(config_file)

    if validate_config(config) == False:
        exit(1)

    return config


def validate_config(config: ConfigParser) -> bool:
    """
    Validates the configuration file for required sections and values.

    Fatal validation problems are emitted as a single `reporter.error` event
    (so they surface to humans on the CLI and to programmatic consumers like
    the web service through the JSON event stream). Non-fatal problems are
    emitted as individual `reporter.warning` events.

    Args:
        config (ConfigParser): Parsed configuration object.

    Returns:
        bool: True if the configuration is valid, False otherwise.
    """
    reporter = get_reporter()
    fatal_errors: list[str] = []
    warnings: list[str] = []

    # Check Sections first
    sections = ['Auth', 'Network', 'Cameras', 'Settings']

    for section in sections:
        if not config.has_section(section):
            fatal_errors.append(f'[{section}] section is missing from config')

    # If any required section is missing, downstream checks would crash; bail
    # now and surface the section-level errors.
    if fatal_errors:
        reporter.error(
            "ConfigError",
            "Config validation failed:\n" + "\n".join(fatal_errors),
        )
        return False

    # Validate entries individually
    if 'user' not in config['Auth'] or not config['Auth']['user'].strip():
        fatal_errors.append('user is missing or empty')

    if 'password' not in config['Auth'] or not config['Auth']['password'].strip():
        fatal_errors.append('password is missing or empty')

    for server_name, server_ip in config['Network'].items():
        if not server_ip.strip():
            fatal_errors.append(f'Server: {server_name} has no server_ip')

    if 'timezone' not in config['Settings'] or not config['Settings']['timezone'].strip():
        fatal_errors.append('timezone is missing or empty')

    if 'timelapse_multiplier' not in config['Settings'] or not config['Settings']['timelapse_multiplier'].strip():
        warnings.append('timelapse_multiplier is missing or empty. Program will default to 10')
    else:
        try:
            if int(config['Settings']['timelapse_multiplier']) <= 0:
                fatal_errors.append('timelapse_multiplier must be a positive integer')
        except ValueError:
            fatal_errors.append('timelapse_multiplier must be a positive integer')

    if 'compression_level' not in config['Settings'] or not config['Settings']['compression_level'].strip():
        warnings.append('compression_level is missing or empty. Program will default to medium')

    crop_dimensions = config['Settings'].get('crop_dimensions', '').strip()
    if crop_dimensions:
        try:
            crop_dimensions = literal_eval(crop_dimensions)
            # Check if all values are integers
            if not all(isinstance(coord, int) for point in crop_dimensions for coord in point):
                warnings.append('crop_dimensions should contain integers only')
        except ValueError:
            warnings.append('crop_dimensions should follow the format: ((x, y), (width, height))')

    if 'font_weight' not in config['Settings'] or not config['Settings']['font_weight'].strip():
        warnings.append('font_weight is missing or empty. Program will default to 2')
    else:
        try:
            if int(config['Settings']['font_weight']) <= 0:
                fatal_errors.append('font_weight must be a positive integer')
        except ValueError:
            fatal_errors.append('font_weight must be a positive integer')

    if 'caption' not in config['Settings']:
        warnings.append('caption is missing from Settings header.')
    else:
        if len(config['Settings']['caption']) > Settings.caption_limit:
            fatal_errors.append(f'Caption character limit of {Settings.caption_limit} exceeded.')

    # Only validate Runtime section if it exists
    if config.has_section('Runtime') and 'server' in config['Runtime']:
        server = config['Runtime']['server']
        if server not in config['Network']:
            fatal_errors.append(f'Server {server} not found in the Network list')

    for camera_number, camera_value in config['Cameras'].items():
        if not camera_value.strip():
            fatal_errors.append(f'Camera {camera_number} has no id')
        else:
            try:
                int(camera_value)
            except ValueError:
                fatal_errors.append(f'Camera ID {camera_number} must be an integer')

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

        coords = ((x,y),(w,h))
        reporter.info(f"Crop coordinates selected: {coords}", crop_dimensions=coords)
        reporter.info(
            f"For future use: copy this into config file under [Settings]: "
            f"crop_dimensions = {coords}"
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
            caption_font_scale = font_scale*0.8
            caption_x, caption_y = calculate_xy_text_position(crop_height*.85, crop_width, settings.caption, caption_font_scale)
            cv2.putText(finished_frame, settings.caption, (caption_x, caption_y), cv2.FONT_HERSHEY_SIMPLEX, caption_font_scale, (255, 255, 255), settings.font_weight, cv2.LINE_AA)

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
    extract_parser.add_argument('config_file', type=str, help='Filepath of local config file')
    extract_parser.add_argument('--server', type=str, help='Server location initials ("Clark Hill" = "ch")')
    extract_parser.add_argument('-o', '--output_name', type=str, help='Desired filepath')
    extract_parser.add_argument('--quality', type=str, choices=['low', 'medium', 'high'], help='Desired video quality')
    extract_parser.add_argument('--multiplier', type=int, help='Desired timelapse multiplier (must be a positive integer)')
    extract_parser.add_argument('-c', '--crop', action='store_true', help='Crop the video. Set by config file or query user.')
    extract_parser.add_argument('--caption', type=str, help='Add caption above timestamp (max of 40 chars)')

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
    timelapse_parser.add_argument('-c', '--crop', action='store_true', help='Crop the video. Set by config file or query user.')
    timelapse_parser.add_argument('--caption', type=str, help='Add caption above timestamp (max of 40 chars)')

    return arg_parser.parse_args()


def convert_input_to_datetime(date:str, start:str, end:str) -> tuple[datetime, datetime]:
    """
    Converts date and time strings to datetime objects for video extraction.

    Args:
        date (str): Date in MM/DD format (e.g., '3/11').
        start (str): Start time (e.g., '6 pm', '18:30').
        end (str): End time (e.g., '6 pm', '18:30').

    Returns:
        tuple[datetime, datetime]: Start and end datetime objects, adjusted for year and day if needed.
    """
    
    start_datetime = duparse(f'{date} {start}')
    end_datetime = duparse(f'{date} {end}')

    # Adjust the date's year from the current year to the previous if the date hasn't happened yet.
    if start_datetime > datetime.now():
        start_datetime = start_datetime - relativedelta(years=1)
        end_datetime = end_datetime - relativedelta(years=1)

    # Adjust the end timestamp date to the following day if the end time occurs earlier than the start time.
    if end_datetime < start_datetime :
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

    # If config file is specified in args, then read the config.
    config_file = getattr(args, 'config_file', None)
    if config_file:
        config = import_config(config_file)

    settings = Settings.from_args_and_config(args, config) if config else Settings.from_args_and_config(args)

    try:
        if args.command == 'extract':

            cameras = settings.cameras
            timezone = ZoneInfo(settings.timezone)

            start, end = convert_input_to_datetime(settings.date, settings.start_time, settings.end_time)

            # Instantiate api class and retrieve video
            exapi = Exacqvision(settings.server_ip, settings.user, settings.password, timezone)

            try:
                extracted_video_name = exapi.get_video(settings.camera_id, start, end, video_filename=settings.output_filename)
                exapi = Exacqvision(settings.server_ip, settings.user, settings.password, timezone)  # Reinstantiated object because of auth token timeout.
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
