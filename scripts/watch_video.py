"""Top-level orchestrator for the watch-video skill.

Resolves input → probes → extracts frames → (optionally) transcribes →
writes <workdir>/meta.json as the durable contract for the agent.

Usage:
    python watch_video.py <input> [flags]

<input> can be:
    - A local path (e.g. C:\\path\\video.mp4)
    - A public URL (https://youtu.be/..., Loom, Vimeo, TikTok, etc.)
    - A Jira key (e.g. PROJ-1234)
    - A Jira issue URL (https://*.atlassian.net/browse/<KEY>)
    - The literal "auto" to grab newest video from ~/Downloads/

Stdout: meta.json contents (single JSON object).
Stderr: structured events from each step.
Exit codes: as defined in _common.ExitCode.
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
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


SCRIPTS_DIR = Path(__file__).parent
JIRA_KEY_RE = re.compile(r"^[A-Z]{2,10}-\d+$")
JIRA_URL_RE = re.compile(r"https?://[^/]+/browse/([A-Z]{2,10}-\d+)")
DEFAULT_WORKDIR_ROOT = Path("c:/tmp") if sys.platform == "win32" else Path("/tmp")


# ---- Input dispatch -------------------------------------------------------

def classify_input(raw: str) -> tuple[str, str]:
    """Return (kind, normalized_value)."""
    if raw == "auto":
        return "auto", ""
    if JIRA_KEY_RE.match(raw):
        return "jira", raw
    m = JIRA_URL_RE.match(raw)
    if m:
        return "jira", m.group(1)
    if raw.startswith(("http://", "https://")):
        return "url", raw
    # Otherwise assume local path
    return "path", raw


def slugify(text: str, max_len: int = 60) -> str:
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower()
    return out[:max_len] or "video"


def default_workdir(kind: str, value: str) -> Path:
    if kind == "jira":
        slug = f"watch-{value.lower()}"
    elif kind == "path":
        slug = f"watch-{slugify(Path(value).stem)}"
    elif kind == "url":
        slug = f"watch-{slugify(value.rsplit('/', 1)[-1] or 'url')}"
    else:
        slug = f"watch-{int(time.time())}"
    return DEFAULT_WORKDIR_ROOT / slug


# ---- Sub-script invocation -----------------------------------------------

def run_step(name: str, cmd: list[str]) -> dict:
    """Run a sub-script. Stream its stderr (already JSON-line events) through.
    Parse its single-JSON-object stdout. Propagate exit code on failure.

    On non-zero exits we ALSO forward stdout -- this matters for ExitCode.AMBIGUOUS
    where the sub-script prints a candidates JSON the agent needs to see.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    if proc.returncode != 0:
        # Forward stdout so AMBIGUOUS candidates and similar payloads reach the agent
        if proc.stdout:
            sys.stdout.write(proc.stdout)
            sys.stdout.flush()
        sys.exit(proc.returncode)
    if not proc.stdout.strip():
        die(ExitCode.IO_FAIL, f"step {name} produced no output")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def fetch(kind: str, value: str, workdir: Path,
          attachment_id: str | None, credentials: str | None,
          since_seconds: int) -> dict:
    cmd = [sys.executable, str(SCRIPTS_DIR / "fetch.py"), str(workdir)]
    if kind == "jira":
        cmd += ["--jira-key", value]
        if attachment_id:
            cmd += ["--attachment-id", attachment_id]
        if credentials:
            cmd += ["--credentials", credentials]
    elif kind == "url":
        cmd += ["--url", value]
    elif kind == "path":
        cmd += ["--path", value]
    else:  # auto
        cmd += ["--auto-downloads", "--since-seconds", str(since_seconds)]
    return run_step("fetch", cmd)


def probe(video: Path) -> dict:
    return run_step("probe", [sys.executable, str(SCRIPTS_DIR / "probe.py"), str(video)])


