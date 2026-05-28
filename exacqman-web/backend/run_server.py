#!/usr/bin/env python3
"""
ExacqMan Web Server Startup Script

Starts the FastAPI app under uvicorn as a single process by default. This
keeps shutdown straightforward -- a single SIGTERM/SIGINT triggers
uvicorn's graceful shutdown which in turn runs the FastAPI lifespan
``shutdown_event`` (and ``JobQueue.stop()``) before the process exits and
releases the port.

For development, pass ``--reload`` to enable uvicorn's autoreload mode.
Note: reload mode spawns a watcher parent plus a worker child that both
inherit the listening socket, so a clean shutdown requires signaling the
parent (the worker is supervised). Stick with the default for any
non-iterative workflow.
"""

import uvicorn
import sys
import os
import argparse
from pathlib import Path

backend_dir = Path(__file__).parent
sys.path.insert(0, str(backend_dir))

# Make the ExacqMan project root importable so backend modules can pull
# in shared helpers that live alongside the CLI (e.g. ``exacqman_naming``).
# Layout: <project_root>/exacqman-web/backend/run_server.py -> root is two
# levels up. Insert *after* backend_dir so backend-local imports still win
# if there's ever a name collision.
project_root = backend_dir.parent.parent
sys.path.insert(1, str(project_root))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Start ExacqMan Web Server')
    parser.add_argument('--port', '-p', type=int, default=8000,
                       help='Port number to run the server on (default: 8000)')
    parser.add_argument('--host', default='0.0.0.0',
                       help='Host to bind the server to (default: 0.0.0.0)')
    parser.add_argument('--reload', action='store_true',
                       help=(
                           'Enable uvicorn auto-reload for development. Off by '
                           'default because reload mode runs a watcher + worker '
                           'that complicate clean shutdown.'
                       ))

    args = parser.parse_args()

    os.chdir(backend_dir)

    print(f"Starting ExacqMan Web Server on {args.host}:{args.port}"
          f"{' (reload mode)' if args.reload else ''}")

    uvicorn.run(
        "app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        access_log=True,
    )
