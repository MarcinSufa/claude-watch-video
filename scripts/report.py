"""Generate <workdir>/report.md and <workdir>/report.html -- evidence bundles.

Reads <workdir>/meta.json + transcript.md + frames/ and produces TWO documents
interleaving transcript paragraphs with their matching frame thumbnails:

  report.md   -- Markdown, frame refs as `![](frames/t_NNN.jpg)` relative paths.
                 Best for pasting into Jira comments / PR descriptions / wikis.
  report.html -- Self-contained HTML with base64-embedded frame thumbnails.
                 Best for viewing in any browser without workspace sandboxing
                 issues (VSCode preview blocks images from outside the
                 workspace; this file works anywhere).

These are *evidence only* -- what was on screen + what was said. The agent's
analysis (root cause, suggested fix) goes elsewhere.

Usage:
    python report.py <workdir> [--no-html]

Stdout: single JSON object with { "report_path", "html_path" }
"""
from __future__ import annotations

import argparse
import base64
import html
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


HTML_CSS = """
:root {
  color-scheme: light dark;
  --fg: #24292e;
  --bg: #ffffff;
  --muted: #6a737d;
  --accent: #0366d6;
  --rule: #e1e4e8;
  --code-bg: #f6f8fa;
  --quote-bg: #f6f8fa;
  --quote-border: #0366d6;
  --banner-bg: #fffbeb;
  --banner-border: #f9c513;
  --banner-fg: #735c0f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #c9d1d9;
    --bg: #0d1117;
    --muted: #8b949e;
    --accent: #58a6ff;
    --rule: #30363d;
    --code-bg: #161b22;
    --quote-bg: #161b22;
    --quote-border: #58a6ff;
    --banner-bg: #2d2410;
    --banner-border: #f0c419;
    --banner-fg: #d4a426;
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  max-width: 920px;
  margin: 2em auto;
  padding: 0 1.5em;
  line-height: 1.6;
  color: var(--fg);
  background: var(--bg);
}
h1 { font-size: 2em; border-bottom: 1px solid var(--rule); padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid var(--rule); padding-bottom: 0.3em; margin-top: 2em; }
h3 { font-size: 1.15em; color: var(--accent); margin-top: 2.5em; padding-top: 0.3em; border-top: 1px dashed var(--rule); }
ul { padding-left: 1.5em; }
li { margin: 0.2em 0; }
code { background: var(--code-bg); padding: 0.15em 0.4em; border-radius: 3px; font-size: 0.9em; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; border: 1px solid var(--rule); border-radius: 6px; display: block; margin: 1em 0; }
blockquote {
  border-left: 4px solid var(--quote-border);
  background: var(--quote-bg);
  padding: 0.6em 1em;
  margin: 0.5em 0;
  border-radius: 0 4px 4px 0;
}
blockquote p { margin: 0; }
.evidence-banner {
  border-left: 4px solid var(--banner-border);
  background: var(--banner-bg);
  color: var(--banner-fg);
  padding: 0.8em 1em;
  font-size: 0.95em;
  margin: 1em 0;
  border-radius: 0 4px 4px 0;
}
.evidence-banner strong { color: var(--banner-fg); }
.timeline-entry { margin: 0.5em 0 2em; }
.ocr-link { font-style: italic; color: var(--muted); }
.footer { color: var(--muted); font-size: 0.9em; text-align: center; margin-top: 4em; padding-top: 2em; border-top: 1px solid var(--rule); }
.silent-note { color: var(--muted); font-style: italic; }
""".strip()