def extract_frames(video: Path, workdir: Path,
                   frames: int, resolution: int,
                   start: str | None, end: str | None,
                   scene_mode: bool, scene_threshold: float) -> dict:
    """Note: dedup happens in a separate post-transcribe step (see smart_dedup)
    so the deduper can use transcript paragraph timestamps as protection."""
    cmd = [sys.executable, str(SCRIPTS_DIR / "frames.py"), str(video), str(workdir),
           "--resolution", str(resolution)]
    if frames > 0:
        cmd += ["--frames", str(frames)]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    if scene_mode:
        cmd += ["--scene-mode", "--scene-threshold", str(scene_threshold)]
    return run_step("frames", cmd)


def smart_dedup(workdir: Path, threshold: int,
                min_interval: float, protect_window: float) -> dict:
    return run_step("dedup",
                    [sys.executable, str(SCRIPTS_DIR / "dedup.py"), str(workdir),
                     "--threshold", str(threshold),
                     "--min-interval", str(min_interval),
                     "--protect-window", str(protect_window)])


def ocr(workdir: Path, lang: str, min_text_length: int) -> dict:
    return run_step("ocr",
                    [sys.executable, str(SCRIPTS_DIR / "ocr.py"), str(workdir),
                     "--lang", lang,
                     "--min-text-length", str(min_text_length)])


def transcribe(video: Path, workdir: Path,
               model: str | None, lang: str | None,
               start: str | None, end: str | None,
               whisper: str, whisper_api_key: str | None,
               whisper_credentials: str | None) -> dict:
    cmd = [sys.executable, str(SCRIPTS_DIR / "transcribe.py"), str(workdir),
           "--video", str(video), "--whisper", whisper]
    if model:
        cmd += ["--model", model]
    if lang:
        cmd += ["--lang", lang]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    if whisper_api_key:
        cmd += ["--whisper-api-key", whisper_api_key]
    if whisper_credentials:
        cmd += ["--whisper-credentials", whisper_credentials]
    return run_step("transcribe", cmd)


def report(workdir: Path) -> dict:
    return run_step("report",
                    [sys.executable, str(SCRIPTS_DIR / "report.py"), str(workdir)])


