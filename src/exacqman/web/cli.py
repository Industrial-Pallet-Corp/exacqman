#!/usr/bin/env python3
"""
exacqman-web -- start / stop / status entrypoint.

A single, symmetrical control script for the FastAPI app under uvicorn:

    exacqman-web start  [--port/-p N] [--host ADDR] [--reload/-r]
    exacqman-web stop
    exacqman-web status

The subcommand is optional and defaults to ``start``.

Process model
-------------
``start`` runs uvicorn as a single foreground process (logs stream to the
terminal; Ctrl-C triggers uvicorn's graceful shutdown, which runs the FastAPI
lifespan ``shutdown_event`` -> ``JobQueue.stop()`` before the process exits and
releases the port). The running PID, host, and port are recorded in a small
JSON PID file so ``stop`` / ``status`` can find the server later -- from any
terminal -- without hunting through ``ps``/``lsof`` by hand.

Running unattended (auto-restart, start-at-login, a background service) is
delegated to the OS service manager rather than a hand-rolled daemon: a clean
foreground process is exactly what ``brew services`` (launchd on macOS,
systemd on Linux) supervises via the formula's ``service`` block. There is no
``--background`` flag -- ``brew services start exacqman`` is the supported way
to run it detached.

``stop`` reads the PID file and sends SIGTERM to the process group (catching a
``--reload`` watcher+worker), waits up to a fixed grace period for a clean
exit, then escalates to SIGKILL. If the PID file is missing or stale, it falls
back to discovering the listener on the port via ``lsof``. Either way it
confirms the port is free before reporting success.

``--reload`` remains development-only: it spawns a watcher + worker that both
inherit the listening socket. ``stop`` handles that because it signals the
whole process group, but stick with the default for any non-iterative
workflow.

Stdlib-only -- no extra dependencies beyond uvicorn (already required).
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import uvicorn

from exacqman import paths, __version__

# Runtime state (the PID file) lives in the log dir resolved by exacqman.paths
# (Homebrew ``var/log`` or the XDG state dir), the same directory the job queue
# writes per-job logs into (UUID ``{job_id}.log`` stems), so ``server.pid``
# won't collide. Resolved at import so it's stable regardless of the process's
# cwd.
RUNTIME_DIR = paths.log_dir()
PID_FILE = RUNTIME_DIR / "server.pid"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8887

# How long `stop` waits for a graceful exit before escalating to SIGKILL.
# Comfortably above the 8s `JobQueue.stop()` bound in the FastAPI lifespan so
# an in-flight job teardown has room to finish cleanly.
STOP_GRACE_SECONDS = 15


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` exists and is signalable.

    ``os.kill(pid, 0)`` sends no signal but performs the existence +
    permission check. ESRCH -> gone; EPERM -> exists but owned by someone
    else (still "alive" for our purposes).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _port_free(host: str, port: int) -> bool:
    """Return True if nothing is accepting TCP connections on host:port.

    A connect attempt that's refused (or times out) means free. We connect to
    a loopback-equivalent for bind-all hosts since you can't connect to
    0.0.0.0 directly.
    """
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((connect_host, port))
        except (ConnectionRefusedError, socket.timeout, OSError):
            return True
        return False


def _wait_until(predicate, timeout: float, interval: float = 0.25) -> bool:
    """Poll ``predicate`` until it's truthy or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_pidfile() -> dict | None:
    """Return the parsed PID file dict, or None if missing/unreadable."""
    try:
        with open(PID_FILE, "r") as fp:
            data = json.load(fp)
    except (FileNotFoundError, IsADirectoryError):
        return None
    except (json.JSONDecodeError, OSError):
        # Corrupt PID file -- treat as absent; callers fall back to lsof.
        return None
    if not isinstance(data, dict) or "pid" not in data:
        return None
    return data


def _write_pidfile(pid: int, host: str, port: int, mode: str) -> None:
    """Persist the running server's identity for stop/status to find."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "host": host,
        "port": port,
        "mode": mode,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    with open(PID_FILE, "w") as fp:
        json.dump(payload, fp)


def _clear_pidfile() -> None:
    """Remove the PID file if present; never raise."""
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _lsof_listeners(port: int) -> list[int]:
    """Best-effort: PIDs listening on ``port`` via ``lsof``.

    Returns an empty list if ``lsof`` is unavailable or finds nothing. Used as
    a fallback when the PID file is missing or stale (server started from
    another terminal, or the file was deleted).
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in result.stdout.split():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _url(host: str, port: int) -> str:
    """Human-facing URL; render bind-all as localhost for clickability."""
    display_host = "localhost" if host in ("0.0.0.0", "", "::") else host
    return f"http://{display_host}:{port}/"


def _signal_pid_group(pid: int, sig: int) -> bool:
    """Send ``sig`` to ``pid``'s process group, falling back to the bare PID.

    Returns False if the target is already gone. Targeting the group catches a
    ``--reload`` watcher+worker (or a service-supervised session) in one shot.
    """
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    except (AttributeError, OSError):
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Can't signal it; report as "still there" so caller doesn't claim a
        # false success.
        return True
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    """Start the server in the foreground."""
    host, port = args.host, args.port

    # Already-running guard: a live PID file on the same port means there's a
    # server up; refuse rather than double-bind. A stale file (dead PID) is
    # silently overwritten.
    existing = _read_pidfile()
    if existing and _pid_alive(int(existing["pid"])):
        print(
            f"ExacqMan Web is already running (PID {existing['pid']}) on "
            f"{_url(existing.get('host', host), existing.get('port', port))}\n"
            f"Use '{_self_invocation()} stop' to stop it first.",
            file=sys.stderr,
        )
        return 1
    if existing:
        # Dead PID recorded -- clean it up before we (re)start.
        _clear_pidfile()

    return _start_foreground(host, port, args.reload)


