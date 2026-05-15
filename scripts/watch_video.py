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
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402
from _cache import (  # noqa: E402
    dir_fingerprint, file_fingerprint, get_cache, invalidate_downstream,
    is_cached, record_step, step_fingerprint,
)


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
    """Run a sub-script. STREAM its stderr to our stderr line-by-line so users
    see progress events in real time. Collect stdout in full (we need to parse
    it as JSON at the end).

    Propagate exit code on failure. On non-zero exits we also forward stdout
    (needed for ExitCode.AMBIGUOUS where stdout carries the candidates JSON).
    """
    import threading
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    stderr_thread = threading.Thread(target=_pump_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None
    stdout_data = proc.stdout.read()
    proc.wait()
    stderr_thread.join(timeout=2)

    if proc.returncode != 0:
        if stdout_data:
            sys.stdout.write(stdout_data)
            sys.stdout.flush()
        sys.exit(proc.returncode)
    if not stdout_data.strip():
        die(ExitCode.IO_FAIL, f"step {name} produced no output")
    return json.loads(stdout_data.strip().splitlines()[-1])


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


def report(workdir: Path, *, no_html: bool = False, no_docx: bool = False) -> dict:
    cmd = [sys.executable, str(SCRIPTS_DIR / "report.py"), str(workdir)]
    if no_html:
        cmd.append("--no-html")
    if no_docx:
        cmd.append("--no-docx")
    return run_step("report", cmd)


def run_highlights(workdir: Path, prompt: str, max_n: int | None,
                   model: str | None, api_key: str | None,
                   credentials: str | None) -> dict:
    cmd = [sys.executable, str(SCRIPTS_DIR / "highlights.py"), str(workdir),
           "--prompt", prompt]
    if max_n is not None:
        cmd += ["--max-n", str(max_n)]
    if model:
        cmd += ["--model", model]
    if api_key:
        cmd += ["--anthropic-api-key", api_key]
    if credentials:
        cmd += ["--credentials", credentials]
    return run_step("highlights", cmd)


def post_to_jira(workdir: Path, jira_key: str | None,
                 dry_run: bool, yes: bool,
                 credentials: str | None,
                 no_embed_images: bool = False,
                 style: str | None = None,
                 summary_key_frames: int | None = None) -> dict:
    cmd = [sys.executable, str(SCRIPTS_DIR / "post_to_jira.py"), str(workdir)]
    if jira_key:
        cmd += ["--jira-key", jira_key]
    if dry_run:
        cmd += ["--dry-run"]
    if yes:
        cmd += ["--yes"]
    if credentials:
        cmd += ["--credentials", credentials]
    if no_embed_images:
        cmd += ["--no-embed-images"]
    if style:
        cmd += ["--style", style]
    if summary_key_frames is not None:
        cmd += ["--summary-key-frames", str(summary_key_frames)]
    return run_step("post_to_jira", cmd)


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
    ap.add_argument("--no-html", action="store_true",
                    help="skip report.html generation (Markdown + DOCX still produced)")
    ap.add_argument("--no-docx", action="store_true",
                    help="skip report.docx generation. Useful when python-docx is "
                         "not installed -- the report step otherwise emits a "
                         "warning and continues.")
    # Progress display
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print human-readable progress lines to stderr in addition "
                         "to the JSON event lines. On a TTY, progress events update "
                         "in place (carriage-return overwrite) for the active step.")
    # Cache options
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the per-step cache; re-run every step from scratch")
    ap.add_argument("--force-step", default=None,
                    help="comma-separated step names to force-rerun "
                         "(fetch/probe/frames/transcribe/dedup/ocr/report). "
                         "Downstream steps are invalidated automatically.")
    # Jira posting (OPT-IN ONLY -- never default on)
    ap.add_argument("--post-to-jira", action="store_true",
                    help="POST the generated report.md as a comment on the source "
                         "Jira ticket. Requires explicit user authorization. Adds a "
                         "confirmation prompt unless --post-to-jira-yes is also given. "
                         "Idempotency-checked: won't double-post a prior /watch-video analysis. "
                         "Does NOT default on; the agent must never silently enable this.")
    ap.add_argument("--post-to-jira-yes", action="store_true",
                    help="skip the interactive confirmation prompt for --post-to-jira "
                         "(use only when the user has explicitly pre-authorized).")
    ap.add_argument("--post-to-jira-dry-run", action="store_true",
                    help="with --post-to-jira: print the comment preview but DO NOT post")
    ap.add_argument("--post-to-jira-no-embed-images", action="store_true",
                    help="with --post-to-jira: skip image embedding (text refs only)")
    ap.add_argument("--post-to-jira-style", choices=("collapsed", "inline", "summary"),
                    default=None,
                    help="comment layout: 'collapsed' (default) wraps Timeline in an ADF "
                         "expand panel; 'inline' shows full Timeline expanded (legacy); "
                         "'summary' posts N key moments and attaches report.html as a "
                         "downloadable artifact.")
    ap.add_argument("--post-to-jira-summary-key-frames", type=int, default=None,
                    help="--post-to-jira-style summary only: how many key timeline "
                         "moments to include (default 3, evenly distributed)")
    # Intelligent highlights (LLM-driven moment selection)
    ap.add_argument("--highlights-prompt", default=None,
                    help="enable LLM-driven highlight selection. Describes what to "
                         "look for, e.g. 'highlight only bug-related parts'. Requires "
                         "ANTHROPIC_API_KEY. Runs highlights.py before posting; when "
                         "summary mode posts, picks come from the LLM instead of even "
                         "distribution.")
    ap.add_argument("--highlights-max-n", type=int, default=None,
                    help="max number of highlights the LLM is allowed to pick (default 5)")
    ap.add_argument("--highlights-model", default=None,
                    help="Anthropic model id (default claude-haiku-4-5-20251001)")
    ap.add_argument("--highlights-api-key", default=None,
                    help="Anthropic API key (also reads ANTHROPIC_API_KEY env)")
    ap.add_argument("--highlights-credentials", default=None,
                    help="path to credentials JSON containing 'anthropic_api_key'. "
                         "Distinct from --credentials (which is Atlassian/Jira). "
                         "Defaults to ~/.watch-video/credentials.json inside "
                         "highlights.py if neither --highlights-api-key nor env "
                         "ANTHROPIC_API_KEY is set.")
    args = ap.parse_args()

    overall_t0 = time.time()
    kind, value = classify_input(args.input)
    workdir = Path(args.workdir).resolve() if args.workdir else default_workdir(kind, value)
    workdir.mkdir(parents=True, exist_ok=True)
    meta_path = workdir / "meta.json"
    frames_dir = workdir / "frames"

    # Verbose mode: propagate to subprocesses via env vars. WATCH_VERBOSE turns
    # on human-readable pretty lines in _common.emit(); WATCH_VERBOSE_TTY adds
    # in-place \r overwriting for progress events when stderr is a TTY.
    if args.verbose:
        os.environ["WATCH_VERBOSE"] = "1"
        if sys.stderr.isatty():
            os.environ["WATCH_VERBOSE_TTY"] = "1"

    # Load existing meta.json to inherit cache state; reset if --no-cache.
    if meta_path.exists() and not args.no_cache:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    else:
        meta = {}
    # Ensure required scaffolding
    meta.setdefault("schema_version", 2)
    meta.setdefault("workdir", str(workdir))
    meta.setdefault("input", {"raw": args.input, "kind": kind, "value": value})
    meta.setdefault("window", {"start": args.start, "end": args.end})
    meta.setdefault("report", None)
    if args.no_cache:
        meta["cache"] = {"schema": 1, "steps": {}}
    get_cache(meta)  # ensure cache block exists

    # Honor --force-step: drop those entries (downstream is invalidated when run)
    if args.force_step:
        for step in (s.strip() for s in args.force_step.split(",") if s.strip()):
            meta["cache"]["steps"].pop(step, None)
            invalidate_downstream(meta, step)

    emit("start", step="orchestrator", workdir=str(workdir),
         input_kind=kind, cache_enabled=not args.no_cache)

    def save_meta() -> None:
        staging = atomic_path(meta_path)
        staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        finalize(staging, meta_path)

    # 1. Fetch ---------------------------------------------------------------
    fetch_fp_inputs = {
        "input": args.input, "kind": kind, "value": value,
        "attachment_id": args.attachment_id,
        "credentials": args.credentials,
        "since_seconds": args.since_seconds if kind == "auto" else None,
    }
    fetch_fp = step_fingerprint("fetch", fetch_fp_inputs)
    cached_video_path = Path(meta.get("video", {}).get("path") or "")
    if is_cached(meta, "fetch", fetch_fp, [cached_video_path] if cached_video_path.name else []):
        emit("cache_hit", step="fetch", fingerprint=fetch_fp)
        fetch_info = meta["video"]
        video = Path(fetch_info["path"])
    else:
        invalidate_downstream(meta, "fetch")
        fetch_info = fetch(kind, value, workdir,
                           args.attachment_id, args.credentials, args.since_seconds)
        meta["video"] = fetch_info
        video = Path(fetch_info["path"])
        record_step(meta, "fetch", fetch_fp, [video])
    save_meta()

    # 2. Probe (always re-run; it's cheap and its result gates transcription) -
    probe_info = probe(video)
    meta["probe"] = probe_info
    save_meta()

    # 3. Frames --------------------------------------------------------------
    # `dedup_will_mutate` is part of the frames-step fingerprint because dedup
    # rewrites the same frames/ directory in place. Without this flag in the
    # fingerprint, toggling --dedup between runs would cause the next run to
    # cache-hit on a directory whose contents are the wrong (deduped) subset.
    frames_fp_inputs = {
        "video_fp": file_fingerprint(video),
        "frames": args.frames, "resolution": args.resolution,
        "start": args.start, "end": args.end,
        "scene_mode": args.scene_mode, "scene_threshold": args.scene_threshold,
        "dedup_will_mutate": args.dedup,
    }
    frames_fp = step_fingerprint("frames", frames_fp_inputs)
    if is_cached(meta, "frames", frames_fp, [frames_dir]):
        emit("cache_hit", step="frames", fingerprint=frames_fp,
             frame_count=meta.get("frames", {}).get("frame_count"))
        frames_info = meta["frames"]
    else:
        invalidate_downstream(meta, "frames")
        frames_info = extract_frames(video, workdir,
                                      args.frames, args.resolution,
                                      args.start, args.end,
                                      args.scene_mode, args.scene_threshold)
        meta["frames"] = frames_info
        record_step(meta, "frames", frames_fp, [frames_dir])
    save_meta()

    # 4. Transcribe (optional) ----------------------------------------------
    transcribe_info: dict | None = None
    skipped_audio_reason: str | None = None
    if args.no_audio:
        skipped_audio_reason = "disabled via --no-audio"
    elif not probe_info["has_audio"]:
        skipped_audio_reason = "no audio stream"
    elif probe_info["is_silent"]:
        skipped_audio_reason = f"silent track (mean_volume={probe_info['mean_volume_db']} dB)"

    if skipped_audio_reason:
        meta["transcript"] = None
        meta["skipped_audio_reason"] = skipped_audio_reason
        # Skipping invalidates any prior transcript-derived caches downstream
        meta["cache"]["steps"].pop("transcribe", None)
        invalidate_downstream(meta, "transcribe")
    else:
        transcribe_fp_inputs = {
            "video_fp": file_fingerprint(video),
            "start": args.start, "end": args.end,
            "whisper": args.whisper, "model": args.model, "lang": args.lang,
        }
        transcribe_fp = step_fingerprint("transcribe", transcribe_fp_inputs)
        transcript_outputs = [workdir / "transcript.txt", workdir / "transcript.md"]
        if is_cached(meta, "transcribe", transcribe_fp, transcript_outputs):
            emit("cache_hit", step="transcribe", fingerprint=transcribe_fp)
            transcribe_info = meta.get("transcript")
        else:
            invalidate_downstream(meta, "transcribe")
            transcribe_info = transcribe(video, workdir, args.model, args.lang,
                                         args.start, args.end,
                                         args.whisper, args.whisper_api_key,
                                         args.whisper_credentials)
            meta["transcript"] = transcribe_info
            meta["skipped_audio_reason"] = None
            record_step(meta, "transcribe", transcribe_fp, transcript_outputs)
    save_meta()

    # Track the transcribe step fingerprint for downstream cache inputs.
    transcribe_step_fp = meta["cache"]["steps"].get("transcribe", {}).get("fingerprint")

    # 5. Dedup (optional) ---------------------------------------------------
    # Use upstream step fingerprints (frames_fp, transcribe_fp) -- they identify
    # the upstream inputs/flags deterministically, even when the upstream
    # step mutated its own output dir (which dedup does to frames/).
    dedup_step_fp: str | None = None
    if args.dedup:
        dedup_fp_inputs = {
            "frames_step_fp": frames_fp,
            "transcribe_step_fp": transcribe_step_fp,
            "threshold": args.dedup_threshold,
            "min_interval": args.dedup_min_interval,
            "protect_window": args.dedup_protect_window,
        }
        dedup_step_fp = step_fingerprint("dedup", dedup_fp_inputs)
        if is_cached(meta, "dedup", dedup_step_fp, [frames_dir]):
            emit("cache_hit", step="dedup", fingerprint=dedup_step_fp,
                 frame_count=meta.get("frames", {}).get("frame_count"))
        else:
            invalidate_downstream(meta, "dedup")
            smart_dedup(workdir,
                        args.dedup_threshold,
                        args.dedup_min_interval,
                        args.dedup_protect_window)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            record_step(meta, "dedup", dedup_step_fp, [frames_dir])
            save_meta()

    # 6. OCR (optional) -----------------------------------------------------
    ocr_step_fp: str | None = None
    if args.ocr:
        ocr_fp_inputs = {
            "frames_step_fp": frames_fp,
            "dedup_step_fp": dedup_step_fp,
            "lang": args.ocr_lang,
            "min_text_length": args.ocr_min_text_length,
        }
        ocr_step_fp = step_fingerprint("ocr", ocr_fp_inputs)
        ocr_path = workdir / "ocr.txt"
        if is_cached(meta, "ocr", ocr_step_fp, [ocr_path]):
            emit("cache_hit", step="ocr", fingerprint=ocr_step_fp)
        else:
            invalidate_downstream(meta, "ocr")
            ocr(workdir, args.ocr_lang, args.ocr_min_text_length)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            record_step(meta, "ocr", ocr_step_fp, [ocr_path])
            save_meta()

    # 7. Report (optional) --------------------------------------------------
    if not args.no_report:
        # python-docx availability participates in the cache key: if a prior
        # run skipped docx because the dep was missing and the user then
        # installs python-docx, the fingerprint changes and docx gets
        # regenerated. Without this, the cache would keep serving the old
        # [md, html] outputs even though docx is now producible.
        if args.no_docx:
            docx_available = False
        else:
            try:
                import docx as _docx_probe  # noqa: F401
                docx_available = True
            except ImportError:
                docx_available = False

        # Cache key includes the format flags so toggling --no-html / --no-docx
        # invalidates cache and regenerates the missing format.
        report_fp_inputs = {
            "frames_step_fp": frames_fp,
            "transcribe_step_fp": transcribe_step_fp,
            "dedup_step_fp": dedup_step_fp,
            "ocr_step_fp": ocr_step_fp,
            "no_html": args.no_html,
            "no_docx": args.no_docx,
            "docx_available": docx_available,
        }
        report_fp = step_fingerprint("report", report_fp_inputs)
        report_path = workdir / "report.md"
        expected_report_outputs = [report_path]
        if not args.no_html:
            expected_report_outputs.append(workdir / "report.html")
        # When python-docx is usable, require report.docx for a cache hit.
        # When unavailable, the prior fingerprint differs (docx_available
        # transitioned False -> True), so we'd never reach this branch with
        # a stale cache entry.
        if docx_available:
            expected_report_outputs.append(workdir / "report.docx")

        if is_cached(meta, "report", report_fp, expected_report_outputs):
            emit("cache_hit", step="report", fingerprint=report_fp)
        else:
            report_info = report(workdir,
                                 no_html=args.no_html,
                                 no_docx=args.no_docx)
            meta["report"] = report_info
            # Record only outputs that actually exist so cache state matches
            # disk state on the next run (docx may still be skipped by
            # warning if python-docx is uninstalled between probe and run).
            actual_outputs = [report_path]
            if not args.no_html and (workdir / "report.html").exists():
                actual_outputs.append(workdir / "report.html")
            if not args.no_docx and (workdir / "report.docx").exists():
                actual_outputs.append(workdir / "report.docx")
            record_step(meta, "report", report_fp, actual_outputs)
            save_meta()

    meta["generated_at"] = int(time.time())
    meta["elapsed_seconds"] = round(time.time() - overall_t0, 2)
    save_meta()

    # 7.5. Optionally run intelligent highlights (LLM-driven moment picking).
    # When --highlights-prompt is set, we call highlights.py which writes
    # highlights.json + adds a `highlights` block to meta. post_to_jira.py's
    # summary mode auto-picks up highlights.json when present.
    if args.highlights_prompt and transcribe_info is not None:
        run_highlights(
            workdir,
            prompt=args.highlights_prompt,
            max_n=args.highlights_max_n,
            model=args.highlights_model,
            api_key=args.highlights_api_key,
            # NOTE: do NOT forward args.credentials -- that is the Atlassian
            # auth path, which would corrupt the Anthropic credentials loader
            # in highlights.py. Use the dedicated --highlights-credentials flag.
            credentials=args.highlights_credentials,
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # 8. Optionally post to Jira -- NEVER default on, never auto-added by the
    # agent. Per the skill's no-unsolicited-Jira-writes rule, this only runs
    # when the user explicitly passed --post-to-jira on this invocation.
    if args.post_to_jira:
        jira_key = (meta.get("video") or {}).get("issue_key")
        post_to_jira(
            workdir,
            jira_key=jira_key,
            dry_run=args.post_to_jira_dry_run,
            yes=args.post_to_jira_yes,
            credentials=args.credentials,
            no_embed_images=args.post_to_jira_no_embed_images,
            style=args.post_to_jira_style,
            summary_key_frames=args.post_to_jira_summary_key_frames,
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    emit("complete", step="orchestrator",
         duration_seconds=meta["elapsed_seconds"],
         meta_path=str(meta_path))
    print(json.dumps(meta, indent=2))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
