"""
exacqman_config.py

Shared TOML config + credentials loading for ExacqMan.

Several callers consume this module: the main CLI (``cli.py``, including its
``list-cameras`` subcommand) and the inspection helpers in ``inspect.py``.
They all need to parse the same config + credentials files using identical
rules, and surface validation errors through the same progress reporter.
Centralising the loaders here keeps the callers from drifting in subtle ways
(which fields are required, where credentials live, etc.) and avoids forcing
lightweight consumers to import the heavy ``moviepy`` / ``cv2`` dependencies
that ``cli.py`` pulls in at module load.

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

from exacqman.progress import get_reporter


# Top-level tables that are NOT servers. Every other top-level table in the
# config is treated as a server (see split_servers_and_cameras).
RESERVED_TABLES = frozenset({"settings"})

# Accepted compression values, for both the global [settings].default_compression_level
# and the per-camera [<server>.<alias>].compression_level override. 'none' bypasses
# the compression/re-encode step entirely (keeps native, post-crop quality).
VALID_COMPRESSION_LEVELS = ('none', 'low', 'medium', 'high')


def split_servers_and_cameras(config: dict) -> tuple[dict[str, str], dict[str, dict]]:
    """Split a parsed config into its server-URL map and cameras-by-server map.

    Schema (flat form): each top-level table other than ``[settings]`` is a
    server. Inside a server table, the scalar ``url`` key is the server URL and
    every *dict-valued* sub-table (``[<server>.<alias>]``) is a camera, keyed by
    alias with an ``id`` and optional ``crop_dimensions``.

    Returns:
        ``(servers_by_name, cameras_by_server)`` where ``servers_by_name`` is
        ``{name: url}`` (only servers that declare a non-empty url) and
        ``cameras_by_server`` is ``{name: {alias: cam_dict}}`` for every server
        table encountered. This is the single source of truth for the schema;
        all CLI consumers go through it so they can't drift.
    """
    servers_by_name: dict[str, str] = {}
    cameras_by_server: dict[str, dict] = {}
    for name, table in (config or {}).items():
        if name in RESERVED_TABLES or not isinstance(table, dict):
            continue
        url = table.get("url")
        if isinstance(url, str) and url.strip():
            servers_by_name[name] = url
        # Dict-valued sub-tables are cameras; the scalar `url` is skipped.
        cameras_by_server[name] = {
            str(alias): cam
            for alias, cam in table.items()
            if isinstance(cam, dict)
        }
    return servers_by_name, cameras_by_server


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

    # [settings] must exist before any sub-checks; otherwise downstream code
    # raises confusing AttributeError/KeyError.
    if not isinstance(config.get('settings'), dict):
        reporter.error(
            "ConfigError",
            "Config validation failed:\n[settings] table is missing from config",
        )
        return False

    # Servers and their cameras. Each top-level table other than the reserved
    # [settings] is a server, with a scalar `url` and dict-valued
    # camera sub-tables ([<server>.<alias>]). split_servers_and_cameras() is the
    # single source of truth for that mapping; here we re-walk the raw tables so
    # we can report shape problems (bad url, bad id, reserved name) precisely.
    _, cameras_by_server = split_servers_and_cameras(config)
    if not cameras_by_server:
        fatal_errors.append('At least one server must be defined as a top-level [<name>] table')

    for srv_name, _table in config.items():
        if srv_name in RESERVED_TABLES or not isinstance(_table, dict):
            continue
        if '.' in srv_name:
            fatal_errors.append(f'Server name "{srv_name}" must not contain "."')

        url = _table.get('url', '')
        if not isinstance(url, str) or not url.strip():
            fatal_errors.append(f'[{srv_name}].url is missing or empty')

        cameras_table = cameras_by_server.get(srv_name, {})
        if not cameras_table:
            warnings.append(f'Server "{srv_name}" has no cameras defined')
            continue

        for alias, cam_data in cameras_table.items():
            if '.' in str(alias):
                fatal_errors.append(f'Camera alias "{alias}" must not contain "."')

            cam_id = cam_data.get('id')
            if cam_id is None:
                fatal_errors.append(f'[{srv_name}.{alias}].id is required')
            elif not isinstance(cam_id, int) or isinstance(cam_id, bool) or cam_id <= 0:
                fatal_errors.append(
                    f'[{srv_name}.{alias}].id must be a positive integer'
                )

            cam_crop = cam_data.get('crop_dimensions')
            if cam_crop is not None and not _is_valid_crop(cam_crop):
                fatal_errors.append(
                    f'[{srv_name}.{alias}].crop_dimensions must be '
                    '[[x, y], [w, h]] with integer values'
                )

            cam_compression = cam_data.get('compression_level')
            if cam_compression is not None and cam_compression not in VALID_COMPRESSION_LEVELS:
                fatal_errors.append(
                    f"[{srv_name}.{alias}].compression_level must be one of: "
                    "'low', 'medium', 'high'"
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

    default_compression_level = settings_table.get('default_compression_level')
    if default_compression_level is None or (isinstance(default_compression_level, str) and not default_compression_level.strip()):
        warnings.append('settings.default_compression_level is missing. Program will default to medium')
    elif default_compression_level not in VALID_COMPRESSION_LEVELS:
        fatal_errors.append("settings.default_compression_level must be one of: 'low', 'medium', 'high'")

    font_weight = settings_table.get('font_weight')
    if font_weight is None:
        warnings.append('settings.font_weight is missing. Program will default to 3')
    elif not isinstance(font_weight, int) or isinstance(font_weight, bool) or not 1 <= font_weight <= 5:
        fatal_errors.append('settings.font_weight must be an integer from 1 (thinnest) to 5 (heaviest)')

    default_crop = settings_table.get('default_crop')
    if default_crop is not None and not isinstance(default_crop, bool):
        fatal_errors.append(
            f'settings.default_crop must be a boolean (got {type(default_crop).__name__})'
        )

    default_crop_dimensions = settings_table.get('default_crop_dimensions')
    if default_crop_dimensions is not None and not _is_valid_crop(default_crop_dimensions):
        fatal_errors.append(
            'settings.default_crop_dimensions must be [[x, y], [w, h]] with integer values'
        )

    credentials_file = settings_table.get('credentials_file')
    if credentials_file is not None and (
        not isinstance(credentials_file, str) or not credentials_file.strip()
    ):
        fatal_errors.append('settings.credentials_file must be a non-empty string')

    # Web-UI port. Optional: when absent, `exacqman-web` falls back to its
    # built-in default. When present it must be a valid TCP port.
    port = settings_table.get('port')
    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
    ):
        fatal_errors.append('settings.port must be an integer between 1 and 65535')

    for warning in warnings:
        reporter.warning(warning)

    if fatal_errors:
        reporter.error(
            "ConfigError",
            "Config validation failed:\n" + "\n".join(fatal_errors),
        )
        return False
    return True
