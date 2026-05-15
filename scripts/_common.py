"""Shared helpers for the watch-video skill scripts.

Provides:
- Standardized exit codes
- Structured JSON-line stderr events (so the orchestrator can parse progress)
- Time-string parsing for --start/--end flags
- Atomic-rename helper for crash-safe writes
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn


# ---- Exit codes -----------------------------------------------------------

class ExitCode:
    OK = 0
    BAD_INPUT = 2          # missing arg, file not found, invalid value
    MISSING_DEP = 3        # ffmpeg / faster_whisper / yt_dlp not on PATH
    AUTH_FAIL = 4          # Atlassian 401
    AMBIGUOUS = 5          # multiple matches, caller must disambiguate
    IO_FAIL = 6            # disk full, write failed, corrupt download
    TIMEOUT = 7            # network or subprocess timeout


# ---- Structured stderr events --------------------------------------------

def emit(event: str, **kwargs: Any) -> None:
    """Write a single JSON line to stderr (parent can parse with `for line in proc.stderr`).

    Convention:
      event="start" / "progress" / "complete" --per-step lifecycle
      event="warning"                          --non-fatal, work continues
      event="error"                            --fatal, exit follows
    Always include: step (which sub-task), and any relevant counters.
    """
    payload = {"ts": round(time.time(), 3), "event": event}
    payload.update(kwargs)
    line = json.dumps(payload, separators=(",", ":"), default=str)
    print(line, file=sys.stderr, flush=True)


def die(code: int, msg: str, **extra: Any) -> NoReturn:
    """Emit an error event and exit with the given code. Never returns."""
    emit("error", exit_code=code, msg=msg, **extra)
    sys.exit(code)


# ---- Time-window parsing --------------------------------------------------

def parse_time_spec(s: str | None) -> float | None:
    """Parse 'MM:SS', 'HH:MM:SS', or raw seconds → float seconds. None passes through."""
    if s is None or s == "":
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        raise ValueError(f"invalid time spec: {s!r}")
    return float(s)


def window_duration(start: float | None, end: float | None, total: float) -> float:
    """Effective duration after applying --start/--end clamps to total video duration."""
    s = start or 0.0
    e = end if end is not None else total
    e = min(e, total)
    if e <= s:
        raise ValueError(f"empty window: start={s} end={e} (total={total})")
    return e - s


# ---- Atomic writes --------------------------------------------------------

def atomic_path(dest: Path) -> Path:
    """Return a sibling .partial path for staged writes; rename onto dest at end.

    Preserves the file extension so format-sniffing tools (e.g. ffmpeg picking a
    muxer from the output filename) still work on the staging path. For e.g.
    `audio.wav` returns `audio.partial-abc12345.wav`."""
    return dest.with_name(f"{dest.stem}.partial-{uuid.uuid4().hex[:8]}{dest.suffix}")


def finalize(staging: Path, dest: Path) -> None:
    """Atomically rename staging -> dest, replacing dest if it exists.

    Handles the Windows quirk where rename fails if dest exists.
    """
    if dest.exists():
        dest.unlink()
    os.replace(staging, dest)


# ---- Dependency probing --------------------------------------------------

def require_executable(name: str) -> str:
    """Return absolute path to `name` on PATH, or die with MISSING_DEP."""
    import shutil
    path = shutil.which(name)
    if not path:
        die(
            ExitCode.MISSING_DEP,
            f"{name} not found on PATH",
            dependency=name,
        )
    return path  # type: ignore[return-value]