def _embed_image_as_data_uri(frame_path: Path) -> str:
    """Read a JPG file and return a base64 data URI for inline <img src='...'>."""
    data = base64.b64encode(frame_path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def _h(text: str) -> str:
    """HTML-escape a string (for text content, not attribute values)."""
    return html.escape(text, quote=False)


def render_html(workdir: Path, meta: dict) -> str:
    """Generate a self-contained HTML evidence bundle (images embedded as base64)."""
    video = meta.get("video") or {}
    probe = meta.get("probe") or {}
    transcript = meta.get("transcript")
    frames_info = meta.get("frames") or {}
    timestamps_by_frame: dict[str, float] = frames_info.get("timestamps_by_frame", {})
    ocr_info = meta.get("ocr") or {}

    issue_key = video.get("issue_key")
    video_title = video.get("title")
    if issue_key:
        page_title = f"{issue_key} - {video.get('issue_summary', '')}".strip(" -")
    elif video_title:
        page_title = str(video_title)
    else:
        page_title = Path(str(video.get("path", "video"))).name

    parts: list[str] = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>{_h(page_title)}</title>",
        "<style>", HTML_CSS, "</style>",
        "</head>",
        "<body>",
        f"<h1>{_h(page_title)}</h1>",
        "<div class='evidence-banner'>",
        "<strong>Evidence bundle</strong> -- frames + narration captured by <code>/watch-video</code>.<br>",
        "Add your analysis above or below this section; the timeline below is auto-generated.",
    ]
    if ocr_info.get("path"):
        parts.append(
            f"<br><span class='ocr-link'>On-screen text from "
            f"{ocr_info.get('frames_with_text', 0)} frame(s) extracted to "
            f"<code>ocr.txt</code> -- grep it for specific labels/values.</span>"
        )
    parts.append("</div>")

    # ---- Source section ----
    parts.append("<h2>Source</h2><ul>")
    if issue_key:
        site = video.get("site")
        if site:
            parts.append(
                f"<li><strong>Issue:</strong> "
                f"<a href='https://{_h(site)}/browse/{_h(issue_key)}'>{_h(issue_key)}</a></li>"
            )
        else:
            parts.append(f"<li><strong>Issue:</strong> <code>{_h(issue_key)}</code></li>")
        parts.append(f"<li><strong>Attachment:</strong> <code>{_h(video.get('attachment_name', ''))}</code></li>")
    elif video.get("source_url"):
        parts.append(f"<li><strong>URL:</strong> <a href='{_h(video['source_url'])}'>{_h(video['source_url'])}</a></li>")
        if video_title:
            parts.append(f"<li><strong>Title:</strong> {_h(str(video_title))}</li>")
        if video.get("uploader"):
            parts.append(f"<li><strong>Uploader:</strong> {_h(str(video['uploader']))}</li>")
        if video.get("extractor"):
            parts.append(f"<li><strong>Source:</strong> {_h(str(video['extractor']))}</li>")
        if video.get("upload_date"):
            d = video["upload_date"]
            if len(d) == 8:
                parts.append(f"<li><strong>Uploaded:</strong> {d[:4]}-{d[4:6]}-{d[6:8]}</li>")
        parts.append(f"<li><strong>File:</strong> <code>{_h(Path(video.get('path', '')).name)}</code></li>")
    else:
        parts.append(f"<li><strong>File:</strong> <code>{_h(Path(video.get('path', '')).name)}</code></li>")
        parts.append(f"<li><strong>Source:</strong> {_h(str(video.get('source', 'unknown')))}</li>")

    if probe.get("duration"):
        dur = probe["duration"]
        m = int(dur // 60)
        s = dur % 60
        dur_str = f"{m}:{s:05.2f}" if m else f"{s:.1f}s"
        dim_str = f"{probe.get('width')}x{probe.get('height')}" if probe.get("width") else ""
        parts.append(f"<li><strong>Duration:</strong> {dur_str} ({_h(dim_str)})</li>")
    if probe.get("has_audio"):
        parts.append(
            f"<li><strong>Audio:</strong> {_h(str(probe.get('audio_codec')))} @ "
            f"{probe.get('audio_sample_rate')} Hz, mean volume "
            f"{probe.get('mean_volume_db')} dB</li>"
        )
    window = meta.get("window") or {}
    if window.get("start") or window.get("end"):
        parts.append(
            f"<li><strong>Window:</strong> {_h(str(window.get('start') or '0:00'))} - "
            f"{_h(str(window.get('end') or 'end'))}</li>"
        )
    if transcript:
        parts.append(
            f"<li><strong>Language:</strong> {_h(str(transcript.get('language')))} "
            f"(p={transcript.get('language_probability')})</li>"
        )
    parts.append("</ul>")

    # ---- Timeline ----
    frames = list_frames(workdir / "frames")
    if transcript:
        parts.append("<h2>Timeline</h2>")
        paragraphs = parse_prose_transcript(workdir / "transcript.md")
        if not paragraphs:
            parts.append("<p class='silent-note'>(no transcript paragraphs to render)</p>")
        else:
            for ts_seconds, text in paragraphs:
                frame = nearest_frame(ts_seconds, frames, timestamps_by_frame)
                ts_str = format_ts(ts_seconds)
                parts.append("<section class='timeline-entry'>")
                parts.append(f"<h3>{ts_str}</h3>")
                if frame is not None and frame.exists():
                    parts.append(
                        f"<img src='{_embed_image_as_data_uri(frame)}' alt='{_h(ts_str)}'>"
                    )
                parts.append(f"<blockquote><p>{_h(text)}</p></blockquote>")
                parts.append("</section>")
    else:
        skip_reason = meta.get("skipped_audio_reason", "no transcript")
        parts.append(f"<p class='silent-note'>Transcription skipped: {_h(str(skip_reason))}.</p>")
        parts.append("<h2>Timeline (silent video -- frames only)</h2>")
        for frame in frames:
            ts = float(timestamps_by_frame.get(frame.name, 0.0))
            ts_str = format_ts(ts)
            parts.append("<section class='timeline-entry'>")
            parts.append(f"<h3>{ts_str} - <code>{_h(frame.name)}</code></h3>")
            parts.append(
                f"<img src='{_embed_image_as_data_uri(frame)}' alt='{_h(ts_str)}'>"
            )
            parts.append("</section>")

    parts.append(
        "<p class='footer'><em>Generated by <code>/watch-video</code> skill "
        "&mdash; frames + faster-whisper transcript.</em></p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def generate(workdir: Path, *, write_html: bool = True) -> dict:
    meta_path = workdir / "meta.json"
    if not meta_path.exists():
        die(ExitCode.BAD_INPUT, f"meta.json not found at {meta_path} -- run watch_video.py first")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # ---- Markdown ----
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

    # ---- HTML (self-contained, base64-embedded images) ----
    html_path: Path | None = None
    if write_html:
        html_text = render_html(workdir, meta)
        html_path = workdir / "report.html"
        html_staging = atomic_path(html_path)
        html_staging.write_text(html_text, encoding="utf-8")
        finalize(html_staging, html_path)

    return {
        "report_path": str(report_path),
        "html_path": str(html_path) if html_path else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--no-html", action="store_true",
                    help="skip report.html generation (Markdown only). HTML output "
                         "embeds frame thumbnails as base64 so it works in any browser "
                         "without workspace sandboxing issues; pass this flag if you "
                         "don't need the HTML version.")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        die(ExitCode.BAD_INPUT, f"workdir not found: {workdir}")

    emit("start", step="report", workdir=str(workdir), write_html=(not args.no_html))
    t0 = time.time()
    result = generate(workdir, write_html=(not args.no_html))
    emit("complete", step="report", duration_seconds=round(time.time() - t0, 2),
         report_path=result["report_path"],
         html_path=result["html_path"])

    print(json.dumps(result))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
