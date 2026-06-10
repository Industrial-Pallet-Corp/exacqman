"""Runtime path resolution for ExacqMan.

Single source of truth for *where things live at runtime*, split into three
independent concerns so an installed (read-only) package never tries to write
into its own tree:

  * ``config_dir()``  -- persistent config + credentials. On a Homebrew install
    this is ``$(brew --prefix)/etc/exacqman`` (the ``pkgetc`` convention, which
    Homebrew preserves across upgrades); otherwise the XDG location
    ``~/.config/exacqman``.
  * ``exports_dir()`` -- where finished videos land. Defaults to ``./exports``
    in the current working directory for interactive CLI / foreground web use;
    a backgrounded service points it elsewhere via ``EXACQMAN_EXPORTS_DIR`` (or
    an explicit override).
  * ``tmp_dir()``     -- temporary scratch space for in-progress extract
    intermediates (the raw download, the timelapsed clip, the compressed clip).
    Kept deliberately *outside* the exports dir so half-finished files never
    show up in the web file browser's "exported footage" list while a job is
    running; only the finished, tagged file is moved into the exports dir on
    success. Defaults to an ``exacqman-tmp`` sibling of the exports dir (same
    filesystem, so the final move is a fast rename) and is overridable via
    ``EXACQMAN_TMP_DIR``.
  * ``log_dir()``     -- server PID file, server log, and per-job logs. On
    Homebrew this is ``$(brew --prefix)/var/log``; otherwise the XDG state
    location ``~/.local/state/exacqman``.

All resolvers are env-overridable and never create directories as a side
effect; call ``ensure_dir`` (or the caller's own ``mkdir``) at the point of
use so importing this module stays free of filesystem writes.
"""

from __future__ import annotations

import os
from pathlib import Path

# Environment overrides (highest precedence within each resolver).
ENV_CONFIG_DIR = "EXACQMAN_CONFIG_DIR"
ENV_EXPORTS_DIR = "EXACQMAN_EXPORTS_DIR"
ENV_TMP_DIR = "EXACQMAN_TMP_DIR"
ENV_LOG_DIR = "EXACQMAN_LOG_DIR"


def _homebrew_prefix() -> Path | None:
    """Return the Homebrew prefix if this package is installed under one.

    Detection is filesystem-derived and needs no ``brew`` subprocess (so it
    works inside launchd/systemd where the shell environment is absent):
    once installed, ``__file__`` resolves to a real path under
    ``<prefix>/Cellar/exacqman/<version>/...`` (the ``opt`` symlink is
    collapsed by ``resolve()``), so the segment before ``Cellar`` is the
    prefix. ``HOMEBREW_PREFIX`` in the environment takes precedence when set.
    """
    env = os.environ.get("HOMEBREW_PREFIX")
    if env:
        candidate = Path(env)
        if candidate.is_dir():
            return candidate

    parts = Path(__file__).resolve().parts
    if "Cellar" in parts:
        return Path(*parts[: parts.index("Cellar")])
    return None


def _xdg_dir(env_var: str, default_relative: tuple[str, ...]) -> Path:
    """Resolve an XDG base dir from ``env_var`` or fall back under ``$HOME``."""
    base = os.environ.get(env_var)
    if base:
        return Path(base).expanduser()
    return Path.home().joinpath(*default_relative)


def config_dir() -> Path:
    """Directory holding the user's ``*.config`` / ``*.credentials`` files.

    Precedence: ``EXACQMAN_CONFIG_DIR`` > Homebrew ``<prefix>/etc/exacqman`` >
    ``$XDG_CONFIG_HOME/exacqman`` (default ``~/.config/exacqman``).
    """
    env = os.environ.get(ENV_CONFIG_DIR)
    if env:
        return Path(env).expanduser()

    prefix = _homebrew_prefix()
    if prefix is not None:
        return prefix / "etc" / "exacqman"

    return _xdg_dir("XDG_CONFIG_HOME", (".config",)) / "exacqman"


def exports_dir(override: str | os.PathLike | None = None) -> Path:
    """Directory where finished videos are written / served.

    Precedence: explicit ``override`` (a ``--exports-dir`` flag or
    ``[settings].exports_dir`` resolved by the caller) > ``EXACQMAN_EXPORTS_DIR``
    > ``./exports`` in the current working directory. Interactive CLI runs and
    a foreground web server fall through to the cwd; a managed background
    service sets the env var (or override) to a stable location such as
    ``<prefix>/var/exacqman/exports``.
    """
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_EXPORTS_DIR)
    if env:
        return Path(env).expanduser()
    return Path.cwd() / "exports"


def tmp_dir(override: str | os.PathLike | None = None) -> Path:
    """Temporary scratch directory for in-progress extract intermediates.

    An extract run downloads the raw clip, timelapses it, and compresses it
    in a fresh per-run subdirectory of this location, then moves *only* the
    finished, tagged file into the exports dir. Keeping the tmp dir separate
    from the exports dir is what stops half-finished files from appearing in
    the web file browser's "exported footage" list while a job is active.

    Precedence: explicit ``override`` > ``EXACQMAN_TMP_DIR`` > an
    ``exacqman-tmp`` sibling of the exports dir. The default deliberately
    shares the exports dir's parent so the final move is a same-filesystem
    rename rather than a copy; a managed service whose exports live at
    ``<prefix>/var/exacqman/exports`` therefore works in
    ``<prefix>/var/exacqman/exacqman-tmp``.
    """
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_TMP_DIR)
    if env:
        return Path(env).expanduser()
    return exports_dir().parent / "exacqman-tmp"


def log_dir() -> Path:
    """Directory for the server PID file, server log, and per-job logs.

    Precedence: ``EXACQMAN_LOG_DIR`` > Homebrew ``<prefix>/var/log`` >
    ``$XDG_STATE_HOME/exacqman`` (default ``~/.local/state/exacqman``).
    """
    env = os.environ.get(ENV_LOG_DIR)
    if env:
        return Path(env).expanduser()

    prefix = _homebrew_prefix()
    if prefix is not None:
        return prefix / "var" / "log"

    return _xdg_dir("XDG_STATE_HOME", (".local", "state")) / "exacqman"


def ensure_dir(path: Path) -> Path:
    """``mkdir -p`` ``path`` and return it (convenience for call sites)."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def iter_config_files() -> list[Path]:
    """Discover ``*.config`` files in the config dir, then the cwd (deduped).

    Used by the web UI's config dropdown and the CLI's config search so both
    surface the same set of files regardless of where the process was launched.
    Config-dir matches win over same-named cwd matches.
    """
    seen: dict[str, Path] = {}
    for base in (config_dir(), Path.cwd()):
        try:
            for candidate in sorted(base.glob("*.config")):
                seen.setdefault(candidate.name, candidate)
        except OSError:
            continue
    return list(seen.values())
