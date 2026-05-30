"""
exacqman_naming.py

Shared filename/naming conventions for ExacqMan.

Both the CLI (``exacqman.py``) and the web service
(``exacqman-web/backend/services/exacqman_service.py``) need to construct
the canonical default output filename for an extract job. This module is
the single source of truth for that convention so the two callers can't
drift.

Public surface:

  * ``sanitize_filename_component(value)``
        Normalize a single path component for use in an output filename
        (lowercase + spaces -> hyphens). Used internally by
        ``default_output_stem`` and exposed for the web service's user-
        supplied-filename path.

  * ``default_output_stem(start, server, camera_alias, multiplier)``
        Build the canonical default *stem* (no extension):
        ``{YYYY-MM-DD}_{HHMM}_{server}_{camera}_{multiplier}x``.

The stem is filename-safe on macOS / Linux (no path separators, no
reserved characters introduced by the sanitizer) but does not include an
extension; callers append ``.mp4`` (or whatever) themselves.

Notes on the format choices:

  * ``YYYY-MM-DD`` keeps the date in ISO-8601 calendar form so filenames
    sort lexically by date.
  * ``HHMM`` is 24-hour with zero padding so a 9:15 am file sorts before
    a 3:42 pm file lexically, with no am/pm suffix to disambiguate.
  * The server alias is included because the same camera alias can exist
    under multiple servers; without it, two extracts of the same alias
    on different servers would collide on disk.
  * ``unknown`` is the explicit fallback when the caller doesn't have a
    server set (CLI run ad-hoc, etc.) so the stem stays parseable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def sanitize_filename_component(value: str) -> str:
    """Lowercase and replace whitespace with hyphens for filesystem safety.

    This is the same normalization both the CLI auto-default and the
    web's user-supplied-filename path use, so a custom filename a user
    types in the UI ends up styled consistently with one we generate
    for them automatically.

    Length limits and path-separator rejection are *not* enforced here
    -- those belong on the API model layer (which can produce useful
    validation errors before this helper is even reached). This function
    is just a normalizer.
    """
    return value.lower().replace(" ", "-")


def default_output_stem(
    start: datetime,
    server: Optional[str],
    camera_alias: str,
    multiplier: int,
) -> str:
    """Build the canonical default output filename stem (no extension).

    Format: ``{YYYY-MM-DD}_{HHMM}_{server}_{camera}_{multiplier}x``

    Args:
        start: Start of the requested time range. The date and 24-hour
            time of this moment are baked into the stem.
        server: Server alias the camera lives under, e.g. ``"gpa"``.
            Falls back to ``"unknown"`` if ``None`` so the stem stays
            parseable for CLI-only ad-hoc runs.
        camera_alias: Camera alias as configured in the ``[<server>.<alias>]``
            table.
        multiplier: Timelapse multiplier (e.g. ``50`` for 50x).

    Returns:
        The stem, with no extension. Callers should append ``.mp4``
        (or whatever format applies).
    """
    date_str = start.strftime("%Y-%m-%d")
    time_str = f"{start.hour:02d}{start.minute:02d}"
    server_part = sanitize_filename_component(server) if server else "unknown"
    camera_part = sanitize_filename_component(camera_alias)
    return f"{date_str}_{time_str}_{server_part}_{camera_part}_{multiplier}x"
