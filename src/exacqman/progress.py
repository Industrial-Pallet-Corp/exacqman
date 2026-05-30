"""
progress.py

Unified progress reporting for the ExacqMan CLI.

Two output modes are provided:

- HumanReporter: pretty TTY output with stage banners and a single tqdm bar per
  stage. Intended for direct interactive CLI use.

- JsonReporter: emits one JSON event per line on stdout. Intended for
  programmatic consumers (the ExacqMan web service runs the CLI as a subprocess
  and parses these events to drive the UI).

Format selection:

    init_reporter(format="auto", quiet=False)

`format` may be "human", "json", or "auto". `auto` resolves as follows
(highest priority first):

  1. The EXACQMAN_PROGRESS_FORMAT environment variable, if set to
     "human" or "json".
  2. "human" when stdout is a TTY, otherwise "json".

A module-level singleton holds the active reporter. Before init_reporter() is
called, get_reporter() returns a NullReporter so that module imports never
fail.

Stage names used across the CLI (in approximate order for `extract`):

  request          - submitting the export request to the server
  export_wait      - waiting for the server to prepare the export
  export_download  - downloading the prepared export from the server
  timelapsing      - local frame processing (timelapse / timestamping)
  compression      - local video compression (MoviePy / ffmpeg)
  done             - terminal success event
  error            - terminal error event

The `extract` subcommand emits all stages; `compress` emits only `compression`;
`timelapse` emits only `timelapsing`. Each subcommand emits a final `done`
(or `error`) event.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore

try:
    from proglog import ProgressBarLogger
except ImportError:  # pragma: no cover
    ProgressBarLogger = None  # type: ignore


# Human-readable labels for known stages. Unknown stages fall back to the raw
# stage name.
_STAGE_LABELS = {
    "request": "Requesting export from server",
    "export_wait": "Waiting for server to prepare export",
    "export_download": "Downloading footage",
    "timelapsing": "Timelapsing footage",
    "compression": "Compressing video",
    "done": "Done",
    "error": "Error",
}


class Reporter:
    """Abstract reporter. Subclasses implement the display behavior."""

    def stage(self, name: str, message: Optional[str] = None, **meta: Any) -> None:
        raise NotImplementedError

    def update(
        self,
        stage: str,
        current: int,
        total: int,
        unit: str,
        message: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    def info(self, message: str, **meta: Any) -> None:
        raise NotImplementedError

    def warning(self, message: str, **meta: Any) -> None:
        raise NotImplementedError

    def error(self, error_type: str, message: str, **meta: Any) -> None:
        raise NotImplementedError

    def done(self, output: Optional[str] = None, **meta: Any) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Tear down any held resources (e.g. open tqdm bars)."""

    def moviepy_logger(self, stage: str = "compression"):
        """Return a proglog-compatible logger that funnels MoviePy progress
        events back into this reporter.

        Pass to MoviePy's `VideoFileClip.write_videofile(logger=...)`.
        Returns None if proglog is not installed (caller should fall back to
        MoviePy's default).
        """
        if ProgressBarLogger is None:
            return None
        return _ReporterProgressLogger(self, stage)