def _start_foreground(host: str, port: int, reload: bool) -> int:
    """Run uvicorn in this process, owning the PID file for our lifetime."""
    mode = "reload" if reload else "foreground"
    _write_pidfile(os.getpid(), host, port, mode)

    print(
        f"Starting ExacqMan Web on {_url(host, port)}"
        f"{' (reload mode)' if reload else ''}  (Ctrl-C to stop)"
    )

    try:
        uvicorn.run(
            "exacqman.web.app:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info",
            access_log=True,
        )
    finally:
        # Remove the PID file on any exit (clean shutdown, Ctrl-C, crash) so a
        # later start/status doesn't see a stale entry. Only clear it if it
        # still points at us -- a racing restart may have rewritten it.
        current = _read_pidfile()
        if current and int(current.get("pid", -1)) == os.getpid():
            _clear_pidfile()
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the running server, however it was started, and free the port."""
    record = _read_pidfile()

    pid: int | None = None
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    source = ""

    if record and _pid_alive(int(record["pid"])):
        pid = int(record["pid"])
        host = record.get("host", DEFAULT_HOST)
        port = int(record.get("port", DEFAULT_PORT))
        source = "pidfile"
    else:
        # Stale or missing PID file. Clean it up and try to discover a listener
        # on the recorded (or default) port via lsof.
        if record:
            port = int(record.get("port", DEFAULT_PORT))
            host = record.get("host", DEFAULT_HOST)
            _clear_pidfile()
        listeners = _lsof_listeners(port)
        if not listeners:
            _clear_pidfile()
            print(f"ExacqMan Web is not running. Port {port} is free.")
            return 0
        pid = listeners[0]
        source = "lsof"

    print(
        f"Stopping ExacqMan Web (PID {pid}"
        f"{', discovered via lsof' if source == 'lsof' else ''})..."
    )

    # Graceful: SIGTERM the process group, wait for exit + port release.
    _signal_pid_group(pid, signal.SIGTERM)

    def _gone() -> bool:
        return not _pid_alive(pid) and _port_free(host, port)

    if not _wait_until(_gone, STOP_GRACE_SECONDS):
        print(
            f"  did not exit within {STOP_GRACE_SECONDS}s of SIGTERM; "
            f"escalating to SIGKILL."
        )
        _signal_pid_group(pid, signal.SIGKILL)
        # Also kill any remaining listeners lsof can still see (covers reload
        # workers that escaped the group, etc.).
        for extra in _lsof_listeners(port):
            if extra != pid:
                _signal_pid_group(extra, signal.SIGKILL)
        _wait_until(_gone, 5)

    _clear_pidfile()

    if _port_free(host, port):
        print(f"Stopped ExacqMan Web. Port {port} is free.")
        return 0

    print(
        f"Sent kill signals but port {port} still appears occupied. "
        f"Something else may be holding it.",
        file=sys.stderr,
    )
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Report whether the server is running and on what host/port."""
    record = _read_pidfile()

    if record and _pid_alive(int(record["pid"])):
        host = record.get("host", DEFAULT_HOST)
        port = int(record.get("port", DEFAULT_PORT))
        mode = record.get("mode", "foreground")
        started = record.get("started", "?")
        listening = "" if not _port_free(host, port) else "  (warning: port not accepting connections)"
        print(
            f"ExacqMan Web is running (PID {record['pid']}) on {_url(host, port)} "
            f"-- {mode} mode, since {started}{listening}"
        )
        return 0

    # No live PID file. Is something untracked listening on the default/known port?
    port = int(record.get("port", DEFAULT_PORT)) if record else DEFAULT_PORT
    if record:
        # Stale file -- clean it up so subsequent runs are tidy.
        _clear_pidfile()
    listeners = _lsof_listeners(port)
    if listeners:
        print(
            f"ExacqMan Web is running untracked (PID {listeners[0]}) on port "
            f"{port} -- no PID file. Use 'stop' to shut it down."
        )
        return 0

    print(f"ExacqMan Web is not running. Port {port} is free.")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _self_invocation() -> str:
    """Console-script name for help text."""
    return "exacqman-web"


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the start/stop/status CLI.

    The subcommand is optional and defaults to ``start`` (so a bare
    ``exacqman-web`` boots the server in the foreground).
    """
    parser = argparse.ArgumentParser(
        prog="exacqman-web",
        description="Start, stop, or check the ExacqMan Web server.",
    )
    parser.add_argument(
        "--version", "-v", action="version", version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    start_p = subparsers.add_parser(
        "start", help="Start the server (foreground by default)."
    )
    start_p.add_argument(
        "--port", "-p", type=int, default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT}).",
    )
    # --host is long-only on purpose: -h stays globally reserved for --help.
    start_p.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Interface to bind (default: {DEFAULT_HOST}).",
    )
    start_p.add_argument(
        "--reload", "-r", action="store_true",
        help=(
            "Enable uvicorn auto-reload for development. Off by default "
            "because reload mode runs a watcher + worker that complicate "
            "clean shutdown."
        ),
    )

    subparsers.add_parser("stop", help="Stop the running server and free the port.")
    subparsers.add_parser("status", help="Report whether the server is running.")

    args = parser.parse_args(argv)

    # Default to `start` when no subcommand is given. Synthesize the defaults
    # the `start` subparser would have produced.
    if args.command is None:
        args.command = "start"
        args.port = DEFAULT_PORT
        args.host = DEFAULT_HOST
        args.reload = False

    return args


_COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
