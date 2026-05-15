"""Smart frame dedup -- perceptual hash with temporal + transcript-aware protection.

Reads <workdir>/meta.json (and transcript.md if present), drops near-duplicate
frames, and updates meta.json with the new frame inventory.

A frame is KEPT if any of these conditions hold:
  1. Its perceptual hash differs from every prior keeper by > --threshold bits.
  2. Time since the last keeper >= --min-interval seconds (temporal coverage --
     guarantees no big gaps where slow UI changes are mistakenly dedup'd).
  3. It lies within --protect-window seconds of a transcript paragraph start
     (the narrator explicitly described something at that moment).

Otherwise it is dropped. Remaining frames are renumbered sequentially and
the timestamps map is updated.

Usage:
    python dedup.py <workdir> [--threshold N] [--min-interval Ns]
                              [--protect-window Ns]

Stdout: single JSON object with before/after counts + protected timestamps.
Stderr: structured events.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


# Matches prose-mode paragraph prefix written by transcribe.py: "(_MM:SS_)"
PARA_TS_RE = re.compile(r"^\(_(\d+):(\d+)_\)", re.MULTILINE)


def load_meta(workdir: Path) -> dict:
    p = workdir / "meta.json"
    if not p.exists():
        die(ExitCode.BAD_INPUT, f"meta.json not found at {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def write_meta(workdir: Path, meta: dict) -> None:
    p = workdir / "meta.json"
    staging = atomic_path(p)
    staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    finalize(staging, p)


def protected_times_from_transcript(workdir: Path) -> list[float]:
    """Read transcript.md, return paragraph-start times in original-video seconds."""
    md = workdir / "transcript.md"
    if not md.exists():
        return []
    text = md.read_text(encoding="utf-8")
    times = []
    for m in PARA_TS_RE.finditer(text):
        mm, ss = int(m.group(1)), int(m.group(2))
        times.append(float(mm * 60 + ss))
    return times


def dedup(workdir: Path, threshold: int, min_interval: float,
          protect_window: float) -> dict:
    try:
        from PIL import Image
        import imagehash
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "Pillow + imagehash required. Install: pip install --user Pillow imagehash",
            dependency="imagehash")

    meta = load_meta(workdir)
    frames_info = meta.get("frames") or {}
    timestamps: dict[str, float] = dict(frames_info.get("timestamps_by_frame", {}))
    frames_dir = Path(frames_info.get("frames_dir", workdir / "frames"))

    frames = sorted(frames_dir.glob("t_*.jpg"))
    if len(frames) <= 1:
        emit("warning", step="dedup", msg="0 or 1 frame, nothing to dedup")
        return meta

    protected = protected_times_from_transcript(workdir)

    emit("start", step="dedup",
         before=len(frames), threshold=threshold,
         min_interval=min_interval, protect_window=protect_window,
         protected_timestamp_count=len(protected))

    keepers: list[Path] = []
    keeper_hashes: list = []
    last_keeper_time = -1e18
    dropped: list[Path] = []
    forced_temporal = 0
    forced_protected = 0

    for f in frames:
        ts = float(timestamps.get(f.name, 0.0))
        h = imagehash.phash(Image.open(f))
        if keeper_hashes:
            min_dist = min(int(h - prev) for prev in keeper_hashes)
            visually_distinct = min_dist > threshold
        else:
            visually_distinct = True
        temporal_force = (ts - last_keeper_time) >= min_interval
        protected_force = any(abs(ts - p) <= protect_window for p in protected)

        if visually_distinct or temporal_force or protected_force:
            keepers.append(f)
            keeper_hashes.append(h)
            last_keeper_time = ts
            if not visually_distinct:
                if temporal_force: forced_temporal += 1
                if protected_force: forced_protected += 1
        else:
            dropped.append(f)

    # Delete dropped files
    for f in dropped:
        f.unlink(missing_ok=True)

    # Renumber keepers sequentially (two-phase rename to avoid collisions)
    new_timestamps: dict[str, float] = {}
    temp_paths: list[tuple[Path, Path, float]] = []
    for i, f in enumerate(keepers, 1):
        ts = float(timestamps.get(f.name, 0.0))
        tmp = f.with_name(f"tmp_t_{i:03d}.jpg")
        f.rename(tmp)
        temp_paths.append((tmp, f.with_name(f"t_{i:03d}.jpg"), ts))
    for tmp, final, ts in temp_paths:
        tmp.rename(final)
        new_timestamps[final.name] = round(ts, 3)

    # Update meta.json
    meta["frames"] = dict(frames_info)  # don't mutate the original
    meta["frames"]["timestamps_by_frame"] = new_timestamps
    meta["frames"]["frame_count"] = len(new_timestamps)
    meta["frames"]["dedup"] = {
        "threshold": threshold,
        "min_interval": min_interval,
        "protect_window": protect_window,
        "before": len(frames),
        "after": len(new_timestamps),
        "dropped": len(dropped),
        "kept_by_temporal_protection": forced_temporal,
        "kept_by_transcript_protection": forced_protected,
        "protected_timestamps": protected,
    }
    write_meta(workdir, meta)

    emit("complete", step="dedup",
         before=len(frames), after=len(new_timestamps), dropped=len(dropped),
         kept_by_temporal=forced_temporal, kept_by_transcript=forced_protected)
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--threshold", type=int, default=5,
                    help="pHash Hamming distance threshold (default 5)")
    ap.add_argument("--min-interval", type=float, default=5.0,
                    help="minimum seconds between consecutive keepers (default 5.0)")
    ap.add_argument("--protect-window", type=float, default=1.5,
                    help="seconds around transcript paragraph timestamps where frames are protected (default 1.5)")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        die(ExitCode.BAD_INPUT, f"workdir not found: {workdir}")

    t0 = time.time()
    meta = dedup(workdir, args.threshold, args.min_interval, args.protect_window)
    dedup_info = (meta.get("frames", {}) or {}).get("dedup", {})

    print(json.dumps({
        "frame_count": meta.get("frames", {}).get("frame_count"),
        "dedup": dedup_info,
        "elapsed_seconds": round(time.time() - t0, 2),
    }))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
