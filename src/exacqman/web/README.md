# ExacqMan Web

A small [FastAPI](https://fastapi.tiangolo.com/) UI that wraps the `exacqman` CLI. The browser collects extract parameters (camera, time range, multiplier, caption); the backend runs the **same CLI** as a subprocess and streams progress back to the page. All jobs run through a single serial queue, so concurrent submissions line up one-at-a-time.

This package is bundled with ExacqMan and is **opt-in at runtime** — `brew install` never starts a server.

## Running

```bash
exacqman-web start            # foreground on http://localhost:8887 (Ctrl-C to stop)
exacqman-web start -p 9000    # custom port
exacqman-web start --host 0.0.0.0
exacqman-web start --reload   # development auto-reload (watcher + worker)
exacqman-web status
exacqman-web stop
```

- `start` runs one foreground uvicorn process. The PID/host/port are recorded in a PID file under the log directory (`exacqman.paths.log_dir()`), so `stop`/`status` work from any terminal; if the PID file is missing/stale they fall back to discovering the listener on the port via `lsof`.
- There is **no `--background` flag**. For unattended operation use the OS service manager (`brew services start exacqman`) — a clean foreground process is exactly what a supervisor expects. See the repo root [`README.md`](../../../README.md) for the formula `service` block.

## Layout

```
web/
  app.py                  FastAPI app: CORS, routers, /health, static mounts (/ frontend, /exports)
  cli.py                  exacqman-web entrypoint (start / stop / status)
  api/
    models.py             Pydantic request/response models (ExtractRequest, Job, FileInfo, ...)
    routes.py             REST endpoints + the shared serial JobQueue instance
  services/
    exacqman_service.py   spawns `python -m exacqman ... --progress-format=json extract ...`
    job_queue.py          serial FIFO queue, terminal-job TTL, per-job log capture, export pruning
    config_service.py     reads *.config for the camera/server dropdowns
    file_service.py       lists / serves / deletes finished exports
  frontend/               static SPA (index.html, css, js) served at /
```

Paths (config dir, exports dir, log dir) all resolve through `exacqman.paths`; nothing is written inside the installed package. Exports default to `./exports` in the server's working directory, or `EXACQMAN_EXPORTS_DIR` when set (the service sets it explicitly). The frontend is served from inside the package via `importlib.resources`.

## REST API

All under the `/api` prefix unless noted.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/extract` | Enqueue an extract job. Returns `{job_id}`. `429` when the waiting backlog is full. |
| `GET` | `/api/jobs?since=<iso>` | Snapshot: running job, FIFO waiting list, and terminal jobs newer than `since`. |
| `GET` | `/api/jobs/{job_id}/log` | Download the captured log snippet for a failed/completed job (`404` if none). |
| `GET` | `/api/files` | List finished exports. |
| `GET` | `/api/download/{filename}` | Download an export. |
| `DELETE` | `/api/files/{filename}` | Delete an export. |
| `GET` | `/api/config/{config_file}` | Cameras + servers + timelapse options for a config. |
| `GET` | `/api/cameras/{config_file}` | Cameras for a config. |
| `GET` | `/health`, `/api/health` | Health checks. |

The web UI always operates on `default.config` (resolved from the config dir, then cwd) — there is no config picker. It loads that one config's servers and cameras on first page load and passes `config_file="default.config"` to every per-config endpoint and the extract request. The `{config_file}` endpoints remain parameterized so other callers can target any config by name.

Static mounts: `/` serves the frontend; `/exports` serves finished videos.

## CLI ↔ backend integration contract

The backend invokes the CLI as `python -m exacqman --progress-format=json extract <camera> --config <abs> --output-dir <exports> ...` and consumes **newline-delimited JSON** on stdout — one event object per line. Any non-JSON line (stray prints, ffmpeg noise, tracebacks) is logged and ignored, so it never breaks progress tracking. This contract is what any programmatic caller (not just this UI) should build against.

### Event types

| `event` | Fields | Meaning |
| --- | --- | --- |
| `stage` | `stage`, `message?` | Pipeline advanced to a new stage. |
| `progress` | `stage`, `current`, `total`, `unit`, `ts`, `message?`, `rate_label?` | Intra-stage progress. `rate_label` is a pre-formatted string (e.g. `"12.4 MB/s"`, `"140 FPS"`) when meaningful. |
| `info` | `message` | Informational note. |
| `warning` | `message` | Non-fatal warning. |
| `error` | `type`, `message` | Fatal error of a structured `type` (see below). |
| `done` | `output?` | Success. `output` is the authoritative on-disk path of the produced file. |

### Stage taxonomy

`request` → `export_wait` → `export_download` → `timelapsing` → `compression`. The UI maps each stage to a percentage band (see `_STAGE_RANGES` in `exacqman_service.py`) and scales `progress` events within it.

### Error types

Emitted as the `type` of an `error` event and mapped to a user-facing bucket by `ExtractFailure._CATEGORIES`:

| `type` | Bucket |
| --- | --- |
| `ConfigError`, `CredentialsError`, `CaptionTooLong` | configuration / input the user can fix |
| `ExacqvisionError` | the camera (ExacqVision) server itself |
| `VideoOpenError` | local video decode/transform failure |
| anything else / none | `InternalError` — synthesized when the CLI exits non-zero without an `error` event |

### Exit codes

- **0** — success; the CLI emitted exactly one `done` event whose `output` is the produced file.
- **non-zero** — failure; the CLI emitted an `error` event (use its `type`/`message`), or, if it exited abnormally without one, the backend synthesizes `InternalError`.

The full progress/event implementation lives in [`exacqman.progress`](../progress.py) (`JsonReporter`); the consumer side is `ExacqManService._consume_cli_events`.
