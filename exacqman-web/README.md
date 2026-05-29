# ExacqMan Web

A web frontend + REST API around the [ExacqMan CLI](../README.md). End users
pick a server, camera, time range, multiplier, caption, and filename in the
browser; the backend spawns the CLI per request, streams progress back as
the extract runs, and delivers the finished video into `exports/`.

## Architecture

```
┌────────────────┐    HTTP    ┌────────────────────┐   subprocess   ┌──────────────────┐
│  Browser UI    │  ◄──────►  │  FastAPI backend   │   ──────────►  │  exacqman.py CLI │
│  (vanilla JS)  │            │  + job queue       │   ◄──────────  │  (JSON events)   │
└────────────────┘            └────────────────────┘     stdout     └──────────────────┘
                                       │
                                       │ writes
                                       ▼
                               `<repo root>/exports/`
```

- **Frontend** (`frontend/`): vanilla HTML/CSS/JS. Polls `GET /api/jobs` for
  live job state; renders the queue, progress bars, completed jobs, and the
  extracted-footage file list.
- **Backend** (`backend/`): FastAPI service. A single global job queue
  (1 running + up to 3 waiting) serializes CLI subprocesses, so an extract
  job is always running alone against the camera server. Per-job logs are
  captured to `logs/{job_id}.log` for the UI's "Download log" link on
  failure.
