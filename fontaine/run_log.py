"""Timestamped activity logging (progress during gather/F2P) + optional transaction timings."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


_LOG = logging.getLogger("fontaine.progress")
_LOG.setLevel(logging.INFO)
_LOG.propagate = False

_TIMING = logging.getLogger("fontaine.timing")
_TIMING.setLevel(logging.INFO)
_TIMING.propagate = False


def configure_progress_logging(
    *,
    activity_log: Path | None = None,
    transaction_log: Path | None = None,
    log_terminal: bool = False,
) -> None:
    """
    Route activity lines to a log file and/or stderr.

    - ``activity_log``: if set, append UTF-8 lines to this path (parent dirs created).
    - ``transaction_log``: if set, append **TIMING** lines only (phase boundaries + durations).
    - ``log_terminal``: if True, also mirror activity lines to stderr (in addition to any file).
    """
    for h in list(_LOG.handlers):
        _LOG.removeHandler(h)
        h.close()

    for h in list(_TIMING.handlers):
        _TIMING.removeHandler(h)
        h.close()

    fmt = logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if activity_log is not None:
        path = activity_log.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        _LOG.addHandler(fh)

    if transaction_log is not None:
        tpath = transaction_log.expanduser().resolve()
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tf = logging.FileHandler(tpath, encoding="utf-8")
        tf.setFormatter(fmt)
        _TIMING.addHandler(tf)

    if log_terminal:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        _LOG.addHandler(sh)

    if not _LOG.handlers:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        _LOG.addHandler(sh)


def progress(msg: str, *args: object) -> None:
    """Log one activity line."""
    _LOG.info(msg, *args)


def _fmt_timing_extra(extra: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for k in sorted(extra.keys()):
        v = extra[k]
        if isinstance(v, bool):
            v = int(v)
        elif v is None:
            continue
        parts.append(f"{k}={v}")
    return parts


def format_duration_human(seconds: float) -> str:
    """
    Readable wall time: days, hours, minutes, and seconds with words and spacing.

    Omitted if zero (no leading ``0d 0h``). Trailing zero parts are dropped
    (e.g. exact minutes show no ``0 secs``). A lone sub-minute span is compact
    (``30s``); mixed units use forms like ``1 min 21 secs``, ``1 day 2 hours``.
    """
    if seconds < 0:
        seconds = 0.0
    ts = int(round(seconds))
    if ts == 0:
        if seconds > 0:
            return f"{seconds:.1f}s"
        return "0s"

    days, rem = divmod(ts, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append("1 day" if days == 1 else f"{days} days")
    if hours:
        parts.append("1 hour" if hours == 1 else f"{hours} hours")
    if minutes:
        parts.append("1 min" if minutes == 1 else f"{minutes} mins")
    if secs:
        if not parts:
            return f"{secs}s"
        parts.append("1 sec" if secs == 1 else f"{secs} secs")
    return " ".join(parts)


def _format_phase_duration(seconds: float) -> str:
    """Human-readable duration for console phase completion lines."""
    return format_duration_human(seconds)


def timing_event(phase: str, duration_ms: float | None = None, **extra: Any) -> None:
    """
    Emit one machine-friendly timing line (``TIMING phase=... duration_ms=...``).

    Written to ``--transaction-log`` when configured; always mirrored to the activity log / stderr
    when those handlers exist so a single ``fontaine-activity.log`` remains grep-friendly.
    """
    segments = ["TIMING", f"phase={phase}"]
    if duration_ms is not None:
        segments.append(f"duration_ms={duration_ms:.2f}")
    segments.extend(_fmt_timing_extra(extra))
    msg = " ".join(segments)

    if _TIMING.handlers:
        _TIMING.info(msg)
    if _LOG.handlers:
        _LOG.info(msg)
    elif not _TIMING.handlers:
        print(msg, file=sys.stderr)


@contextmanager
def timed_phase(phase: str, **extra: Any) -> Iterator[None]:
    """Measure wall time for a logical step and emit :func:`timing_event` on exit."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timing_event(phase, duration_ms=(time.perf_counter() - t0) * 1000, **extra)


class GatherPhaseTracker:
    """
    Prints numbered phase starts to **stderr** (always flushed) so runs don’t look hung when
    activity lines go only to ``--activity-log``. Wraps :func:`timed_phase` so TIMING lines are unchanged.
    """

    def __init__(self, phases: Sequence[tuple[str, str, str | None]]) -> None:
        self._phases = list(phases)
        self._ids = [p[0] for p in self._phases]
        self._labels = {p[0]: p[1] for p in self._phases}
        self._estimates = {p[0]: p[2] for p in self._phases}

    @contextmanager
    def step(self, phase_id: str, **timing_extra: Any) -> Iterator[None]:
        if phase_id not in self._ids:
            raise ValueError(f"unknown phase_id {phase_id!r}; expected one of {self._ids}")
        idx = self._ids.index(phase_id) + 1
        n = len(self._ids)
        label = self._labels[phase_id]
        est = self._estimates.get(phase_id)
        est_part = f" — typical duration {est}" if est else ""
        print(
            f"Starting phase {idx}/{n}: {label}{est_part}",
            file=sys.stderr,
            flush=True,
        )
        t0 = time.perf_counter()
        try:
            with timed_phase(phase_id, **timing_extra):
                yield
        finally:
            elapsed = time.perf_counter() - t0
            dur = _format_phase_duration(elapsed)
            print(
                f"Phase {idx}/{n} completed in {dur}",
                file=sys.stderr,
                flush=True,
            )
