"""
exacqman_config.py

Shared TOML config + credentials loading for ExacqMan.

Two callers consume this module: the main CLI (``exacqman.py``) and the
``--list-cameras`` utility CLI in ``exacqvision.py``. Both need to parse
the same config + credentials files using identical rules, and surface
validation errors through the same progress reporter. Centralising the
loaders here keeps the two CLIs from drifting in subtle ways (which
fields are required, where credentials live, etc.) and avoids forcing
``exacqvision.py`` to import the heavy ``moviepy`` / ``cv2``
dependencies that ``exacqman.py`` pulls in at module load.

Public surface:

  * ``import_config(config_file)``
  * ``resolve_credentials_path(config_file, config, cli_path=None)``
  * ``import_credentials(credentials_file)``
  * ``validate_config(config)``
  * ``validate_credentials(credentials)``

All five honor the active progress reporter for error / warning events,
which is initialised by the caller before these functions are invoked
(see ``init_reporter`` in ``progress.py``). On any fatal validation
failure each helper calls ``exit(1)`` so the calling CLI exits non-zero
and any orchestrator (e.g. the web service) can surface the failure.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from progress import get_reporter


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