- **CLI** (the same `exacqman.py` humans use from a terminal): spawned per
  job with `--progress-format=json`, which makes it emit one JSON event per
  line on stdout. The backend reads those events and turns them into job
  state updates. See the [Integration Contract](#cli-integration-contract)
  below for the full protocol.

## Layout

```
exacqman-web/
├── README.md           # this file
├── backend/            # FastAPI app, job queue, CLI integration
│   ├── api/            #   routes + Pydantic models
│   ├── services/       #   job queue, CLI runner
│   ├── app.py          #   FastAPI app factory
│   ├── exacqman_web.py #   start/stop/status entrypoint (uvicorn)
│   └── README.md       #   backend-specific notes
├── frontend/           # static UI (HTML/CSS/JS)
└── logs/               # per-job log snippets + server.pid/server.log (gitignored)
```

Finished videos are written to `exports/` at the **project root** (one level
above `exacqman-web/`, gitignored), shared with the CLI so terminal users
running from the repo root find them right alongside the tool.

## Getting Started

Prerequisites: same as the CLI (Python 3.8+, `requests`, `tqdm`, `moviepy`,
`opencv-python`, `python-dateutil`, `tzdata`) plus FastAPI/uvicorn for the
backend.

```bash
# 1. Install backend dependencies
pip install -r backend/requirements.txt

# 2. Make sure default.config + default.credentials exist at the repo root
#    (see ../README.md for setup)

# 3. Start the server (defaults: 0.0.0.0:8887, foreground, no auto-reload)
python backend/exacqman_web.py start
#   --port / -p <n>      pick a different port
#   --host <addr>        bind to a different interface (long-form only)
#   --reload / -r        watch for file changes (development only)
#   --background / -b    detach and run in the background (logs -> logs/server.log)

# Stop it (works from any terminal, however it was started):
python backend/exacqman_web.py stop

# Check whether it's running:
python backend/exacqman_web.py status
```

`start` is the default subcommand, so `python backend/exacqman_web.py` (no
arguments) still boots the server in the foreground. The running server's PID,
host, and port are recorded in `logs/server.pid` so `stop`/`status` can find it
later without hunting through `ps`/`lsof`. `stop` sends a graceful SIGTERM
(escalating to SIGKILL after 15s), then confirms the port is free; if the PID
file is missing it falls back to discovering the listener on the port via
`lsof`.

The browser UI is served at the configured port (e.g.
`http://localhost:8887/`); FastAPI's auto-generated API docs live at
`/docs` and `/redoc`.

## API Surface

All endpoints are JSON over HTTP. The browser UI uses these; external
callers can use them directly too.

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/api/extract`             | Enqueue an extract job; returns the assigned `job_id` |
| `GET`    | `/api/jobs`                | Snapshot of all jobs (running + queued + recently finished) |
| `GET`    | `/api/jobs/{job_id}/log`   | Download the per-job log snippet (failures only) |
| `GET`    | `/api/files`               | List finished videos in `exports/` |
| `GET`    | `/api/download/{filename}` | Stream a finished video |
| `DELETE` | `/api/files/{filename}`    | Delete a finished video |
| `GET`    | `/api/configs`             | List `.config` files at the repo root |
| `GET`    | `/api/config/{config_file}`| Parsed view of a config file |
| `GET`    | `/api/cameras/{config_file}` | Cameras declared in that config (with their server) |

Queue overflow: `POST /api/extract` returns **HTTP 429** when the queue is
full (1 running + 3 waiting); the UI mirrors this by disabling the "Extract
Footage" button.

## CLI Integration Contract

This is the contract the backend relies on when spawning `exacqman.py`. It
is also what any other programmatic caller (CI pipeline, CLI wrapper,
alternative frontend) should target. Wherever it disagrees with the source,
the source wins -- but the source is structured to keep this contract
stable.

### Invocation

The backend spawns the CLI as:

```bash
python3 -u <repo>/exacqman.py \
    --progress-format=json \
    extract <camera_alias> \
    --config <abs path to .config> \
    --start-iso-datetime <ISO 8601> \
    --end-iso-datetime <ISO 8601> \
    --multiplier <int> \
    -c \
    -o <output stem> \
    --output-dir <abs path to <repo>/exports> \
    [--server <name>] \
    [--caption <text>]
```

Notes:

- **`-u`** (unbuffered) makes the JSON event stream arrive in real time.
- **`--progress-format=json`** switches the global reporter from
  `HumanReporter` (tqdm bars on a TTY) to `JsonReporter`. The human form
  is preserved for users running the CLI from a terminal; the JSON form
  is what we consume here.
- **`--config`**, **`--start-iso-datetime`**, **`--end-iso-datetime`** are
  flag-form alternatives to the positional `config_file` / `date` /
  `start` / `end` arguments. Using flags here means the call is
  order-independent and never round-trips through `%m/%d` + `%I:%M%p`
  (which the old code did, losing year and timezone precision).
- **`--output-dir`** delivers a single clean file at
  `<output_dir>/<output stem>.mp4`. See [Output Contract](#output-contract).
- **Process group**: the backend spawns the CLI with
  `start_new_session=True` so the CLI and every ffmpeg / exacqvision
  download it fork-execs share a fresh process group ID. On job
  cancellation or server shutdown the whole group is torn down with
  `killpg(SIGTERM)` → 3s grace → `killpg(SIGKILL)`. No orphaned ffmpeg
  processes survive a graceful or forceful shutdown.

### JSON event stream

The CLI writes one event per line on stdout. Each line is a self-contained
JSON object terminated by `\n`. Non-JSON lines (stray prints, ffmpeg
output that escapes the reporter) are logged on the backend and ignored
for job state.

Every event carries:

| Field   | Type   | Description |
|---|---|---|
| `event` | string | One of `stage`, `progress`, `info`, `warning`, `error`, `done` |
| `ts`    | float  | Unix epoch (seconds, with sub-second precision) when the event was produced or, for `progress`, when the sample was observed |

Event-specific fields:

#### `stage`

Signals entry into a new pipeline stage. Emitted once per stage transition.

```json
{ "event": "stage", "stage": "export_download", "message": "Downloading footage", "ts": 1735812345.123 }
```

Extra metadata may be attached as additional keys (e.g.
`filename`, `output`, `total_frames`). Consumers should treat unknown
keys as informational.

#### `progress`

Periodic sample within the current stage. Throttled by the CLI to at most
one event per 200ms per stage (the terminal sample of a stage is always
flushed). Consumers can treat any newer `progress` as superseding the
previous one for the same stage.

```json
{
  "event": "progress",
  "stage": "export_download",
  "current": 25000000,
  "total": 100000000,
  "unit": "bytes",
  "rate_label": "12.4 MB/s",
  "ts": 1735812346.435
}
```

| Field        | Type    | Description |
|---|---|---|
| `stage`      | string  | The active stage (must match a recent `stage` event) |
| `current`    | int     | Work units done so far |
| `total`      | int     | Total work units (always positive) |
| `unit`       | string  | `bytes`, `frames`, or `percent` |
| `rate_label` | string  | Optional, pre-formatted. Present for `bytes` (`"<x.x> MB/s"`) and `frames` (`"<n> FPS"`) once at least two samples have been observed in the stage. Computed inside `JsonReporter` (EMA, factor 0.3 -- matches tqdm's default) so consumers don't have to recompute it. |

`current` is always clamped to `total` so consumers can compute a ratio
safely. `percent` events carry no `rate_label`.

#### `info`, `warning`

Free-form one-line messages. The backend logs them and discards them for
job state.

```json
{ "event": "info",    "message": "Saved exports/dock-6-test.mp4", "ts": 1735812400.0 }
{ "event": "warning", "message": "Cropped frame size doesn't match expected", "ts": 1735812350.0 }
```

#### `error`

A structured failure event. The CLI emits at most one per run. The next
event is typically the process exiting non-zero (see
[Exit Codes](#exit-codes)) -- callers must wait for the exit to fully
distinguish "error event mid-run, recovered" from "error event then died".
In practice the CLI does not currently recover from `error`; it exits
non-zero immediately afterwards.

```json
{ "event": "error", "type": "ExacqvisionError", "message": "Failed to get video: ...", "ts": 1735812410.0 }
```

| Field     | Type   | Description |
|---|---|---|
| `type`    | string | Stable error type identifier (see [Error Type Taxonomy](#error-type-taxonomy)) |
| `message` | string | Detailed technical message (goes into the per-job log) |

#### `done`

Successful completion. Emitted exactly once on success. `output` is the
authoritative path of the artifact -- the backend uses this rather than
scanning the workspace to discover what was produced.

```json
{ "event": "done", "output": "/abs/path/to/exports/dock-6-test.mp4", "ts": 1735812420.0 }
```

| Field    | Type   | Description |
|---|---|---|
| `output` | string | Absolute (or CLI-cwd-relative) path of the deliverable |

### Stage Taxonomy

The extract pipeline emits stages in this fixed order. Their `unit`
defines what `progress.current` / `progress.total` count, and the
backend's percentage ranges show how a stage maps onto the 0-100 overall
job progress bar in the UI.

| Stage             | Unit      | % range  | Meaning |
|---|---|---|---|
| `request`         | (no progress) | 0-1   | Sending the export request to the exacqvision server |
| `export_wait`     | `percent` | 1-10     | Polling the server while it prepares the export |
| `export_download` | `bytes`   | 10-25    | Streaming the prepared file to disk |
| `timelapsing`     | `frames`  | 25-75    | Cropping + timestamping + speed-up; emits per-frame samples |
| `compression`     | `frames`  | 75-99    | libx264 encode of the timelapsed video |
| (`done`)          | -         | 100      | Pipeline complete |

Notes:

- A stage without any `progress` events (like `request`) still emits the
  `stage` event; the UI advances to the low end of the range and waits
  for the next stage.
- Unknown stages are logged but don't advance the bar -- the backend warns
  about contract drift and otherwise ignores them.

### Error Type Taxonomy

The CLI emits a small, stable vocabulary of `error.type` values. The
backend maps each one onto a single user-facing message (see
`ExtractFailure._CATEGORIES` in
`backend/services/exacqman_service.py`); unrecognized types fall through
to a default "internal error" message. New error types should be added to
both ends in the same patch.

| `error.type`        | Bucket          | User-facing message                                      | Typical cause |
|---|---|---|---|
| `ConfigError`       | configuration   | "Video extraction failed: configuration problem"         | Bad TOML, missing required field, malformed datetime, mutually-exclusive flags |
| `CredentialsError`  | configuration   | "Video extraction failed: authentication problem"        | Missing credentials file, missing username/password |
| `CaptionTooLong`    | configuration   | "Video extraction failed: caption too long"              | Caption exceeded the 30-char limit (the web UI normally blocks this client-side) |
| `ExacqvisionError`  | server          | "Video extraction failed: couldn't reach the camera server" | Camera server unreachable, export request rejected, export progress stalled |
| `VideoOpenError`    | processing      | "Video extraction failed: video processing error"        | OpenCV / moviepy couldn't open the downloaded file |
| `InternalError`     | (default)       | "Video extraction failed: internal error"                | Synthesized on the backend when the CLI exits non-zero without emitting any `error` event (a contract violation -- treat as "we don't know what happened") |
| _anything else_     | (default)       | "Video extraction failed: internal error"                | Unrecognized type. Add a `_CATEGORIES` entry to surface a more specific message. |

The detailed `error.message` is preserved in the per-job log
(`logs/{job_id}.log`) for debugging.

### Output Contract

The CLI's extract pipeline writes three files. **Without** `--output-dir`,
they all land in CWD with their stem-based names (codec suffix on the
compressed file), and intermediates are left in place for the human user
to inspect:

```
{stem}.mp4                              # raw download
{stem}_{multiplier}x.mp4                # timelapsed
{stem}_{multiplier}x_libx264_{q}.mp4    # final compressed
```

**With** `--output-dir DIR`, the pipeline writes all three into `DIR/`,
then on successful completion:

1. Deletes the raw download `{stem}.mp4`.
2. Deletes the timelapsed intermediate `{stem}_{multiplier}x.mp4`.
3. Atomically renames the compressed file to bare `{stem}.mp4`.

So `DIR/` ends up holding exactly one user-facing file:
`DIR/{stem}.mp4`. The path emitted in `done.output` reflects the
post-rename location. This is the programmatic-delivery contract -- the
backend depends on it so it doesn't need to do any post-pipeline move /
cleanup work itself (i.e. it has no reason to touch any path outside
`exports/`).

If the pipeline fails partway, no rename or cleanup happens. The
intermediates remain in `DIR/` for inspection. Callers retrying with the
same `--output-name` will simply overwrite them on the next run.

### Output File Metadata

In addition to the deliverable mp4, the extract pipeline embeds a
JSON-encoded provenance blob directly into the file's MP4 container, so
the metadata travels with the file wherever it goes. This is what makes
the web UI's "Camera" column resolve correctly even if the source
`.config` is later renamed or rewritten -- and it works regardless of
what filename the user picked via `-o`.

The embed is a no-re-encode `ffmpeg -codec copy` pass, run as the final
step of the extract pipeline (after `--output-dir` finalization). On
any ffmpeg failure the helper logs a warning via the reporter and leaves
the file untouched: the deliverable is the contract, the metadata is a
bonus.

**Where it lives.** The blob is stored in the standard MP4 `comment`
atom (`\xa9cmt`). The standard `title` atom (`\xa9nam`) is also set to
the file stem as a convenience for generic media players; it carries
no structured meaning and is not part of the contract.

**Reading it.** Any mp4 metadata tool will surface it:

```bash
# ffprobe (bundled with ffmpeg)
ffprobe -v error -show_format -of json file.mp4 \
    | jq -r '.format.tags.comment' | jq .

# mediainfo
mediainfo --Output=JSON file.mp4 | jq -r '.media.track[0].Comment' | jq .
```

```python
# mutagen (the backend uses this -- see backend/services/file_service.py)
from mutagen.mp4 import MP4
import json
mp4 = MP4("file.mp4")
payload = json.loads(mp4.tags["\xa9cmt"][0])
```

**Schema.** The blob is a single JSON object. Field shape and presence:

| Field                          | Type    | Required | Description |
|---|---|---|---|
| `exacqman_metadata_version`    | int     | yes      | Schema version (currently `1`). Bumped on incompatible changes; additive changes do not bump. |
| `server`                       | string  | yes      | Server alias from the config file (e.g. `"gpa"`) |
| `camera_alias`                 | string  | yes      | Camera alias from the config file (e.g. `"dock-6"`) |
| `camera_id`                    | int     | yes      | Exacqvision camera ID (stable across config renames) |
| `multiplier`                   | int     | yes      | Timelapse multiplier (e.g. `50` for 50x) |
| `start_iso`                    | string  | yes      | ISO 8601 start datetime, with timezone offset |
| `end_iso`                      | string  | yes      | ISO 8601 end datetime, with timezone offset |
| `timezone`                     | string  | yes      | IANA timezone name (e.g. `"America/Indiana/Indianapolis"`) |
| `caption`                      | string  | no       | User-supplied caption. Omitted from the payload when empty. |

Example payload (formatted -- the on-disk form is minified, no whitespace):

```json
{
  "exacqman_metadata_version": 1,
  "server": "gpa",
  "camera_alias": "dock-6",
  "camera_id": 12345,
  "multiplier": 50,
  "start_iso": "2026-05-27T09:30:00-04:00",
  "end_iso": "2026-05-27T09:45:00-04:00",
  "timezone": "America/Indiana/Indianapolis",
  "caption": "Hello world"
}
```

Empty-string / `None` values are dropped before encoding, so readers
should treat missing keys as "unset" rather than as a schema violation.

**Legacy files.** Files written before this contract existed have no
`comment` tag at all. The backend treats that as "no embedded
metadata" and silently falls back to parsing the canonical filename
convention (`{date}_{time}_{server}_{camera}_{multiplier}x.mp4`); files
that match neither will surface as "Unknown" in the file browser
until they are re-extracted.

### Exit Codes

| Code  | Meaning |
|---|---|
| `0`   | Pipeline completed; `done` event emitted. The file at `done.output` is the deliverable. |
| _non-zero_ | Pipeline failed. If an `error` event was emitted just before exit, its `type` + `message` describes the failure. If no `error` event was emitted, the backend synthesizes a generic `InternalError` (contract violation, treat as "something went wrong but we don't know what"). |

The backend treats any non-zero exit as a `ExtractFailure`. Callers that
want to retry should base the decision on `error.type` (the
`configuration` bucket is user-fixable; `server` is transient; `processing`
and `internal` are usually not retry-worthy without intervention).

### Stability

Anything documented above is part of the contract; everything else
(internal stage progress sub-events, ffmpeg's own stderr noise, the exact
wording of `info` / `warning` messages, intermediate filenames in
human-mode) is implementation detail and can change without notice.

Adding a new stage, event field, error type, or output-metadata field
is backwards-compatible as long as existing consumers gracefully ignore
unknown fields and treat unknown stages as informational. Removing or
renaming anything in the tables above is a breaking change.

The embedded-metadata schema (`Output File Metadata`) is independently
versioned by `exacqman_metadata_version`. Additive changes (new
optional keys) do not bump the version; incompatible changes (key
removal, type change, semantic redefinition) bump it, and readers
should treat any version newer than they understand as best-effort.
