"""Read-only inspection of Exacqvision servers.

This module builds higher-level, human-facing inspection commands on top of
the transport client in :mod:`exacqman.exacqvision`. The first action is
camera listing (``exacqman list-cameras``); future actions (``list-servers``,
etc.) can slot in alongside it without touching the transport layer.

Kept separate from ``exacqvision.py`` so that module stays a focused
client/transport layer (the ``Exacqvision`` class + reachability probes), while
the presentation/orchestration logic that turns config + credentials into a
rendered table or JSON lives here.
"""

import json
import sys
from typing import Iterable
from zoneinfo import ZoneInfo

from requests.exceptions import RequestException

from exacqman.exacqman_config import split_servers_and_cameras
from exacqman.exacqvision import Exacqvision, ExacqvisionError
from exacqman.progress import get_reporter


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

    The ExacqVision API does not document the `state` field's value set, so any
    code we don't explicitly recognize (including a missing value) collapses to
    ``UNKNOWN`` rather than leaking a raw integer.
    """
    if camera.get("disabled") == 1:
        return "DISABLED"
    state = camera.get("state")
    if state == 0:
        return "OK"
    if state in _DISCONNECT_STATE_CODES:
        return "OFFLINE"
    return "UNKNOWN"


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


def list_cameras(
    config: dict,
    auth: dict,
    timezone: ZoneInfo,
    server: str | None = None,
    as_json: bool = False,
) -> int:
    """List the cameras reported by the configured Exacqvision server(s).

    Resolves which servers to query (one via ``server``, else all declared),
    logs into each, lists its cameras, and prints either an aligned text table
    or a JSON document. Per-server failures are reported as warnings and folded
    into the output rather than aborting the whole run.

    Returns a process exit code: ``1`` if any server errored (unreachable,
    auth failure, etc.), else ``0`` -- mirroring ``check``'s convention so
    scripts can branch on reachability.
    """
    servers_by_name, cameras_by_server = split_servers_and_cameras(config)
    servers = _resolve_servers_to_query(cameras_by_server, server)
    results = _list_cameras_for_servers(
        servers, servers_by_name, cameras_by_server, auth, timezone
    )
    if as_json:
        _emit_camera_json(results)
    else:
        _emit_camera_table(results, cameras_by_server)
    return 1 if any(entry.get("error") for entry in results) else 0