# ---- Main ----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Watch a video: frames + transcript for Claude to consume.")
    ap.add_argument("input", help="path, URL, Jira key, Jira URL, or 'auto'")
    ap.add_argument("--workdir",
                    help="override workdir (default: c:/tmp/watch-<slug>/)")
    # Fetch options
    ap.add_argument("--attachment-id",
                    help="(Jira mode) disambiguate multiple video attachments")
    ap.add_argument("--credentials", help="(Jira mode) path to credentials JSON")
    ap.add_argument("--since-seconds", type=int, default=300,
                    help="(auto mode) max age of Downloads file")
    # Frame options
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--resolution", type=int, default=960)
    ap.add_argument("--scene-mode", action="store_true",
                    help="extract frames at scene changes (better token efficiency on long videos)")
    ap.add_argument("--scene-threshold", type=float, default=0.3)
    ap.add_argument("--dedup", action="store_true",
                    help="drop near-duplicate frames via pHash, AFTER transcribe so paragraph timestamps protect important moments")
    ap.add_argument("--dedup-threshold", type=int, default=5,
                    help="pHash Hamming distance for dedup (default 5)")
    ap.add_argument("--dedup-min-interval", type=float, default=5.0,
                    help="minimum seconds between consecutive dedup keepers (default 5.0)")
    ap.add_argument("--dedup-protect-window", type=float, default=1.5,
                    help="seconds around transcript paragraph timestamps where frames are kept regardless of pHash similarity (default 1.5)")
    # OCR options
    ap.add_argument("--ocr", action="store_true",
                    help="run Tesseract OCR on kept frames and write ocr.txt. "
                         "Best for UI bug videos -- lets you grep on-screen text instead of "
                         "re-reading each image. Requires Tesseract + pytesseract installed.")
    ap.add_argument("--ocr-lang", default="eng",
                    help="Tesseract language(s), e.g. 'eng', 'pol', 'eng+pol' (default 'eng')")
    ap.add_argument("--ocr-min-text-length", type=int, default=10,
                    help="skip frames with fewer than N non-whitespace chars of OCR text (default 10)")
    # Audio options
    ap.add_argument("--no-audio", action="store_true",
                    help="skip transcription even if audio is present")
    ap.add_argument("--model", help="whisper model id (provider-specific; see SKILL.md)")
    ap.add_argument("--lang", help="ISO language code or 'auto'")
    ap.add_argument("--whisper", choices=("local", "groq", "openai"), default="local",
                    help="transcription provider. Default 'local' = faster-whisper offline. "
                         "'groq' / 'openai' use hosted APIs (need key, ~10x faster cold-start).")
    ap.add_argument("--whisper-api-key", default=None,
                    help="API key for hosted providers. WARNING: visible in shell history "
                         "and process listings; do not use on shared machines or recorded "
                         "sessions. Prefer env var or credentials file.")
    ap.add_argument("--whisper-credentials", default=None,
                    help="credentials JSON path (default: ~/.watch-video/credentials.json)")
    # Window options (applied to BOTH frames and audio)
    ap.add_argument("--start", help="window start MM:SS")
    ap.add_argument("--end", help="window end MM:SS")
    # Report options
    ap.add_argument("--no-report", action="store_true",
                    help="skip report.md generation (faster, no evidence bundle)")
    args = ap.parse_args()

    overall_t0 = time.time()
    kind, value = classify_input(args.input)
    workdir = Path(args.workdir).resolve() if args.workdir else default_workdir(kind, value)
    workdir.mkdir(parents=True, exist_ok=True)
    emit("start", step="orchestrator", workdir=str(workdir), input_kind=kind)

    # 1. Fetch input → local path
    fetch_info = fetch(kind, value, workdir,
                       args.attachment_id, args.credentials, args.since_seconds)
    video = Path(fetch_info["path"])

    # 2. Probe
    probe_info = probe(video)

    # 3. Frames
    frames_info = extract_frames(video, workdir,
                                  args.frames, args.resolution,
                                  args.start, args.end,
                                  args.scene_mode, args.scene_threshold)

    # 4. Transcribe (optional)
    transcribe_info: dict | None = None
    skipped_audio_reason: str | None = None
    if args.no_audio:
        skipped_audio_reason = "disabled via --no-audio"
    elif not probe_info["has_audio"]:
        skipped_audio_reason = "no audio stream"
    elif probe_info["is_silent"]:
        skipped_audio_reason = f"silent track (mean_volume={probe_info['mean_volume_db']} dB)"
    else:
        transcribe_info = transcribe(video, workdir, args.model, args.lang,
                                     args.start, args.end,
                                     args.whisper, args.whisper_api_key,
                                     args.whisper_credentials)

    # 5. Write meta.json (atomic) -- needed before dedup/report.py read it
    meta = {
        "schema_version": 2,
        "workdir": str(workdir),
        "input": {"raw": args.input, "kind": kind, "value": value},
        "video": fetch_info,
        "probe": probe_info,
        "frames": frames_info,
        "transcript": transcribe_info,
        "skipped_audio_reason": skipped_audio_reason,
        "window": {"start": args.start, "end": args.end},
        "report": None,
        "generated_at": int(time.time()),
        "elapsed_seconds": round(time.time() - overall_t0, 2),
    }
    meta_path = workdir / "meta.json"
    staging = atomic_path(meta_path)
    staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    finalize(staging, meta_path)

    # 6. Smart dedup (transcript-aware) -- runs after transcribe so paragraph
    # timestamps protect narrated moments. dedup.py updates meta.json in place.
    if args.dedup:
        smart_dedup(workdir,
                    args.dedup_threshold,
                    args.dedup_min_interval,
                    args.dedup_protect_window)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # 7. OCR (optional) -- after dedup so we only OCR the kept frames.
    # ocr.py updates meta.json in place with the `ocr` block.
    if args.ocr:
        ocr(workdir, args.ocr_lang, args.ocr_min_text_length)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # 8. Generate evidence bundle (report.md) -- after dedup + OCR so it
    # reflects the final frame inventory and links to ocr.txt.
    if not args.no_report:
        report_info = report(workdir)
        meta["report"] = report_info
        staging = atomic_path(meta_path)
        staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        finalize(staging, meta_path)

    meta["elapsed_seconds"] = round(time.time() - overall_t0, 2)
    emit("complete", step="orchestrator",
         duration_seconds=meta["elapsed_seconds"],
         meta_path=str(meta_path))
    print(json.dumps(meta, indent=2))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
