"""Extract keyframes from a video into <workdir>/frames/.

Two extraction modes:
  uniform (default)   : evenly spaced by computed fps
  scene-change        : ffmpeg `select=gt(scene,T)` -- frames at moments of change

Optional post-process:
  --dedup             : drop frames whose perceptual hash (imagehash.phash) is within
                       DEDUP_THRESHOLD Hamming distance of an earlier keeper.
                       Renumbers remaining files sequentially.

Always emits per-frame timestamps in the JSON output so downstream tools
(report.py) can map without recomputing from fps -- essential when frames are
non-uniformly spaced.

Usage:
    python frames.py <video> <workdir> [--frames N] [--resolution W]
                                       [--start MM:SS] [--end MM:SS]
                                       [--scene-mode] [--scene-threshold 0.3]
                                       [--dedup] [--dedup-threshold 5]

Stdout: single JSON object: {frames_dir, frame_count, mode, timestamps, ...}
Stderr: structured events.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (  # noqa: E402
    ExitCode, atomic_path, die, emit, finalize,
    parse_time_spec, require_executable, window_duration,
)


PTS_TIME_RE = re.compile(r"pts_time:\s*([0-9]+\.?[0-9]*)")


def auto_frame_budget(duration: float) -> int:
    if duration <= 30: return 25
    if duration <= 60: return 25
    if duration <= 180: return 40
    if duration <= 600: return 60
    return 80


def _probe_total(ffprobe: str, video: Path) -> float:
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
             "--", str(video)],
            text=True,
        ).strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError) as e:
        die(ExitCode.IO_FAIL, f"ffprobe failed on {video}: {e}")


def extract_uniform(video: Path, staging: Path, fps: float, resolution: int,
                    start: float | None, end: float | None, ffmpeg: str) -> dict[str, float]:
    """Uniform sampling. Returns {filename: original_video_seconds}."""
    pattern = str(staging / "t_%03d.jpg")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None: cmd += ["-ss", f"{start}"]
    if end is not None: cmd += ["-to", f"{end}"]
    cmd += ["-i", str(video),
            "-vf", f"fps={fps},scale={resolution}:-1",
            "-q:v", "3", pattern]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        die(ExitCode.IO_FAIL, f"ffmpeg uniform extraction failed: {e}")
    frames = sorted(staging.glob("t_*.jpg"))
    offset = start or 0.0
    return {p.name: offset + (i / fps) for i, p in enumerate(frames)}


def extract_scene_change(video: Path, staging: Path, threshold: float, resolution: int,
                         start: float | None, end: float | None,
                         ffmpeg: str) -> dict[str, float]:
    """Scene-change extraction. Parses pts_time from showinfo stderr lines.

    Returns {filename: original_video_seconds}. Times are offset by `start`
    so they map to the original video timeline (showinfo emits 0-based when
    `-ss` is before `-i`).
    """
    pattern = str(staging / "t_%03d.jpg")
    cmd = [ffmpeg, "-hide_banner", "-y", "-loglevel", "info"]
    if start is not None: cmd += ["-ss", f"{start}"]
    if end is not None: cmd += ["-to", f"{end}"]
    # format=yuvj420p forces full-range YUV so the MJPEG encoder accepts
    # sources with non-full-range YUV (common in OBS / screen recordings).
    cmd += ["-i", str(video),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo,scale={resolution}:-1,format=yuvj420p",
            "-fps_mode", "vfr", "-q:v", "3", pattern]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(ExitCode.IO_FAIL, f"ffmpeg scene-change extraction failed",
            stderr_tail=proc.stderr[-300:])

    # Parse pts_time per emitted frame from showinfo (writes to stderr)
    pts_times = [float(m.group(1)) for m in PTS_TIME_RE.finditer(proc.stderr)]
    frames = sorted(staging.glob("t_*.jpg"))
    if len(pts_times) != len(frames):
        # Sometimes showinfo emits one less than frame count; align by truncation
        n = min(len(pts_times), len(frames))
        pts_times = pts_times[:n]
        frames = frames[:n]
    offset = start or 0.0
    return {p.name: offset + ts for p, ts in zip(frames, pts_times)}


def dedup_phash(staging: Path, timestamps: dict[str, float],
                threshold: int) -> tuple[dict[str, float], int]:
    """Drop near-duplicate frames (Hamming distance <= threshold), renumber sequentially.

    Returns (new_timestamps_by_filename, dropped_count).
    """
    try:
        from PIL import Image
        import imagehash
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "Pillow + imagehash required for --dedup. Install: pip install --user Pillow imagehash",
            dependency="imagehash")

    frames = sorted(staging.glob("t_*.jpg"))
    if len(frames) <= 1:
        return timestamps, 0

    keepers: list[Path] = [frames[0]]
    keeper_hashes = [imagehash.phash(Image.open(frames[0]))]
    dropped: list[Path] = []
    for f in frames[1:]:
        h = imagehash.phash(Image.open(f))
        if min(h - prev for prev in keeper_hashes) > threshold:
            keepers.append(f)
            keeper_hashes.append(h)
        else:
            dropped.append(f)

    # Delete dropped files
    for f in dropped:
        f.unlink(missing_ok=True)

    # Renumber keepers sequentially (t_001, t_002, ...) via two-step rename
    # (avoid overwriting in flight). Build new timestamp map keyed by new name.
    new_timestamps: dict[str, float] = {}
    # Temp rename phase
    temp_paths: list[tuple[Path, Path, float]] = []
    for i, f in enumerate(keepers, 1):
        ts = timestamps.get(f.name, 0.0)
        tmp = f.with_name(f"tmp_t_{i:03d}.jpg")
        f.rename(tmp)
        temp_paths.append((tmp, f.with_name(f"t_{i:03d}.jpg"), ts))
    # Final rename phase
    for tmp, final, ts in temp_paths:
        tmp.rename(final)
        new_timestamps[final.name] = ts
    return new_timestamps, len(dropped)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("workdir")
    ap.add_argument("--frames", type=int, default=0, help="override auto frame budget (uniform mode only)")
    ap.add_argument("--resolution", type=int, default=960)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--scene-mode", action="store_true",
                    help="use ffmpeg scene-change detection instead of uniform sampling")
    ap.add_argument("--scene-threshold", type=float, default=0.3,
                    help="scene-change sensitivity (0.1=very sensitive, 0.5=only major cuts). Default 0.3")
    ap.add_argument("--dedup", action="store_true",
                    help="drop near-duplicate frames via perceptual hash (requires Pillow + imagehash)")
    ap.add_argument("--dedup-threshold", type=int, default=5,
                    help="pHash Hamming distance threshold for dedup (default 5)")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        die(ExitCode.BAD_INPUT, f"video not found: {video}")
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    ffmpeg = require_executable("ffmpeg")
    ffprobe = require_executable("ffprobe")

    total = _probe_total(ffprobe, video)
    start = parse_time_spec(args.start)
    end = parse_time_spec(args.end)
    try:
        window = window_duration(start, end, total)
    except ValueError as e:
        die(ExitCode.BAD_INPUT, str(e))

    # Stage frames in a sibling dir, finalize atomically
    frames_dir = workdir / "frames"
    staging = atomic_path(frames_dir)
    staging.mkdir(parents=True, exist_ok=True)

    mode = "scene-change" if args.scene_mode else "uniform"
    emit("start", step="frames", mode=mode, window_seconds=round(window, 2),
         resolution=args.resolution, dedup=args.dedup)
    t0 = time.time()
    try:
        if args.scene_mode:
            timestamps = extract_scene_change(
                video, staging, args.scene_threshold, args.resolution, start, end, ffmpeg)
            # Fallback: if scene-change produced too few frames, fall back to uniform
            min_floor = max(8, auto_frame_budget(window) // 3)
            if len(timestamps) < min_floor:
                emit("warning", step="frames",
                     msg=f"scene-change produced only {len(timestamps)} frames; falling back to uniform",
                     fallback="uniform")
                # Clean staging and rerun uniform
                for f in staging.glob("t_*.jpg"):
                    f.unlink(missing_ok=True)
                frame_count = args.frames if args.frames > 0 else auto_frame_budget(window)
                fps = min(2.0, round(frame_count / window, 3)) or 0.5
                timestamps = extract_uniform(
                    video, staging, fps, args.resolution, start, end, ffmpeg)
                mode = "uniform-fallback"
        else:
            frame_count = args.frames if args.frames > 0 else auto_frame_budget(window)
            fps = min(2.0, round(frame_count / window, 3)) or 0.5
            timestamps = extract_uniform(
                video, staging, fps, args.resolution, start, end, ffmpeg)
    except KeyboardInterrupt:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if not timestamps:
        shutil.rmtree(staging, ignore_errors=True)
        die(ExitCode.IO_FAIL, "no frames extracted")

    dropped = 0
    if args.dedup:
        emit("start", step="dedup", before=len(timestamps), threshold=args.dedup_threshold)
        timestamps, dropped = dedup_phash(staging, timestamps, args.dedup_threshold)
        emit("complete", step="dedup", dropped=dropped, after=len(timestamps))

    # Finalize the staging dir into place
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    finalize(staging, frames_dir)

    emit("complete", step="frames", count=len(timestamps), mode=mode,
         duration_seconds=round(time.time() - t0, 2))

    result = {
        "frames_dir": str(frames_dir),
        "frame_count": len(timestamps),
        "mode": mode,
        "resolution": args.resolution,
        "window_start": start,
        "window_end": end,
        "window_seconds": round(window, 3),
        "timestamps_by_frame": {k: round(v, 3) for k, v in sorted(timestamps.items())},
        "dedup_dropped": dropped if args.dedup else None,
        "scene_threshold": args.scene_threshold if args.scene_mode else None,
    }
    print(json.dumps(result))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
