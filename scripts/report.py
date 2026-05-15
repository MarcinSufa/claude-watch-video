"""Generate <workdir>/report.md -- an evidence-bundle Markdown document.

Reads <workdir>/meta.json + transcript.md + frames/ and produces a single
document interleaving transcript paragraphs with their matching frame
thumbnails. Designed to be paste-ready into Jira comments / PR descriptions /
bug reports.

This document is *evidence only* -- what was on screen + what was said. The
agent's analysis (root cause, suggested fix) goes elsewhere.

Usage:
    python report.py <workdir>

Stdout: single JSON object with { "report_path": "..." }
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


# Matches the prose-mode paragraph prefix written by transcribe.py: "(_MM:SS_)"
PARA_TS_RE = re.compile(r"^\(_(\d+):(\d+)_\)\s+(.+)$", re.DOTALL)


def parse_prose_transcript(md_path: Path) -> list[tuple[float, str]]:
    """Returns list of (start_seconds, paragraph_text) tuples."""
    if not md_path.exists():
        return []
    raw = md_path.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    out = []
    for p in paragraphs:
        m = PARA_TS_RE.match(p)
        if m:
            mm, ss, text = m.group(1), m.group(2), m.group(3).strip()
            out.append((int(mm) * 60 + int(ss), text))
    return out


def list_frames(frames_dir: Path) -> list[Path]:
    return sorted(frames_dir.glob("t_*.jpg"))


def nearest_frame(target_seconds: float, frames: list[Path],
                  timestamps_by_frame: dict[str, float]) -> Path | None:
    """Find the frame whose stored timestamp is closest to target_seconds.

    Uses the per-frame timestamp map written by frames.py, so this works
    consistently across uniform and scene-change extraction modes.
    """
    if not frames:
        return None
    return min(frames, key=lambda p: abs(timestamps_by_frame.get(p.name, 0.0) - target_seconds))


def frame_ts(frame: Path, timestamps_by_frame: dict[str, float]) -> float:
    return timestamps_by_frame.get(frame.name, 0.0)


def format_ts(seconds: float) -> str:
    mm = int(seconds // 60)
    ss = int(seconds % 60)
    return f"{mm:02d}:{ss:02d}"


def header_block(meta: dict) -> str:
    video = meta.get("video", {})
    probe = meta.get("probe", {})
    issue_key = video.get("issue_key")
    video_title = video.get("title")
    if issue_key:
        title = f"# {issue_key} — {video.get('issue_summary', '')}".strip()
    elif video_title:
        title = f"# {video_title}"
    else:
        title = f"# {Path(video.get('path', 'video')).name}"

    ocr_info = meta.get("ocr") or {}
    has_ocr = bool(ocr_info.get("path"))

    lines = [title, ""]
    lines.append("> **Evidence bundle** -- frames + narration captured by `/watch-video`. ")
    lines.append("> Add your analysis above or below this block; do not edit the timeline below.")
    if has_ocr:
        lines.append(f"> On-screen text from {ocr_info.get('frames_with_text', 0)} frame(s) extracted to `ocr.txt` -- grep it for specific labels/values seen in the UI.")
    lines.append("")
    lines.append("## Source")
    lines.append("")
    if issue_key:
        site = video.get("site")
        if site:
            lines.append(f"- **Issue:** [{issue_key}](https://{site}/browse/{issue_key})")
        else:
            lines.append(f"- **Issue:** `{issue_key}` (site unknown -- link omitted)")
        lines.append(f"- **Attachment:** `{video.get('attachment_name', '')}`")
    elif video.get("source_url"):
        # URL source (yt-dlp): show original URL, title, uploader
        lines.append(f"- **URL:** <{video['source_url']}>")
        if video_title:
            lines.append(f"- **Title:** {video_title}")
        if video.get("uploader"):
            lines.append(f"- **Uploader:** {video['uploader']}")
        if video.get("extractor"):
            lines.append(f"- **Source:** {video['extractor']}")
        if video.get("upload_date"):
            d = video["upload_date"]
            if len(d) == 8:
                lines.append(f"- **Uploaded:** {d[:4]}-{d[4:6]}-{d[6:8]}")
        lines.append(f"- **File:** `{Path(video.get('path', '')).name}`")
    else:
        lines.append(f"- **File:** `{Path(video.get('path', '')).name}`")
        lines.append(f"- **Source:** {video.get('source', 'unknown')}")
    lines.append(f"- **Duration:** {probe.get('duration', 0):.1f}s ({probe.get('width')}×{probe.get('height')})")
    if probe.get("has_audio"):
        lines.append(f"- **Audio:** {probe.get('audio_codec')} @ {probe.get('audio_sample_rate')} Hz, "
                     f"mean volume {probe.get('mean_volume_db')} dB")
    window = meta.get("window", {})
    if window.get("start") or window.get("end"):
        lines.append(f"- **Window:** {window.get('start') or '0:00'} → {window.get('end') or 'end'}")
    transcript = meta.get("transcript")
    if transcript:
        lines.append(f"- **Language:** {transcript.get('language')} "
                     f"(p={transcript.get('language_probability')})")
    lines.append("")
    return "\n".join(lines)


def silent_timeline(workdir: Path, meta: dict) -> str:
    """Render a frame-grid timeline when there's no transcript."""
    frames_info = meta.get("frames", {}) or {}
    timestamps_by_frame: dict[str, float] = frames_info.get("timestamps_by_frame", {})
    frames = list_frames(workdir / "frames")
    lines = ["## Timeline (silent video — frames only)", ""]
    for p in frames:
        ts = frame_ts(p, timestamps_by_frame)
        lines.append(f"### {format_ts(ts)} — `{p.name}`")
        lines.append("")
        lines.append(f"![{format_ts(ts)}](frames/{p.name})")
        lines.append("")
    return "\n".join(lines)