class NullReporter(Reporter):
    """No-op reporter used before init_reporter() has been called.

    Lets modules be imported safely without any side effects from progress
    instrumentation.
    """

    def stage(self, *a, **kw): pass
    def update(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def done(self, *a, **kw): pass


class HumanReporter(Reporter):
    """Pretty TTY output: stage banners + a single tqdm bar per stage."""

    _UNIT_TQDM = {
        "bytes": {"unit": "B", "unit_scale": True, "unit_divisor": 1024},
        "frames": {"unit": "frame"},
        "percent": {"unit": "%"},
    }

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self._current_stage: Optional[str] = None
        self._bar = None
        self._bar_total: Optional[int] = None

    @staticmethod
    def _label(stage: str, override: Optional[str]) -> str:
        return override or _STAGE_LABELS.get(stage, stage)

    def _print(self, msg: str) -> None:
        # Route through tqdm.write so an active bar isn't disrupted.
        if tqdm is not None and self._bar is not None:
            tqdm.write(msg)
        else:
            print(msg, flush=True)

    def _close_bar(self) -> None:
        if self._bar is not None:
            try:
                self._bar.close()
            finally:
                self._bar = None
                self._bar_total = None

    def stage(self, name: str, message: Optional[str] = None, **meta: Any) -> None:
        self._close_bar()
        label = self._label(name, message)
        # Append a useful single-line suffix for the most common metadata keys.
        suffix = ""
        if "filename" in meta:
            suffix = f": {meta['filename']}"
        elif "output" in meta:
            suffix = f": {meta['output']}"
        self._print(f"==> {label}{suffix}")
        self._current_stage = name

    def update(
        self,
        stage: str,
        current: int,
        total: int,
        unit: str,
        message: Optional[str] = None,
    ) -> None:
        if total is None or total <= 0 or tqdm is None:
            return
        if self._current_stage != stage:
            # First update for a stage that wasn't explicitly opened.
            self.stage(stage, message)
        if self._bar is None:
            kwargs = {
                "total": total,
                "leave": False,
                "desc": self._label(stage, None),
            }
            kwargs.update(self._UNIT_TQDM.get(unit, {}))
            self._bar = tqdm(**kwargs)
            self._bar_total = total
        elif self._bar_total != total:
            self._bar.total = total
            self._bar_total = total
        delta = current - self._bar.n
        if delta > 0:
            self._bar.update(delta)
        elif current < self._bar.n:
            # Allow bars to be re-seated (rare).
            self._bar.n = current
            self._bar.refresh()

    def info(self, message: str, **meta: Any) -> None:
        if self.quiet:
            return
        self._print(message)

    def warning(self, message: str, **meta: Any) -> None:
        self._print(f"Warning: {message}")

    def error(self, error_type: str, message: str, **meta: Any) -> None:
        self._close_bar()
        self._print(f"Error ({error_type}): {message}")

    def done(self, output: Optional[str] = None, **meta: Any) -> None:
        self._close_bar()
        if output:
            self._print(f"Done. Output: {output}")
        else:
            self._print("Done.")

    def close(self) -> None:
        self._close_bar()


class JsonReporter(Reporter):
    """Emit one JSON event per line on stdout.

    Progress events for the same stage are throttled to at most one every
    PROGRESS_THROTTLE_S seconds; stage / info / warning / error / done events
    are always emitted immediately. A throttled progress value is buffered and
    flushed on the next stage transition so the consumer always sees the final
    progress of a stage.

    Progress events for rate-meaningful units (``bytes`` and ``frames``)
    additionally carry a pre-formatted ``rate_label`` field (e.g.
    ``"12.4 MB/s"`` or ``"140 FPS"``). The rate is computed here at the
    source -- where the samples originate -- using an EMA whose smoothing
    factor matches tqdm's default of 0.3, so the JSON stream and the
    human-mode tqdm bar agree on how aggressively to smooth. Consumers
    (the web service today, anything else later) can render
    ``rate_label`` directly without recomputing.
    """

    PROGRESS_THROTTLE_S = 0.2

    # EMA factor for the per-stage rate calculation; the higher the
    # value the more responsive but jumpier the displayed rate. 0.3
    # mirrors tqdm's default so a human running the same job in TTY
    # mode sees an equivalently smoothed rate via tqdm's built-in bar.
    _RATE_SMOOTHING = 0.3

    def __init__(self):
        self._last_progress_emit = 0.0
        self._last_progress_stage: Optional[str] = None
        self._buffered_progress: Optional[dict] = None
        # Per-stage rate state: stage -> (prev_current, prev_ts, ema_rate).
        # We only look at the active stage's entry, so stale entries from
        # earlier stages cost a few bytes and nothing else; we don't clean
        # them up. Each JsonReporter instance is per-process so this resets
        # naturally across runs.
        self._rate_state: dict = {}

    def _emit(self, payload: dict) -> None:
        # `setdefault` so a sample-time ``ts`` we set in `update()` is
        # preserved here; only synthesize the timestamp for events that
        # didn't carry one (stage / info / warning / error / done).
        payload.setdefault("ts", time.time())
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _flush_buffered_progress(self) -> None:
        if self._buffered_progress is None:
            return
        payload = {"event": "progress", **self._buffered_progress}
        self._emit(payload)
        self._last_progress_emit = time.time()
        self._last_progress_stage = self._buffered_progress["stage"]
        self._buffered_progress = None

    def _compute_rate_label(
        self,
        stage: str,
        unit: str,
        current: int,
        ts: float,
    ) -> Optional[str]:
        """Update per-stage rate state and return a formatted label, or None.

        Only the two rate-meaningful units are tracked:
          * ``bytes`` -> formatted as ``"<x.x> MB/s"`` (decimal MB, matching
            disk-throughput convention rather than tqdm's binary MiB).
          * ``frames`` -> formatted as ``"<n> FPS"``.

        Other units (percent, empty, etc.) return ``None``. The first
        progress event in a stage also returns ``None`` because we need
        two samples to compute a delta. Subsequent samples are smoothed
        with an EMA (factor ``_RATE_SMOOTHING``) so the displayed value
        stays readable instead of jittering with every throttled update.
        """
        if unit not in ("bytes", "frames"):
            return None
        prev = self._rate_state.get(stage)
        if prev is None:
            # First sample in this stage; seed and emit no rate yet.
            self._rate_state[stage] = (current, ts, None)
            return None
        prev_current, prev_ts, prev_ema = prev
        delta_current = current - prev_current
        delta_ts = ts - prev_ts
        if delta_ts <= 0 or delta_current < 0:
            # Same instant or non-monotonic progress (rare, defensive).
            return None
        instant = delta_current / delta_ts
        ema = instant if prev_ema is None else (
            self._RATE_SMOOTHING * instant
            + (1.0 - self._RATE_SMOOTHING) * prev_ema
        )
        self._rate_state[stage] = (current, ts, ema)
        if unit == "bytes":
            return f"{ema / 1_000_000:.1f} MB/s"
        # unit == "frames"
        return f"{int(round(ema))} FPS"

    def stage(self, name: str, message: Optional[str] = None, **meta: Any) -> None:
        self._flush_buffered_progress()
        payload = {"event": "stage", "stage": name}
        if message is not None:
            payload["message"] = message
        if meta:
            payload.update(meta)
        self._emit(payload)

    def update(
        self,
        stage: str,
        current: int,
        total: int,
        unit: str,
        message: Optional[str] = None,
    ) -> None:
        if total is None or total <= 0:
            return
        clamped = min(int(current), int(total))

        # Capture sample-receipt time once. This is also what we embed
        # as `ts` in the emitted payload (via the buffered dict), so any
        # consumer reading `ts` sees the time we observed the sample
        # rather than the (potentially throttled) emit time.
        ts = time.time()

        progress = {
            "stage": stage,
            "current": clamped,
            "total": int(total),
            "unit": unit,
            "ts": ts,
        }
        if message is not None:
            progress["message"] = message
        rate_label = self._compute_rate_label(stage, unit, clamped, ts)
        if rate_label is not None:
            progress["rate_label"] = rate_label
        self._buffered_progress = progress

        is_terminal = clamped >= int(total)
        same_stage = stage == self._last_progress_stage
        throttled = same_stage and (ts - self._last_progress_emit) < self.PROGRESS_THROTTLE_S
        if throttled and not is_terminal:
            return
        self._flush_buffered_progress()

    def info(self, message: str, **meta: Any) -> None:
        payload = {"event": "info", "message": message}
        if meta:
            payload.update(meta)
        self._emit(payload)

    def warning(self, message: str, **meta: Any) -> None:
        payload = {"event": "warning", "message": message}
        if meta:
            payload.update(meta)
        self._emit(payload)

    def error(self, error_type: str, message: str, **meta: Any) -> None:
        self._flush_buffered_progress()
        payload = {"event": "error", "type": error_type, "message": message}
        if meta:
            payload.update(meta)
        self._emit(payload)

    def done(self, output: Optional[str] = None, **meta: Any) -> None:
        self._flush_buffered_progress()
        payload = {"event": "done"}
        if output is not None:
            payload["output"] = output
        if meta:
            payload.update(meta)
        self._emit(payload)


# --- MoviePy / proglog adapter ----------------------------------------------


if ProgressBarLogger is not None:

    class _ReporterProgressLogger(ProgressBarLogger):
        """proglog logger that funnels MoviePy bar updates into a Reporter.

        MoviePy may emit progress for several internal bars; this logger keeps
        the bar whose total is largest (typically the main video-frames bar)
        and forwards only that bar's updates to the reporter. Other proglog
        log messages are silenced (we have our own reporter output).
        """

        def __init__(self, reporter: Reporter, stage: str):
            super().__init__()
            self._reporter = reporter
            self._stage = stage
            self._primary_bar: Optional[str] = None
            self._primary_total: int = 0

        def callback(self, **changes):
            # Silence proglog's textual log messages.
            return

        def bars_callback(self, bar, attr, value, old_value=None):
            try:
                total = int(self.bars.get(bar, {}).get("total", 0) or 0)
            except Exception:
                total = 0
            if total <= 0:
                return
            # Lock onto the largest bar we've seen — typically MoviePy's main
            # frame bar. Smaller setup/teardown bars get ignored to avoid
            # jumpy progress.
            if self._primary_bar is None or total > self._primary_total:
                self._primary_bar = bar
                self._primary_total = total
            if bar != self._primary_bar:
                return
            if attr != "index":
                return
            try:
                current = int(value)
            except Exception:
                return
            self._reporter.update(
                self._stage, current, self._primary_total, unit="frames"
            )

else:  # pragma: no cover

    class _ReporterProgressLogger:  # type: ignore
        def __init__(self, *a, **kw):
            raise RuntimeError("proglog is not installed")


# --- Module-level singleton --------------------------------------------------


_reporter: Reporter = NullReporter()


def init_reporter(format: str = "auto", quiet: bool = False) -> Reporter:
    """Initialize and return the global reporter.

    Args:
        format: One of "human", "json", or "auto" (default). When "auto",
            the EXACQMAN_PROGRESS_FORMAT environment variable is consulted,
            then sys.stdout.isatty() decides.
        quiet: Suppress `info` messages in human mode. Ignored in JSON mode.
    """
    global _reporter
    resolved = _resolve_format(format)
    if resolved == "json":
        _reporter = JsonReporter()
    else:
        _reporter = HumanReporter(quiet=quiet)
    return _reporter


def get_reporter() -> Reporter:
    """Return the currently active reporter (NullReporter before init)."""
    return _reporter


def _resolve_format(format: str) -> str:
    if format and format != "auto":
        return format
    env = os.environ.get("EXACQMAN_PROGRESS_FORMAT", "").strip().lower()
    if env in {"human", "json"}:
        return env
    return "human" if sys.stdout.isatty() else "json"
