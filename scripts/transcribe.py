"""Transcribe a video's audio with the chosen Whisper provider.

Providers:
  local   -- faster-whisper running on CPU. Offline, free, no API key.
  groq    -- Groq hosted Whisper (whisper-large-v3). Cheapest+fastest hosted.
  openai  -- OpenAI hosted Whisper (whisper-1).

Output (same for all providers):
  <workdir>/transcript.txt   -- granular, one line per Whisper segment
  <workdir>/transcript.md    -- prose paragraphs, ~8s max

Window support (--start/--end): audio extraction is scoped to the window
and transcript timestamps are offset by --start so they map to the *original*
video timeline.

Usage:
    python transcribe.py <workdir> [--video <path>] [--lang CODE]
                                   [--whisper local|groq|openai]
                                   [--model NAME]
                                   [--whisper-api-key KEY]
                                   [--whisper-credentials PATH]
                                   [--start MM:SS] [--end MM:SS]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent))
from _common import (  # noqa: E402
    ExitCode, atomic_path, die, emit, finalize,
    parse_time_spec, require_executable,
)


LOCAL_ENGLISH_DEFAULT = "small.en"
LOCAL_MULTILINGUAL_DEFAULT = "small"
GROQ_DEFAULT_MODEL = "whisper-large-v3"
OPENAI_DEFAULT_MODEL = "whisper-1"

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"

DEFAULT_CREDS_PATH = Path.home() / ".watch-video" / "credentials.json"


# ---- Common segment shape ------------------------------------------------

@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str


# ---- Provider + model resolution -----------------------------------------

def pick_model_for_provider(provider: str, model_arg: str | None,
                            lang_arg: str | None) -> tuple[str, str | None]:
    """Return (model_id, language_hint). language_hint=None means auto-detect."""
    if provider == "local":
        if model_arg:
            lang = "en" if model_arg.endswith(".en") else (None if lang_arg in (None, "auto") else lang_arg)
            return model_arg, lang
        if lang_arg in (None, "en"):
            return LOCAL_ENGLISH_DEFAULT, "en"
        if lang_arg == "auto":
            return LOCAL_MULTILINGUAL_DEFAULT, None
        return LOCAL_MULTILINGUAL_DEFAULT, lang_arg
    # Hosted providers: language passed as-is (None = auto)
    if provider == "groq":
        return model_arg or GROQ_DEFAULT_MODEL, (None if lang_arg in (None, "auto") else lang_arg)
    if provider == "openai":
        return model_arg or OPENAI_DEFAULT_MODEL, (None if lang_arg in (None, "auto") else lang_arg)
    die(ExitCode.BAD_INPUT, f"unknown --whisper provider: {provider}")


def resolve_api_key(provider: str, explicit_key: str | None,
                    creds_path: Path) -> str:
    """Resolve API key in order: explicit flag, env var, credentials file."""
    if explicit_key:
        return explicit_key
    env_name = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}[provider]
    if os.environ.get(env_name):
        return os.environ[env_name]
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(ExitCode.BAD_INPUT, f"{creds_path} is not valid JSON: {e}")
        key = creds.get(f"{provider}_api_key")
        if key:
            return key
    die(ExitCode.AUTH_FAIL,
        f"No {provider} API key found. Set one of:\n"
        f"  - env var {env_name}\n"
        f"  - '{provider}_api_key' field in {creds_path}\n"
        f"  - --whisper-api-key flag (one-shot, not persisted)",
        provider=provider, env_var=env_name, creds_path=str(creds_path))


# ---- Audio extraction (atomic) -------------------------------------------

def extract_audio(video: Path, audio_wav: Path,
                  start: float | None, end: float | None) -> None:
    ffmpeg = require_executable("ffmpeg")
    staging = atomic_path(audio_wav)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None: cmd += ["-ss", f"{start}"]
    if end is not None: cmd += ["-to", f"{end}"]
    cmd += ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(staging)]
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, KeyboardInterrupt):
        staging.unlink(missing_ok=True)
        raise
    finalize(staging, audio_wav)


# ---- Local provider (faster-whisper) -------------------------------------

def transcribe_local(workdir: Path, model_name: str,
                     language: str | None) -> tuple[list[Segment], dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "faster_whisper not installed. Run: pip install --user faster-whisper",
            dependency="faster_whisper")

    audio_wav = workdir / "audio.wav"

    emit("start", step="whisper_load", provider="local", model=model_name)
    t0 = time.time()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    emit("complete", step="whisper_load", duration_seconds=round(time.time() - t0, 2))

    emit("start", step="transcribe", provider="local", language=language or "auto")
    t0 = time.time()
    segments_iter, info = model.transcribe(
        str(audio_wav),
        beam_size=5,
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    # Iterate manually so we can emit per-N-segment progress events. The
    # iterator is lazy -- each `next()` produces one segment after Whisper
    # decodes it. We don't know the segment total until it's done, so progress
    # reports "segment X, audio time up to Y seconds" which gives the user a
    # sense of how far through the audio Whisper has gotten.
    PROGRESS_EVERY_N = 10
    PROGRESS_EVERY_SECONDS = 5.0
    segments: list[Segment] = []
    last_emit_at = time.time()
    for i, s in enumerate(segments_iter, 1):
        segments.append(Segment(start=float(s.start), end=float(s.end), text=s.text.strip()))
        now = time.time()
        if i % PROGRESS_EVERY_N == 0 or (now - last_emit_at) > PROGRESS_EVERY_SECONDS:
            emit("progress", step="transcribe",
                 segments_done=i,
                 audio_position_seconds=round(s.end, 1),
                 elapsed_seconds=round(now - t0, 1))
            last_emit_at = now
    emit("complete", step="transcribe",
         duration_seconds=round(time.time() - t0, 2),
         segment_count=len(segments),
         detected_language=info.language,
         language_probability=round(info.language_probability, 3))
    return segments, {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
    }


# ---- Hosted providers (Groq, OpenAI) -------------------------------------

def _build_multipart(audio_path: Path, model: str,
                     language: str | None) -> tuple[bytes, str]:
    """Return (body, content_type) for multipart upload."""
    boundary = f"----watch-video-{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode())
        parts.append(b"\r\n")

    def add_file(name: str, filename: str, content: bytes, mime: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(content)
        parts.append(b"\r\n")

    add_file("file", "audio.wav", audio_path.read_bytes(), "audio/wav")
    add_field("model", model)
    add_field("response_format", "verbose_json")
    if language:
        add_field("language", language)
    parts.append(f"--{boundary}--\r\n".encode())

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe_hosted(workdir: Path, provider: str, model: str,
                      language: str | None, api_key: str) -> tuple[list[Segment], dict]:
    audio_wav = workdir / "audio.wav"
    size = audio_wav.stat().st_size
    if size > 25 * 1024 * 1024:
        die(ExitCode.BAD_INPUT,
            f"audio.wav is {size/1024/1024:.1f} MB; hosted Whisper has a 25 MB limit. "
            f"Use --start/--end to scope to a window.")

    endpoint = GROQ_ENDPOINT if provider == "groq" else OPENAI_ENDPOINT
    body, content_type = _build_multipart(audio_wav, model, language)

    emit("start", step="transcribe", provider=provider, model=model,
         audio_bytes=size)
    t0 = time.time()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:300]
        if e.code == 401:
            die(ExitCode.AUTH_FAIL, f"{provider} API auth failed (401): {err_body}")
        if e.code == 413:
            die(ExitCode.BAD_INPUT, f"audio too large for {provider} (413): {err_body}")
        if e.code == 429:
            die(ExitCode.TIMEOUT, f"{provider} rate-limited (429): {err_body}")
        die(ExitCode.IO_FAIL, f"{provider} API error {e.code}: {err_body}")
    except urllib.error.URLError as e:
        die(ExitCode.TIMEOUT, f"network error reaching {provider}: {e}")

    raw_segments = result.get("segments") or []
    if not raw_segments and result.get("text"):
        # Some providers return only `text` on short clips. Synthesize a single segment.
        raw_segments = [{"start": 0.0, "end": 0.0, "text": result["text"]}]

    segments = [Segment(start=float(s.get("start", 0.0)),
                        end=float(s.get("end", 0.0)),
                        text=str(s.get("text", "")).strip())
                for s in raw_segments]
    detected = result.get("language") or language or "unknown"
    emit("complete", step="transcribe",
         duration_seconds=round(time.time() - t0, 2),
         segment_count=len(segments),
         detected_language=detected,
         provider=provider)
    return segments, {"language": detected, "language_probability": 1.0}


# ---- Output formatting (shared) ------------------------------------------

def _format_ts(seconds: float) -> str:
    mm = int(seconds // 60)
    ss = int(seconds % 60)
    return f"{mm:02d}:{ss:02d}"


def write_outputs(workdir: Path, segments: Iterable[Segment], offset: float) -> None:
    seg_list = list(segments)
    # Granular transcript -- timestamps offset to original video time
    txt_lines = []
    for s in seg_list:
        ts = _format_ts(s.start + offset)
        txt_lines.append(f"[{ts}] {s.text}")

    # Prose transcript -- merge consecutive segments. Break on >2s gap OR >8s elapsed.
    PARA_GAP_SECONDS = 2.0
    PARA_MAX_LENGTH_SECONDS = 8.0
    paragraphs: list[str] = []
    current_text: list[str] = []
    current_start: float | None = None
    last_end: float | None = None
    for s in seg_list:
        force_break = (
            last_end is not None and (
                (s.start - last_end) > PARA_GAP_SECONDS
                or (s.start - current_start) > PARA_MAX_LENGTH_SECONDS
            )
        )
        if current_start is None:
            current_start = s.start
        elif force_break:
            paragraphs.append(
                f"(_{_format_ts(current_start + offset)}_) {' '.join(current_text)}"
            )
            current_text = []
            current_start = s.start
        current_text.append(s.text)
        last_end = s.end
    if current_text and current_start is not None:
        paragraphs.append(f"(_{_format_ts(current_start + offset)}_) {' '.join(current_text)}")

    txt_dest = workdir / "transcript.txt"
    md_dest = workdir / "transcript.md"
    txt_staging = atomic_path(txt_dest)
    md_staging = atomic_path(md_dest)
    txt_staging.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    md_staging.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    finalize(txt_staging, txt_dest)
    finalize(md_staging, md_dest)


# ---- Entry point ---------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--video", help="source video (extracts audio if audio.wav missing)")
    ap.add_argument("--whisper", choices=("local", "groq", "openai"), default="local",
                    help="transcription provider (default: local faster-whisper)")
    ap.add_argument("--whisper-api-key", default=None,
                    help="API key for hosted providers. WARNING: visible in shell history "
                         "and process listings; do not use on shared machines or recorded "
                         "sessions. Prefer env var ($GROQ_API_KEY/$OPENAI_API_KEY) or "
                         "credentials file.")
    ap.add_argument("--whisper-credentials", default=str(DEFAULT_CREDS_PATH),
                    help=f"credentials JSON path (default: {DEFAULT_CREDS_PATH})")
    ap.add_argument("--model", default=None,
                    help="model id. Local: small.en, small, medium, large-v3. Groq: whisper-large-v3, whisper-large-v3-turbo. OpenAI: whisper-1.")
    ap.add_argument("--lang", default=None,
                    help="ISO language code (en, pl, es, ...) or 'auto'. Default: en")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    audio_wav = workdir / "audio.wav"

    start = parse_time_spec(args.start)
    end = parse_time_spec(args.end)
    offset = start or 0.0

    # Always regenerate audio.wav when --video is provided (correctness across windows)
    if args.video:
        emit("start", step="audio_extract", window_start=start, window_end=end)
        t0 = time.time()
        try:
            extract_audio(Path(args.video), audio_wav, start, end)
        except subprocess.CalledProcessError as e:
            die(ExitCode.IO_FAIL, f"audio extraction failed: {e}")
        emit("complete", step="audio_extract",
             duration_seconds=round(time.time() - t0, 2),
             output=str(audio_wav))
    elif not audio_wav.exists():
        die(ExitCode.BAD_INPUT, "audio.wav missing and --video not provided")

    model, language = pick_model_for_provider(args.whisper, args.model, args.lang)

    if args.whisper == "local":
        segments, info = transcribe_local(workdir, model, language)
    else:
        api_key = resolve_api_key(args.whisper, args.whisper_api_key,
                                  Path(args.whisper_credentials))
        segments, info = transcribe_hosted(workdir, args.whisper, model, language, api_key)

    write_outputs(workdir, segments, offset)

    result = {
        "transcript_txt": str(workdir / "transcript.txt"),
        "transcript_md": str(workdir / "transcript.md"),
        "segments": len(segments),
        "language": info["language"],
        "language_probability": info["language_probability"],
        "offset_seconds": offset,
        "provider": args.whisper,
        "model": model,
    }
    print(json.dumps(result))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