def transcript_timeline(workdir: Path, meta: dict) -> str:
    """Interleave prose paragraphs with their nearest matching frames."""
    frames_info = meta.get("frames", {}) or {}
    timestamps_by_frame: dict[str, float] = frames_info.get("timestamps_by_frame", {})

    md_path = workdir / "transcript.md"
    paragraphs = parse_prose_transcript(md_path)
    frames = list_frames(workdir / "frames")

    if not paragraphs:
        return "## Timeline\n\n_(no transcript paragraphs to render)_\n"

    lines = ["## Timeline", ""]
    for ts_seconds, text in paragraphs:
        # Paragraph timestamps in prose md are already in original-video time
        # (transcribe.py offset them by --start). Find nearest frame.
        frame = nearest_frame(ts_seconds, frames, timestamps_by_frame)
        lines.append(f"### {format_ts(ts_seconds)}")
        lines.append("")
        if frame is not None:
            lines.append(f"![{format_ts(ts_seconds)}](frames/{frame.name})")
            lines.append("")
        lines.append(f"> {text}")
        lines.append("")
    return "\n".join(lines)


def generate(workdir: Path) -> Path:
    meta_path = workdir / "meta.json"
    if not meta_path.exists():
        die(ExitCode.BAD_INPUT, f"meta.json not found at {meta_path} -- run watch_video.py first")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    parts = [header_block(meta)]
    if meta.get("transcript"):
        parts.append(transcript_timeline(workdir, meta))
    else:
        skip_reason = meta.get("skipped_audio_reason", "no transcript")
        parts.append(f"_Transcription skipped: {skip_reason}._\n")
        parts.append(silent_timeline(workdir, meta))

    parts.append("---\n_Generated by `/watch-video` skill — frames + faster-whisper transcript._")

    report_path = workdir / "report.md"
    staging = atomic_path(report_path)
    staging.write_text("\n".join(parts), encoding="utf-8")
    finalize(staging, report_path)
    return report_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        die(ExitCode.BAD_INPUT, f"workdir not found: {workdir}")

    emit("start", step="report", workdir=str(workdir))
    t0 = time.time()
    report_path = generate(workdir)
    emit("complete", step="report", duration_seconds=round(time.time() - t0, 2),
         output=str(report_path))

    print(json.dumps({"report_path": str(report_path)}))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
