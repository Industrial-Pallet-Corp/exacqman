"""
Configuration Service

Reads ExacqMan TOML configuration files for the web application.

The CLI tool (``exacqman.py``) consumes the same config files, so this module
mirrors its structure expectations: ``[settings]`` is the reserved top-level
table, and every other top-level table is a server. A
server table holds a scalar ``url`` and dict-valued camera sub-tables
(``[<server>.<alias>]``). Authentication is loaded from a separate credentials
file by the CLI (see ``sample.credentials``).

This module deliberately re-implements the tiny server/camera split rule rather
than importing the root ``exacqman_config`` helper, since the web backend runs
as its own package; the rule below must stay in lockstep with
``exacqman_config.split_servers_and_cameras``.
"""

import logging
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

from api.models import CameraInfo, ConfigInfo

logger = logging.getLogger(__name__)

# Top-level tables that are NOT servers (mirror of
# exacqman_config.RESERVED_TABLES).
RESERVED_TABLES = frozenset({"settings"})


def _iter_server_tables(config: dict):
    """Yield ``(server_name, server_table)`` for each non-reserved top-level table."""
    for name, table in (config or {}).items():
        if name in RESERVED_TABLES or not isinstance(table, dict):
            continue
        yield name, table


class ConfigService:
    """Service for managing ExacqMan configuration files."""

    def __init__(self):
        self.working_directory = Path(__file__).parent.parent.parent.parent  # ExacqMan root
        self.timelapse_options = [1, 2, 5, 10, 15, 20, 25, 30, 40, 50]

    # ---- internal helpers ---------------------------------------------------

    def _resolve_path(self, config_file: str) -> Path:
        config_path = Path(config_file)
        if not config_path.is_absolute():
            config_path = self.working_directory / config_file
        return config_path

    def _load(self, config_file: str) -> dict:
        """Read and parse a TOML config file.

        Raises:
            FileNotFoundError: if the file does not exist
            tomllib.TOMLDecodeError: if the file is not valid TOML
        """
        config_path = self._resolve_path(config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_file}")
        with open(config_path, "rb") as fp:
            return tomllib.load(fp)

    # ---- public API ---------------------------------------------------------

    def get_available_cameras(self, config_file: str) -> List[CameraInfo]:
        """Return every camera across every server in the config.

        Each entry carries its owning server name so the UI can group/filter
        by server. ``alias`` and ``id`` keep their original meaning.
        """
        try:
            config = self._load(config_file)

            cameras: List[CameraInfo] = []
            for srv_name, srv_data in _iter_server_tables(config):
                # Dict-valued sub-tables are cameras; the scalar `url` is skipped.
                for alias, cam_data in srv_data.items():
                    if not isinstance(cam_data, dict):
                        continue
                    cam_id = cam_data.get("id")
                    if cam_id is None:
                        continue
                    alias_str = str(alias)
                    cameras.append(CameraInfo(
                        alias=alias_str,
                        id=str(cam_id),
                        server=srv_name,
                    ))

            if not cameras:
                logger.warning("No cameras found in %s", config_file)
            logger.info("Loaded %d cameras from %s", len(cameras), config_file)
            return cameras

        except Exception:
            logger.exception("Error reading cameras from config %s", config_file)
            raise

    def get_available_servers(self, config_file: str) -> Dict[str, str]:
        """Return a flat ``{server_name: url}`` map."""
        try:
            config = self._load(config_file)

            servers: Dict[str, str] = {}
            for srv_name, srv_data in _iter_server_tables(config):
                url = srv_data.get("url")
                if isinstance(url, str) and url.strip():
                    servers[srv_name] = url

            if not servers:
                logger.warning("No servers found in %s", config_file)
            logger.info("Loaded %d servers from %s", len(servers), config_file)
            return servers

        except Exception:
            logger.exception("Error reading servers from config %s", config_file)
            raise

    def get_config_info(self, config_file: str) -> ConfigInfo:
        """Return cameras + servers + timelapse options in one shot."""
        try:
            cameras = self.get_available_cameras(config_file)
            servers = self.get_available_servers(config_file)
            return ConfigInfo(
                cameras=cameras,
                servers=servers,
                timelapse_options=self.timelapse_options,
            )
        except Exception:
            logger.exception("Error getting config info from %s", config_file)
            raise

    def validate_camera(
        self, config_file: str, camera_alias: str, server: Optional[str] = None
    ) -> bool:
        """Return True iff the requested camera exists in the config.

        When ``server`` is given, the ``(server, alias)`` pair must match: with
        per-server camera tables the same alias can live under multiple servers,
        and the extract job resolves the camera by that exact pair (see
        ``exacqman.Settings``). When ``server`` is None we fall back to a global
        alias check.
        """
        try:
            cameras = self.get_available_cameras(config_file)
            if server:
                return any(
                    camera.alias == camera_alias and camera.server == server
                    for camera in cameras
                )
            return any(camera.alias == camera_alias for camera in cameras)
        except Exception:
            logger.exception("Error validating camera %s", camera_alias)
            return False

    def get_camera_id(self, config_file: str, camera_alias: str) -> Optional[str]:
        """Return the camera ID for `camera_alias`.

        With per-server camera tables it's possible for the same alias to
        exist under multiple servers; this lookup returns the first match
        encountered. Callers that need to disambiguate should use
        ``get_available_cameras`` and filter by ``server``.
        """
        try:
            for camera in self.get_available_cameras(config_file):
                if camera.alias == camera_alias:
                    return camera.id
            return None
        except Exception:
            logger.exception("Error getting camera ID for %s", camera_alias)
            return None

    def get_available_config_files(self) -> List[str]:
        """List ``*.config`` files in the ExacqMan working directory."""
        try:
            config_files = [p.name for p in self.working_directory.glob("*.config")]
            logger.info("Found %d configuration files", len(config_files))
            return config_files
        except Exception:
            logger.exception("Error finding config files")
            return []

    def validate_config_file(self, config_file: str) -> bool:
        """Lightweight structural check: file exists, parses as TOML, has a
        ``[settings]`` table and at least one server table. Deeper semantic
        validation lives in the CLI (``exacqman.validate_config``) and runs
        when an extract job actually executes.
        """
        try:
            config = self._load(config_file)
        except FileNotFoundError:
            return False
        except tomllib.TOMLDecodeError as e:
            logger.error("Invalid TOML in %s: %s", config_file, e)
            return False
        except Exception:
            logger.exception("Error validating config file %s", config_file)
            return False

        if not isinstance(config.get("settings"), dict):
            return False
        return any(True for _ in _iter_server_tables(config))
