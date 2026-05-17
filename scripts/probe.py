"""Probe a video file with ffprobe, emit metadata as JSON on stdout.

Replaces probe.ps1. Cross-platform (Mac/Linux/Win).

Usage:
    python probe.py <video-path>

Stdout: single JSON object with duration, dimensions, codecs, audio-volume info, is_silent.
Stderr: structured JSON events.
Exit codes: see _common.ExitCode.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Make scripts/ importable when invoked directly
sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, die, emit, require_executable  # noqa: E402


SILENT_THRESHOLD_DB = -50.0


def ffprobe_meta(video: Path, ffprobe: str) -> dict:
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_entries", "format=duration,size,bit_rate",
                "-show_entries", "stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
                "--", str(video),
            ],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        die(ExitCode.IO_FAIL,
            f"ffprobe failed on {video} (likely corrupt or unsupported)",
            stderr_tail=(e.stderr or "").strip()[-300:])
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        die(ExitCode.IO_FAIL, f"ffprobe returned invalid JSON: {e}")


def detect_mean_volume(video: Path, ffmpeg: str) -> float | None:
    """Returns mean_volume in dB, or None if no audio stream or detection failed."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(video), "-af", "volumedetect",
         "-vn", "-sn", "-dn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # ffmpeg writes volumedetect info to stderr
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    return float(m.group(1)) if m else None


def run_inproc(video: Path) -> dict:
    """Pure function for in-process invocation from watch_video.py.

    Same behavior as main() but returns the result dict directly instead of
    serializing to stdout. Used in MCP context (see watch_video.py's
    _STEP_LOGS_DIR + WATCH_VIDEO_NO_PIPE notes) to avoid the per-subprocess
    Defender scan tax on Windows -- ~5-20 seconds saved per step. Errors
    still raise SystemExit via die(); the caller is expected to catch and
    surface them.
    """
    video = video.resolve()
    if not video.exists():
        die(ExitCode.BAD_INPUT, f"video not found: {video}")

    ffprobe = require_executable("ffprobe")
    ffmpeg = require_executable("ffmpeg")

    emit("start", step="probe", video=str(video))
    t0 = time.time()
    meta = ffprobe_meta(video, ffprobe)

    video_stream = next((s for s in meta["streams"] if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in meta["streams"] if s["codec_type"] == "audio"), None)
    duration = float(meta["format"]["duration"])

    mean_vol: float | None = None
    if audio_stream is not None:
        mean_vol = detect_mean_volume(video, ffmpeg)
        if mean_vol is None:
            emit("warning", step="probe",
                 msg="audio stream present but volumedetect could not extract mean_volume; "
                     "transcription will proceed (not treated as silent)")

    # is_silent is ONLY true when we positively know it's below threshold.
    # If there's no audio stream at all -> silent. If there's audio but volumedetect
    # failed -> NOT silent (let transcription decide), so we don't skip real audio
    # because of a probe glitch.
    if audio_stream is None:
        is_silent = True
    elif mean_vol is None:
        is_silent = False  # unknown volume -> treat as not silent
    else:
        is_silent = mean_vol < SILENT_THRESHOLD_DB

    result = {
        "path": str(video),
        "duration": duration,
        "size_bytes": int(meta["format"].get("size", 0)),
        "bit_rate": int(meta["format"].get("bit_rate", 0) or 0),
        "width": video_stream and int(video_stream["width"]),
        "height": video_stream and int(video_stream["height"]),
        "video_codec": video_stream and video_stream["codec_name"],
        "has_audio": audio_stream is not None,
        "audio_codec": audio_stream and audio_stream["codec_name"],
        "audio_channels": audio_stream and int(audio_stream.get("channels", 0)),
        "audio_sample_rate": audio_stream and int(audio_stream.get("sample_rate", 0)),
        "mean_volume_db": mean_vol,
        "is_silent": is_silent,
    }

    emit("complete", step="probe", duration_seconds=round(time.time() - t0, 2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    args = ap.parse_args()
    result = run_inproc(Path(args.video))
    print(json.dumps(result))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
