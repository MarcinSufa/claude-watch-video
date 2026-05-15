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

# Module-level state for in-place progress bar rendering (TTY only).
# Tracks which step's progress line is currently "owning" the active line so
# we can overwrite it with \r instead of scrolling new lines for every update.
_progress_state: dict[str, Any] = {"active_step": None, "in_place": False}


def _format_pretty(event: str, payload: dict) -> str:
    """Format a structured event as a human-readable single line.
    Returns empty string for events not worth printing in verbose mode."""
    step = payload.get("step", "")
    ts_struct = time.localtime(payload.get("ts", time.time()))
    timestamp = time.strftime("%H:%M:%S", ts_struct)

    if event == "start":
        extras = []
        for k in ("url", "model", "language", "mode", "input_kind", "issue_key",
                  "filename", "size_bytes", "audio_bytes"):
            if k in payload:
                v = payload[k]
                if isinstance(v, int) and v > 1024 * 1024:
                    v = f"{v / (1024 * 1024):.1f} MB"
                extras.append(f"{k}={v}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"[{timestamp}] {step}: starting{suffix}"

    if event == "complete":
        dur = payload.get("duration_seconds")
        details = []
        for k in ("count", "frame_count", "segment_count", "frames_total",
                  "frames_with_text", "before", "after", "dropped"):
            if k in payload:
                details.append(f"{k}={payload[k]}")
        suffix = f" ({', '.join(details)})" if details else ""
        dur_str = f" in {dur}s" if dur is not None else ""
        return f"[{timestamp}] {step}: done{dur_str}{suffix}"

    if event == "progress":
        if "done" in payload and "total" in payload:
            done = payload["done"]
            total = payload["total"]
            pct = int(100 * done / total) if total else 0
            elapsed = payload.get("elapsed_seconds")
            elapsed_str = f" · elapsed {elapsed}s" if elapsed is not None else ""
            eta_str = ""
            if elapsed and done and total:
                est_total = elapsed * total / done
                eta = max(0, est_total - elapsed)
                eta_str = f" · est {eta:.0f}s left"
            return f"[{timestamp}] {step}: {done}/{total} ({pct}%){elapsed_str}{eta_str}"
        if "segments_done" in payload:
            sd = payload["segments_done"]
            audio_pos = payload.get("audio_position_seconds")
            elapsed = payload.get("elapsed_seconds", 0)
            audio_str = ""
            if audio_pos is not None:
                m = int(audio_pos // 60); s = int(audio_pos % 60)
                audio_str = f", audio at {m}:{s:02d}"
            return f"[{timestamp}] {step}: {sd} segments{audio_str} · elapsed {elapsed}s"
        if "bytes" in payload and "total" in payload:
            mb = payload["bytes"] / (1024 * 1024)
            total_mb = payload["total"] / (1024 * 1024)
            pct = int(100 * payload["bytes"] / payload["total"]) if payload["total"] else 0
            return f"[{timestamp}] {step}: {mb:.1f} MB / {total_mb:.1f} MB ({pct}%)"
        return f"[{timestamp}] {step}: progress"

    if event == "warning":
        return f"[{timestamp}] WARNING {step}: {payload.get('msg', '')}"
    if event == "error":
        return f"[{timestamp}] ERROR ({payload.get('exit_code')}) {step}: {payload.get('msg', '')}"
    if event == "cache_hit":
        return f"[{timestamp}] {step}: cache hit"
    return ""


def emit(event: str, **kwargs: Any) -> None:
    """Write a single JSON line to stderr (parent can parse with `for line in proc.stderr`).

    Convention:
      event="start" / "progress" / "complete" -- per-step lifecycle
      event="warning"                          -- non-fatal, work continues
      event="error"                            -- fatal, exit follows
      event="cache_hit"                        -- step skipped due to cache match

    If env var WATCH_VERBOSE is set, ALSO emit a human-readable pretty line.
    If WATCH_VERBOSE_TTY is set, progress events update the previous line in
    place (\\r overwrite) for the same step.
    """
    payload = {"ts": round(time.time(), 3), "event": event}
    payload.update(kwargs)
    line = json.dumps(payload, separators=(",", ":"), default=str)
    print(line, file=sys.stderr, flush=True)

    if os.environ.get("WATCH_VERBOSE"):
        pretty = _format_pretty(event, payload)
        if not pretty:
            return
        in_place = bool(os.environ.get("WATCH_VERBOSE_TTY"))
        step = payload.get("step")
        if in_place and event == "progress":
            # Overwrite previous line for the SAME step's progress events
            sys.stderr.write(f"\r\033[K{pretty}")
            sys.stderr.flush()
            _progress_state["active_step"] = step
            _progress_state["in_place"] = True
        else:
            # Terminate any active in-place line with a newline first
            if _progress_state["in_place"]:
                sys.stderr.write("\n")
                _progress_state["in_place"] = False
                _progress_state["active_step"] = None
            print(pretty, file=sys.stderr, flush=True)


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
