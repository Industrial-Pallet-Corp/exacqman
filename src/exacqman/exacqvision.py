import argparse
import json
import sys
from pathlib import Path
from time import sleep
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import RequestException

from exacqman.progress import init_reporter, get_reporter


# Request timeouts (seconds). Without these, an unreachable host hangs on the
# OS socket default (often 1+ minute) before failing; bounding them makes both
# the CLI and the web service fail fast and gracefully.
#   * CONNECT_TIMEOUT -- cap on establishing the TCP connection.
#   * READ_TIMEOUT    -- cap on inactivity between received bytes (per-read,
#     not total), so long-but-active streaming downloads are unaffected.
#   * PROBE_TIMEOUT   -- short total budget for the unauthenticated
#     reachability probe.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
PROBE_TIMEOUT = 3
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)


class ExacqvisionError(Exception):
    """Custom exception for Exacqvision API errors."""
    pass


class ExacqvisionTimeoutError(ExacqvisionError):
    """Custom exception for Exacqvision API timeout errors."""
    pass


def probe_server(base_url: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """Check whether an Exacqvision server is reachable on the network.

    This is an unauthenticated, lightweight check: any HTTP response (even an
    error status) means the host is reachable. Only connection-level failures
    (refused, DNS, timeout) count as unreachable. Credentials are intentionally
    not used -- auth problems surface later as a ``CredentialsError`` at extract
    time.

    Args:
        base_url: The server's base URL (the ``url`` field from the config).
        timeout: Total seconds to wait before treating the host as unreachable.

    Returns:
        ``{"reachable": bool, "detail": str}`` where ``detail`` is a short,
        human-readable reason when unreachable (empty when reachable).
    """
    if not base_url:
        return {"reachable": False, "detail": "no URL configured"}

    try:
        # stream=True so we don't download the body; we only care that the
        # server answered at the HTTP layer.
        response = requests.get(base_url, timeout=timeout, stream=True)
        response.close()
        return {"reachable": True, "detail": ""}
    except requests.exceptions.Timeout:
        return {"reachable": False, "detail": f"timed out after {timeout}s"}
    except requests.exceptions.ConnectionError:
        return {"reachable": False, "detail": "connection refused or host unreachable"}
    except RequestException as exc:
        return {"reachable": False, "detail": str(exc)}


def probe_servers(servers: dict, timeout: float = PROBE_TIMEOUT) -> dict:
    """Probe many servers concurrently.

    Args:
        servers: Mapping of ``server_name -> base_url``.
        timeout: Per-server reachability budget (see ``probe_server``).

    Returns:
        Mapping of ``server_name -> {"reachable": bool, "detail": str}``.
    """
    if not servers:
        return {}

    from concurrent.futures import ThreadPoolExecutor

    names = list(servers)
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        results = pool.map(
            lambda name: (name, probe_server(servers[name], timeout)), names
        )
        return {name: result for name, result in results}


class Exacqvision:
    """
    Interface for interacting with the Exacqvision API to manage video exports and camera data.

    Attributes:
        base_url (str): Base URL of the Exacqvision server.
        timezone (ZoneInfo): Timezone for handling timestamps.
        session (str): Session ID for authenticated API calls.
    """
    
    def __init__(self, base_url: str, username: str, password: str, timezone: ZoneInfo):
        self.base_url = base_url
        self.timezone = timezone
        self.session = self.login(username, password)


    def login(self, username: str, password: str) -> str:
        """
        Authenticates with the Exacqvision API and retrieves a session ID.

        Args:
            username (str): Username for authentication.
            password (str): Password for authentication.

        Returns:
            str: Session ID for subsequent API calls.
        """

        url = f"{self.base_url}/v1/login.web"

        payload = f'u={username}&p={password}&responseVersion=2&s=0'
        headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
        }

        try:
            response = requests.request("POST", url, headers=headers, data=payload, timeout=REQUEST_TIMEOUT)
        except RequestException as e:
            raise ExacqvisionError(
                f"Could not reach the Exacqvision server at {self.base_url}: {e}"
            )

        if not response.ok:
            raise ExacqvisionError(
                f"Login to {self.base_url} failed with HTTP {response.status_code}. "
                f"Check the server URL and that the service is reachable."
            )

        try:
            body = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            raise ExacqvisionError(
                f"Login to {self.base_url} returned a non-JSON response; the URL "
                f"may not point to an Exacqvision API."
            )

        session_id = body.get('sessionId')
        if not session_id:
            raise ExacqvisionError(
                f"Login to {self.base_url} did not return a session ID. "
                f"Check the username and password."
            )

        return session_id


    def logout(self):
        '''Logs user out using a valid session_id'''

        if self.session:
            url = f"{self.base_url}/v1/logout.web?s={self.session}"
            response = requests.request("POST", url, timeout=REQUEST_TIMEOUT)
            return response.text
        else:
            print("No active session to logout.")


    def list_cameras(self):
        """
        Retrieves a list of cameras available on the Exacqvision server.

        Returns:
            list: List of camera details.
        """
        url = f"{self.base_url}/v1/config.web?s={self.session}&output=json"

        response = requests.request("GET", url, timeout=REQUEST_TIMEOUT)
        cameras = json.loads(response.text)['Cameras']
        return cameras


    def convert_GMT_to_local(self, time: datetime) -> datetime:
        '''Converts a GMT datetime to the local timezone.'''

        # Parse the input string and assign the timezone in one line
        gmt_datetime = time.replace(tzinfo=ZoneInfo('GMT'))

        # Convert to the local timezone
        local_datetime = gmt_datetime.astimezone(self.timezone)

        return local_datetime


    def convert_local_to_GMT(self, time: datetime) -> datetime:
        '''Converts a local timezone datetime to GMT.'''

        # Parse the input string and assign the timezone in one line
        local_datetime = time.replace(tzinfo=self.timezone)

        # Convert to GMT timezone
        gmt_datetime = local_datetime.astimezone(ZoneInfo('GMT'))

        return gmt_datetime
    

    def convert_datetime_to_iso8601(self, timezone: ZoneInfo, *datetimes: datetime) -> tuple[str]:
        '''Adds timezone data to datetimes, then converts to timestamps in ISO8601 format'''
        return tuple(dt.replace(tzinfo=timezone).isoformat() for dt in datetimes)
    

    def create_search(self, camera_id: int, start: datetime, stop: datetime) -> tuple[str, requests.Response]:
        """
        Creates a search request for video recordings within a time range.

        Args:
            camera_id (int): ID of the camera.
            start (datetime): Start time of the search.
            stop (datetime): End time of the search.

        Returns:
            tuple[str, requests.Response]: Search ID and the API response.

        Raises:
            ExacqvisionError: If the search request fails.
        """

        # Convert datetimes into timestamps
        start, stop = self.convert_datetime_to_iso8601(self.timezone, start, stop)

        url = f"{self.base_url}/v1/search.web?s={self.session}&start={start}&end={stop}&camera={camera_id}&output=json"

        try:
            response = requests.request("GET", url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            # `search_id` is absent when the search produced no results (e.g. a
            # window with no recorded footage). Don't treat that as a hard error
            # -- callers (get_timestamps) inspect `videoInfo` for clips and can
            # decide what an empty result means. Only genuine HTTP / decode
            # failures should raise here.
            search_id = json.loads(response.text).get('search_id')
            return search_id, response
        except (RequestException, ValueError) as e:
            raise ExacqvisionError(f"Search request failed: {str(e)}")


    def export_request(self, camera_id: int, start: datetime, stop: datetime, name: str = None) -> str:
        """
        Initiates a video export request.

        Args:
            camera_id (int): ID of the camera.
            start (datetime): Start time of the video.
            stop (datetime): End time of the video.
            name (str, optional): Desired name for the exported file.

        Returns:
            str: Export ID for tracking the request.

        Raises:
            ExacqvisionError: If the export request fails.
        """

        # Convert datetimes into timestamps
        start, stop = self.convert_datetime_to_iso8601(self.timezone, start, stop)

        url = f"{self.base_url}/v1/export.web?camera={camera_id}&s={self.session}&start={start}&end={stop}&format=mp4"
        if name:
            url = url+f'&name={name}'

        cameras = self.list_cameras()
        if not any(int(camera['id']) == int(camera_id) for camera in cameras):
            raise ExacqvisionError(f'CameraID: {camera_id} is not found in server')

        reporter = get_reporter()
        reporter.stage("request", "Requesting export from server")
        try:
            response = requests.request("GET", url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            export_id = json.loads(response.text).get('export_id')
            if not export_id:
                raise ExacqvisionError("Export creation failed: No export ID found in the response.")
            reporter.info(f"Export ID: {export_id}", export_id=export_id)
            return export_id
        except (RequestException, ValueError, KeyError) as e:
            raise ExacqvisionError(f"Export request failed: {str(e)}")


    def export_status(self, export_id: str) -> tuple[bool, int]:
        """
        Checks the status of an export request.

        Args:
            export_id (str): ID of the export request.

        Returns:
            bool: True if the export is complete (100%), False otherwise.
            progress: Percentage complete.
        """
        url = f"{self.base_url}/v1/export.web?export={export_id}"

        response = requests.request("GET", url, timeout=REQUEST_TIMEOUT)
        progress = int(json.loads(response.text)['progress'])

        return progress == 100, progress


    def export_download(self, export_id: str, output_dir: "Path | None" = None) -> str:
        """
        Downloads the completed video export.

        Args:
            export_id (str): ID of the export request.
            output_dir (Path, optional): Directory to write the file into.
                When ``None`` (default), the file is written into the current
                working directory -- matching the historical CLI behavior.
                When provided, the directory is created if missing and the
                file lands at ``output_dir / {server-supplied-name}.mp4``.

        Returns:
            str: Path to the downloaded video file. Absolute when
                ``output_dir`` is provided; bare basename (in CWD)
                otherwise.
        """

        url = f"{self.base_url}/v1/export.web?export={export_id}&action=download"

        # Setting stream=True is necessary to read the response body in chunks.
        try:
            response = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
        except RequestException as e:
            raise ExacqvisionError(
                f"Could not download export {export_id} from {self.base_url}: {e}"
            )

        if not response.ok:
            raise ExacqvisionError(
                f"Export download failed with HTTP {response.status_code} "
                f"for export {export_id}."
            )

        # Most responses carry the server-chosen name in Content-Disposition,
        # but it isn't guaranteed. Parse defensively and fall back to a stable
        # name derived from the export id so the download still lands sensibly.
        content_disposition = response.headers.get('Content-Disposition', '') or ''
        if 'filename=' in content_disposition:
            file_name = content_disposition.split('filename=')[-1].strip().strip('"')
        else:
            file_name = ''
        if not file_name:
            file_name = f"export_{export_id}.mp4"
        total_size = int(response.headers.get('content-length', 0))

        # Where on disk we'll actually write the bytes. Historically this was
        # always just `file_name` (i.e. CWD); honoring `output_dir` is how
        # programmatic callers (the web service) deliver straight into their
        # own output staging area without a follow-up move step.
        if output_dir is not None:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / file_name
        else:
            output_path = Path(file_name)

        reporter = get_reporter()
        reporter.stage(
            "export_download",
            "Downloading footage",
            filename=str(output_path),
            total_bytes=total_size,
        )

        try:
            with open(output_path, 'wb') as file:
                total_bytes_written = 0
                for data in response.iter_content(chunk_size=65536):
                    total_bytes_written += file.write(data)
                    if total_size > 0:
                        reporter.update(
                            "export_download",
                            total_bytes_written,
                            total_size,
                            unit="bytes",
                        )
        except Exception as e:
            raise ExacqvisionError(f"Download failed at {datetime.now()}: {str(e)}")

        reporter.info(
            f"Saved {output_path}",
            filename=str(output_path),
            bytes_written=total_bytes_written,
        )

        return str(output_path)


    def export_delete(self, export_id: str):
        '''Deletes an export request from the server.'''
        url = f"{self.base_url}/v1/export.web?export={export_id}&action=finish"

        response = requests.request("GET", url, timeout=REQUEST_TIMEOUT)

        return response.text


    def get_video(
        self,
        camera: int,
        start: datetime,
        stop: datetime,
        video_filename: str,
        num_of_retries: int = 5,
        output_dir: "Path | None" = None,
    ):
        """
        Exports and downloads a video from the specified camera and time range.

        Args:
            camera (int):           ID of the camera.
            start (datetime):       Start time of the search as a datetime object.
            stop (datetime):        End time of the search as a datetime object.
            video_filename (str):   Desired name for the exported video file
                                    (the value the server uses for its
                                    Content-Disposition header).
            num_of_retries (int):   How many times the script will retry if progress has not moved.
            output_dir (Path, optional): Directory to write the downloaded
                                    file into. When ``None``, the file goes
                                    into the current working directory --
                                    matching the historical behavior.
                                    Forwarded to ``export_download``.

        Returns:
            str: Path to the downloaded video file (absolute when
                ``output_dir`` is provided, basename in CWD otherwise).

        Raises:
            ExacqvisionError: If the export or download fails.
            ExacqvisionTimeoutError: If the export status fails to reach 100%.
        """
        reporter = get_reporter()
        export_id = None
        try:
            export_id = self.export_request(camera, start, stop, name=video_filename)
            sleep(2)  # Wait briefly before checking status

            reporter.stage("export_wait", "Waiting for server to prepare export")

            retries = 0
            ready_to_export, progress = self.export_status(export_id)
            reporter.update("export_wait", progress, 100, unit="percent")

            while not ready_to_export and retries <= num_of_retries:
                sleep(5)
                ready_to_export, updated_progress = self.export_status(export_id)
                reporter.update("export_wait", updated_progress, 100, unit="percent")

                # If progress doesn't move, tally a retry
                if updated_progress == progress:
                    retries += 1
                else:
                    retries = 0

                progress = updated_progress  # set progress to the last value received

            if retries > num_of_retries:
                raise ExacqvisionTimeoutError(f"Export {export_id} progress stalled for too long.")

            return self.export_download(export_id, output_dir=output_dir)

        except Exception as e:
            raise ExacqvisionError(f"Failed to get video: {str(e)}")
        finally:
            if export_id:
                sleep(2)  # Ensure download completes before cleanup
                self.export_delete(export_id)  # Clean up export request
        
        
    def get_timestamps(self, camera_id: int, start: datetime, stop: datetime) -> list[datetime]:
        """
        Retrieves timestamps for video clips within a time range.

        Args:
            camera_id (int): ID of the camera.
            start (datetime): Start time of the range.
            stop (datetime): End time of the range.

        Returns:
            list[datetime]: List of unique timestamps (one per second) in the local timezone.
        """
        
        _, response = self.create_search(camera_id, start, stop)

        # An empty/absent videoInfo means the window had no recorded footage
        # (common for motion-triggered cameras). Return an empty list rather
        # than raising on a missing key/index so callers can handle "no
        # footage" as an expected outcome instead of an error.
        video_info = json.loads(response.text).get('videoInfo') or []
        if not video_info:
            return []
        clips = video_info[0].get('clips') or []
        if not clips:
            return []

        # Returns list of all seconds between two times
        def generate_time_range(start_time, stop_time, stepsize=1):

            # Change to datetime object and then convert to local timezone
            start_datetime = self.convert_GMT_to_local(datetime.strptime(start_time, '%Y-%m-%dT%H:%M:%SZ'))
            stop_datetime = self.convert_GMT_to_local(datetime.strptime(stop_time, '%Y-%m-%dT%H:%M:%SZ'))

            delta = timedelta(seconds=stepsize)

            times = []
            while start_datetime <= stop_datetime:
                times.append(start_datetime)
                start_datetime += delta

            return times

        # Stretch every start/end time from clips into seconds
        ranged_timestamps = list(map(lambda x: generate_time_range(x['startTime'], x['endTime']), clips))

        # Flatten the timestamps into a one dimensional list.
        flattened_timestamps = [item for sublist in ranged_timestamps for item in sublist]

        # Filter out timestamp duplicates while maintaining their order.
        unique_timestamps = list(dict.fromkeys(flattened_timestamps))
        
        # Add timezone info to start and stop datetimes to prevent crashing due to them being offset-naive
        start = start.replace(tzinfo=self.timezone)
        stop = stop.replace(tzinfo=self.timezone)

        # Remove timestamps outside of the original start and stop times.
        finished_timestamps = [x for x in unique_timestamps if x >= start and x <= stop]

        return finished_timestamps


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
#
# `exacqvision.py` doubles as a small command-line utility for inspecting the
# server it wraps. The first action is `--list-cameras`; future actions
# (`--list-servers`, `--ping`, etc.) can slot in alongside it without
# changing the surrounding wiring.
#
# The CLI is intentionally a thin layer: it reuses the same TOML config +
# credentials conventions as `exacqman.py` (via `exacqman_config`) and the
# same progress reporter (so validation errors look identical across the two
# CLIs). The library half above is untouched -- importing this module from
# `exacqman.py` continues to work exactly as before.


# Exacqvision documents a status enum used across event / status fields
# (see "Event Type" tables in the API doc). The camera's `state` field is
# documented only by example; in practice 0 means "operating normally" and
# any of the documented disconnect codes mean "not currently capturing".
# We surface a small, stable vocabulary in the table -- OK / OFFLINE /
# DISABLED -- and fall back to the raw integer for any code not in the
# disconnect set, so unknown codes are loud rather than silently bucketed.
_DISCONNECT_STATE_CODES = {6, 13, 14, 18, 19, 21}


def _decode_camera_state(camera: dict) -> str:
    """Map a camera entry's `disabled` + `state` ints to a short label.

    `disabled == 1` short-circuits to ``DISABLED`` because an administratively
    disabled camera is a different condition from a temporarily unreachable
    one and conflating them would mask config issues.
    """
    if camera.get("disabled") == 1:
        return "DISABLED"
    state = camera.get("state")
    if state == 0:
        return "OK"
    if state in _DISCONNECT_STATE_CODES:
        return "OFFLINE"
    return f"state={state}" if state is not None else "?"


def _camera_resolution(camera: dict) -> str:
    """Format the `resolution` sub-dict as ``WIDTHxHEIGHT`` for the table."""
    res = camera.get("resolution") or {}
    w, h = res.get("width"), res.get("height")
    if isinstance(w, int) and isinstance(h, int):
        return f"{w}x{h}"
    return "--"


def _camera_fps(camera: dict) -> str:
    """Format the `frameRate` field, or `--` for missing/non-numeric values."""
    rate = camera.get("frameRate")
    return str(rate) if isinstance(rate, int) and rate > 0 else "--"


def _alias_for_camera_id(
    cam_id: int,
    server_cameras_config: dict,
) -> str:
    """Reverse-lookup the local alias for a server-reported camera ID.

    `server_cameras_config` is the active server's camera map -- the
    dict-valued ``[<server>.<alias>]`` sub-tables, shaped as
    ``{alias: {"id": int, ...}}``. Returns ``"--"`` when the server reports a
    camera that isn't wired up in the local config -- that's the discovery use
    case ("what's on the server that we haven't added yet?").
    """
    for alias, cam_data in (server_cameras_config or {}).items():
        if isinstance(cam_data, dict) and cam_data.get("id") == cam_id:
            return str(alias)
    return "--"


def _format_camera_table(
    cameras: list[dict],
    local_cameras: dict,
) -> str:
    """Render one server's cameras as an aligned ASCII table.

    Columns: ID, Local alias, Remote alias, State, Resolution, FPS.
    Widths are computed per-call so each section stays tight; we don't try
    to keep widths consistent across servers because a wide alias in one
    group shouldn't force every other group to pad to match.
    """
    headers = ("ID", "Local alias", "Remote alias", "State", "Resolution", "FPS")
    rows: list[tuple[str, ...]] = [headers]
    for cam in cameras:
        rows.append((
            str(cam.get("id", "?")),
            _alias_for_camera_id(cam.get("id"), local_cameras),
            str(cam.get("name", "")),
            _decode_camera_state(cam),
            _camera_resolution(cam),
            _camera_fps(cam),
        ))

    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    return "\n".join(render(row) for row in rows)


def _enrich_camera_for_json(camera: dict, local_cameras: dict) -> dict:
    """Return a copy of the API camera dict with derived fields added.

    Adds:
      - ``local_alias``: the alias of the ``[<server>.<alias>]`` table whose
        ``id`` matches this camera, or ``None`` if no local config entry.
      - ``state_label``: the same OK / OFFLINE / DISABLED / state=N string
        the table column shows, so machine consumers don't have to
        re-implement the status decode.
    Raw API fields pass through untouched.
    """
    enriched = dict(camera)
    alias = _alias_for_camera_id(camera.get("id"), local_cameras)
    enriched["local_alias"] = None if alias == "--" else alias
    enriched["state_label"] = _decode_camera_state(camera)
    return enriched


def _resolve_servers_to_query(
    cameras_by_server: dict,
    cli_server: str | None,
) -> list[str]:
    """Decide which servers in the config to query.

    Priority (matches `extract` subcommand semantics):
      1. ``--server`` on the CLI.
      2. Every declared server -- the discovery default.

    ``cameras_by_server`` is keyed by every declared server table name (the
    ``[<server>]`` tables), so its keys are the full set of known servers.
    Exits with a clear error if ``--server`` references a name that isn't
    in the config; that's a typo we can catch before any HTTP traffic.
    """
    server_names = list(cameras_by_server.keys())
    if cli_server:
        if cli_server not in cameras_by_server:
            get_reporter().error(
                "ConfigError",
                (
                    f"--server {cli_server!r} is not declared as a [<server>] table "
                    f"in the config. Available: {sorted(server_names)}"
                ),
            )
            sys.exit(1)
        return [cli_server]

    # Multi-server discovery default: iterate over everything declared.
    return server_names


def _list_cameras_for_servers(
    servers: Iterable[str],
    servers_by_name: dict,
    cameras_by_server: dict,
    auth: dict,
    timezone: ZoneInfo,
) -> list[dict]:
    """Login to each server in turn, list its cameras, and collect the result.

    Each server's connect / list / logout is wrapped in its own try block so
    one unreachable server doesn't tank the entire listing. Per-server
    failures surface as ``warning`` reporter events with a ``cameras: []``
    entry in the returned structure, letting callers render an empty
    section rather than dropping the server silently.
    """
    out: list[dict] = []
    for srv_name in servers:
        url = servers_by_name.get(srv_name)
        local_cameras = cameras_by_server.get(srv_name) or {}
        entry: dict = {
            "server": srv_name,
            "url": url,
            "cameras": [],
            "error": None,
        }

        if not url:
            entry["error"] = "no url configured"
            out.append(entry)
            continue

        api = None
        try:
            api = Exacqvision(url, auth["username"], auth["password"], timezone)
            raw_cameras = api.list_cameras()
            entry["cameras"] = [
                _enrich_camera_for_json(cam, local_cameras) for cam in raw_cameras
            ]
        except (RequestException, ExacqvisionError, ValueError, KeyError) as exc:
            entry["error"] = str(exc)
            get_reporter().warning(
                f"Failed to list cameras on server '{srv_name}' ({url}): {exc}"
            )
        finally:
            if api is not None:
                try:
                    api.logout()
                except Exception:
                    # Logout is best-effort; a server that's already returned
                    # a list isn't going to hold session state long enough
                    # for the leak to matter.
                    pass

        out.append(entry)
    return out


def _emit_camera_table(results: list[dict], cameras_by_server: dict) -> None:
    """Print the human-readable table form of `_list_cameras_for_servers` output."""
    sections: list[str] = []
    for entry in results:
        header = f"Server: {entry['server']}  {entry['url'] or '(no url)'}"
        if entry.get("error"):
            sections.append(f"{header}\n  Error: {entry['error']}")
            continue
        if not entry["cameras"]:
            sections.append(f"{header}\n  (no cameras reported)")
            continue
        local_cameras = cameras_by_server.get(entry["server"]) or {}
        # _enrich_camera_for_json adds derived fields but leaves the raw
        # ones in place, so the table formatter can read the same dict.
        table = _format_camera_table(entry["cameras"], local_cameras)
        sections.append(f"{header}\n{table}")
    print("\n\n".join(sections))


def _emit_camera_json(results: list[dict]) -> None:
    """Print the JSON form -- a list of {server, url, cameras, error} objects."""
    print(json.dumps(results, indent=2, sort_keys=False))


def parse_arguments() -> argparse.Namespace:
    """Define the CLI surface for `exacqvision.py`.

    Modeled after `exacqman.py`'s `extract` subcommand so the flags carry
    the same meaning across the two CLIs (positional config + ``--config``
    + ``--credentials`` + ``--server``). The action is in a mutually-
    exclusive group so future utility actions (``--list-servers``, etc.)
    can be added without breaking the contract.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Inspect an Exacqvision server. Currently supports listing "
            "cameras; future utility actions can be added alongside "
            "--list-cameras."
        ),
    )
    parser.add_argument(
        "config_file",
        nargs="?",
        default=None,
        help=(
            "Path to the TOML config file (same format as exacqman.py). "
            "Alternatively, pass --config."
        ),
    )
    parser.add_argument(
        "--config",
        dest="config_flag",
        default=None,
        help="Flag-form alternative to the positional config_file.",
    )
    parser.add_argument(
        "--credentials",
        default=None,
        help=(
            "Path to TOML credentials file. Overrides settings.credentials_file "
            "in the config."
        ),
    )
    parser.add_argument(
        "--server",
        default=None,
        help=(
            "Restrict the query to one server (must match a top-level [<server>] "
            "table in the config). Default: query every server declared in the config."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON instead of the default text table.",
    )

    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "--list-cameras",
        dest="list_cameras",
        action="store_true",
        help="List all cameras on the resolved server(s).",
    )

    return parser.parse_args()


def main() -> None:
    """Entrypoint for `python exacqvision.py ...`."""
    # Local import to avoid forcing the library half of this module to pull
    # in tomllib / config validation just for `from exacqvision import
    # Exacqvision`. The CLI half is the only consumer.
    from exacqman.exacqman_config import (
        import_config,
        import_credentials,
        resolve_credentials_path,
        split_servers_and_cameras,
    )

    args = parse_arguments()

    # Human reporter -- this is an interactive CLI utility, not a pipeline
    # action with structured progress to consume. Errors / warnings still
    # flow through the same reporter so config-validation failures look
    # identical to exacqman.py's.
    init_reporter(format="human", quiet=False)

    config_path = args.config_file or args.config_flag
    if args.config_file and args.config_flag and args.config_file != args.config_flag:
        get_reporter().error(
            "ConfigError",
            (
                "Both positional config_file and --config were given with "
                f"different values ({args.config_file!r} vs {args.config_flag!r}); "
                "specify only one."
            ),
        )
        sys.exit(1)
    if not config_path:
        get_reporter().error(
            "ConfigError",
            (
                "No config file specified. Pass one positionally or via --config "
                "(same format as exacqman.py's config file)."
            ),
        )
        sys.exit(1)

    config = import_config(config_path)
    credentials_path = resolve_credentials_path(config_path, config, args.credentials)
    auth = import_credentials(credentials_path)

    settings_table = config.get("settings", {}) or {}
    timezone_name = settings_table.get("timezone")
    # `validate_config` already enforced that this is a non-empty string,
    # but ZoneInfo's failure mode (ZoneInfoNotFoundError on an unknown name)
    # is still worth surfacing through the reporter rather than a raw
    # traceback.
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        get_reporter().error(
            "ConfigError",
            f"settings.timezone {timezone_name!r} is not a valid IANA zone: {exc}",
        )
        sys.exit(1)

    if args.list_cameras:
        servers_by_name, cameras_by_server = split_servers_and_cameras(config)
        servers = _resolve_servers_to_query(cameras_by_server, args.server)
        results = _list_cameras_for_servers(
            servers, servers_by_name, cameras_by_server, auth, timezone
        )
        if args.as_json:
            _emit_camera_json(results)
        else:
            _emit_camera_table(results, cameras_by_server)


if __name__ == "__main__":
    main()
