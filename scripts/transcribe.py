"""Transcribe a video's audio with the chosen Whisper provider.

Providers:
  captions -- Read VTT captions yt-dlp pulled alongside the video. Free,
              fast, no audio extraction. Available only for URL inputs where
              the source platform supplies captions (most YouTube content).
  local    -- faster-whisper running on CPU. Offline, free, no API key.
  groq     -- Groq hosted Whisper (whisper-large-v3). Cheapest+fastest hosted.
  openai   -- OpenAI hosted Whisper (whisper-1).

Output (same for all providers):
  <workdir>/transcript.txt   -- granular, one line per segment
  <workdir>/transcript.md    -- prose paragraphs, ~8s max

Window support (--start/--end): audio extraction is scoped to the window
and transcript timestamps are offset by --start so they map to the *original*
video timeline.

Usage:
    python transcribe.py <workdir> [--video <path>] [--lang CODE]
                                   [--whisper captions|local|groq|openai]
                                   [--captions-vtt <path>]
                                   [--model NAME]
                                   [--whisper-api-key KEY]
                                   [--whisper-credentials PATH]
                                   [--start MM:SS] [--end MM:SS]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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
DEEPGRAM_DEFAULT_MODEL = "nova-3"     # current SOTA Deepgram model; supports diarization
# WhisperX uses the same model IDs as faster-whisper / local Whisper -- the
# orchestration around it adds word-level alignment + pyannote diarization.
# Default to small.en for parity with --whisper local; users override via --model.
WHISPERX_DEFAULT_MODEL = "small.en"

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
# Deepgram doesn't use the OpenAI Whisper API shape. The request body is
# the raw audio bytes with Content-Type matching the codec (no multipart
# wrapper); feature flags (diarize/punctuate/smart_format/utterances/model/
# language) ride in the query string.
DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"

DEFAULT_CREDS_PATH = Path.home() / ".watch-video" / "credentials.json"


# ---- Common segment shape ------------------------------------------------

@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    # Optional anonymous speaker id ("S0", "S1", ...) from diarization
    # providers (Deepgram, future WhisperX). None for transcription-only
    # providers (captions / local Whisper / OpenAI / Groq). When present,
    # write_outputs() prefixes prose paragraphs with **S0** / **S1** / etc.
    # (transcript.md format: `**S0** (_MM:SS_) text`) and inline-tags
    # transcript.txt lines as `[MM:SS] S0: text`.
    speaker: str | None = None


# ---- Provider + model resolution -----------------------------------------

def pick_model_for_provider(provider: str, model_arg: str | None,
                            lang_arg: str | None) -> tuple[str, str | None]:
    """Return (model_id, language_hint). language_hint=None means auto-detect."""
    if provider == "captions":
        # No model, captions are the source. Language inferred from the file.
        return "vtt-captions", (None if lang_arg in (None, "auto") else lang_arg)
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
    if provider == "deepgram":
        return model_arg or DEEPGRAM_DEFAULT_MODEL, (None if lang_arg in (None, "auto") else lang_arg)
    if provider == "whisperx":
        # WhisperX uses the same Whisper model IDs as faster-whisper. Mirror
        # local's lang heuristic: .en models pin English; everything else
        # respects --lang or auto-detects.
        if model_arg:
            lang = "en" if model_arg.endswith(".en") else (None if lang_arg in (None, "auto") else lang_arg)
            return model_arg, lang
        if lang_arg in (None, "en"):
            return WHISPERX_DEFAULT_MODEL, "en"
        if lang_arg == "auto":
            return LOCAL_MULTILINGUAL_DEFAULT, None
        return LOCAL_MULTILINGUAL_DEFAULT, lang_arg
    die(ExitCode.BAD_INPUT, f"unknown --whisper provider: {provider}")


def resolve_api_key(provider: str, explicit_key: str | None,
                    creds_path: Path) -> str:
    """Resolve API key in order: explicit flag, env var, credentials file."""
    if explicit_key:
        return explicit_key
    # Per-provider env-var + creds-file field. WhisperX is the only provider
    # that uses HuggingFace tokens instead of a per-vendor API key (pyannote
    # gating goes through HF), so it gets its own field/env name.
    env_name = {
        "groq":     "GROQ_API_KEY",
        "openai":   "OPENAI_API_KEY",
        "deepgram": "DEEPGRAM_API_KEY",
        "whisperx": "HF_TOKEN",
    }[provider]
    creds_field = {
        "groq":     "groq_api_key",
        "openai":   "openai_api_key",
        "deepgram": "deepgram_api_key",
        "whisperx": "hf_token",
    }[provider]
    if os.environ.get(env_name):
        return os.environ[env_name]
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(ExitCode.BAD_INPUT, f"{creds_path} is not valid JSON: {e}")
        key = creds.get(creds_field)
        if key:
            return key
    die(ExitCode.AUTH_FAIL,
        f"No {provider} API key/token found. Set one of:\n"
        f"  - env var {env_name}\n"
        f"  - '{creds_field}' field in {creds_path}\n"
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


# ---- Captions provider (VTT from yt-dlp) ---------------------------------

_VTT_TS = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\.(\d{3})"
)
_VTT_CUE_RE = re.compile(
    r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2}\.\d{3})\s*-->\s*"
    r"((?:\d{1,2}:)?\d{1,2}:\d{2}\.\d{3})"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def _vtt_ts_to_seconds(ts: str) -> float:
    """Parse 'HH:MM:SS.mmm' or 'MM:SS.mmm' into seconds."""
    m = _VTT_TS.match(ts.strip())
    if not m:
        return 0.0
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    ms = int(m.group(4))
    return h * 3600 + mm * 60 + ss + ms / 1000.0


def parse_vtt(text: str) -> list[Segment]:
    """Parse WebVTT text into a flat list of Segments.

    Tolerates the variations yt-dlp produces (numbered cues, style tags,
    speaker spans, cue settings on the timing line). Strips inline tags so
    the output is plain prose matching what Whisper would produce.

    Handles YouTube's "rolling window" auto-captions: each cue often
    contains the previous cue's last line(s) plus newly-spoken lines, which
    naively concatenated produces every line twice. We dedupe per cue --
    lines that appeared in the immediately-preceding cue are dropped, so
    the emitted text is the new content only.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw_cues: list[tuple[float, float, list[str]]] = []
    i = 0
    # Skip the WEBVTT header line and any block of header metadata until
    # the first blank line.
    while i < len(lines) and lines[i].strip() != "":
        i += 1
    while i < len(lines):
        line = lines[i].strip()
        m = _VTT_CUE_RE.match(line)
        if not m:
            i += 1
            continue
        start = _vtt_ts_to_seconds(m.group(1))
        end = _vtt_ts_to_seconds(m.group(2))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip() != "":
            cleaned = _VTT_TAG_RE.sub("", lines[i].strip())
            if cleaned:
                text_lines.append(cleaned)
            i += 1
        if text_lines:
            raw_cues.append((start, end, text_lines))

    segments: list[Segment] = []
    prev_line_set: set[str] = set()
    for start, end, cur_lines in raw_cues:
        new_lines = [ln for ln in cur_lines if ln not in prev_line_set]
        if new_lines:
            segments.append(Segment(
                start=start, end=end, text=" ".join(new_lines),
            ))
        prev_line_set = set(cur_lines)
    return segments


def transcribe_from_captions(workdir: Path,
                             vtt_path: Path,
                             window_start: float | None,
                             window_end: float | None,
                             ) -> tuple[list[Segment], dict]:
    """Read VTT captions instead of running Whisper. Free, fast, no audio.

    Honors --start/--end window: segments outside the window are dropped,
    and start times are offset to make 0 = window start (matching Whisper
    behavior so report timestamps line up with --start).
    """
    if not vtt_path.exists():
        die(ExitCode.BAD_INPUT,
            f"captions provider requested but VTT not found at {vtt_path}")

    emit("start", step="transcribe", provider="captions",
         vtt_path=str(vtt_path))
    t0 = time.time()
    vtt_text = vtt_path.read_text(encoding="utf-8", errors="replace")
    all_segments = parse_vtt(vtt_text)

    # VTT timestamps are in original-video time. Filter to the window but
    # keep absolute starts -- the caller passes offset=0 to write_outputs
    # so output timestamps match the original timeline, matching Whisper's
    # behavior (where audio is extracted from the window and offset=start
    # shifts back).
    if window_start is not None or window_end is not None:
        lo = window_start or 0.0
        hi = window_end if window_end is not None else float("inf")
        segments = [s for s in all_segments if s.end >= lo and s.start <= hi]
    else:
        segments = all_segments

    # Infer language from filename (yt-dlp writes <stem>.<lang>.vtt).
    lang = None
    name_parts = vtt_path.name.rsplit(".", 2)
    if len(name_parts) >= 3 and name_parts[-1] == "vtt":
        lang = name_parts[-2].split("-")[0]  # "en-US" -> "en"

    emit("complete", step="transcribe",
         duration_seconds=round(time.time() - t0, 2),
         segment_count=len(segments),
         provider="captions",
         detected_language=lang,
         language_probability=1.0)
    return segments, {
        "language": lang,
        "language_probability": 1.0,
    }


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


def transcribe_deepgram(workdir: Path, model: str,
                        language: str | None,
                        api_key: str) -> tuple[list[Segment], dict]:
    """Transcribe via Deepgram with speaker diarization enabled.

    Deepgram's API is NOT OpenAI-compatible. It accepts the raw audio body
    (no multipart wrapper), with feature flags as query-string params:
      - `diarize=true`        -> per-word speaker ids
      - `punctuate=true`      -> readable text
      - `smart_format=true`   -> numbers, dates, etc. formatted naturally
      - `model=<name>`        -> nova-3 is the current SOTA
      - `language=<code>`     -> ISO code; omit for auto-detect (multi-lang)

    The response's `results.channels[0].alternatives[0].words[]` carries
    per-word `(start, end, word, speaker)`. We group consecutive words by
    speaker into Segment instances so downstream paragraph-merging keeps
    each utterance intact.

    Returns segments with .speaker populated ('S0', 'S1', ...).
    """
    audio_wav = workdir / "audio.wav"
    size = audio_wav.stat().st_size
    # Deepgram supports much larger files than OpenAI/Groq (no 25 MB cap)
    # but on free tier the pre-paid balance is finite. No size guard here.

    # Build the query string with proper URL-encoding so a model name or
    # language code with reserved characters (spaces, ampersands, etc.)
    # can't break the URL or change request semantics.
    params: dict[str, str] = {
        "model": model,
        "diarize": "true",
        "punctuate": "true",
        "smart_format": "true",
        "utterances": "true",
    }
    if language and language != "auto":
        params["language"] = language
    endpoint = f"{DEEPGRAM_ENDPOINT}?{urllib.parse.urlencode(params)}"

    emit("start", step="transcribe", provider="deepgram", model=model,
         audio_bytes=size)
    t0 = time.time()
    # Stream the audio file as the request body instead of read_bytes() to
    # keep memory bounded on long recordings (a 60-min uncompressed WAV is
    # ~600 MB; reading it all into a Python bytes object is wasteful and
    # can OOM on smaller machines). Pass the file object + an explicit
    # Content-Length header so urllib uses it as-is.
    audio_fp = open(audio_wav, "rb")
    req = urllib.request.Request(
        endpoint, data=audio_fp,
        headers={
            "Authorization": f"Token {api_key}",  # Deepgram uses 'Token <key>', not 'Bearer'
            "Content-Type": "audio/wav",
            "Content-Length": str(size),
        },
        method="POST",
    )
    try:
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:300]
            if e.code == 401:
                die(ExitCode.AUTH_FAIL, f"Deepgram API auth failed (401): {err_body}")
            if e.code == 429:
                die(ExitCode.TIMEOUT, f"Deepgram rate-limited (429): {err_body}")
            die(ExitCode.IO_FAIL, f"Deepgram API error {e.code}: {err_body}")
        except urllib.error.URLError as e:
            die(ExitCode.TIMEOUT, f"network error reaching Deepgram: {e}")
    finally:
        # Always close the audio fp -- die() raises SystemExit which would
        # otherwise leak the handle through the request lifecycle.
        try:
            audio_fp.close()
        except OSError:
            pass

    # Prefer utterances (already speaker-grouped). Fall back to words->segments.
    utterances = (((result.get("results") or {}).get("utterances")) or [])
    segments: list[Segment] = []
    if utterances:
        for u in utterances:
            speaker_id = u.get("speaker")
            segments.append(Segment(
                start=float(u.get("start", 0.0)),
                end=float(u.get("end", 0.0)),
                text=str(u.get("transcript", "")).strip(),
                speaker=(f"S{speaker_id}" if speaker_id is not None else None),
            ))
    else:
        # Fallback: group words by speaker into utterance-shaped segments.
        # IMPORTANT: when flushing on a speaker change, the previous group's
        # `end` is the PREVIOUS WORD's end, not the new word's start. Using
        # the new word's start would systematically undercount each group's
        # duration by the inter-word gap and skew speakers.json airtime.
        words = (((result.get("results") or {}).get("channels") or [{}])[0]
                 .get("alternatives", [{}])[0].get("words") or [])
        current_speaker = None
        current_start = 0.0
        current_end = 0.0  # tracks the previous word's end for accurate flush
        current_words: list[str] = []
        for w in words:
            sp = w.get("speaker")
            if sp != current_speaker:
                if current_words:
                    segments.append(Segment(
                        start=current_start,
                        end=current_end,  # previous word's end, not next word's start
                        text=" ".join(current_words),
                        speaker=(f"S{current_speaker}" if current_speaker is not None else None),
                    ))
                current_speaker = sp
                current_start = float(w.get("start", 0.0))
                current_words = []
            current_words.append(str(w.get("punctuated_word") or w.get("word") or ""))
            current_end = float(w.get("end", current_start))
        if current_words:
            segments.append(Segment(
                start=current_start,
                end=current_end,
                text=" ".join(current_words),
                speaker=(f"S{current_speaker}" if current_speaker is not None else None),
            ))

    detected_lang = (((result.get("results") or {}).get("channels") or [{}])[0]
                     .get("detected_language")) or language or "unknown"

    emit("complete", step="transcribe",
         duration_seconds=round(time.time() - t0, 2),
         segment_count=len(segments),
         distinct_speakers=len({s.speaker for s in segments if s.speaker}),
         detected_language=detected_lang,
         provider="deepgram")
    return segments, {"language": detected_lang, "language_probability": 1.0}


def transcribe_whisperx(workdir: Path, model_name: str,
                        language: str | None,
                        hf_token: str) -> tuple[list[Segment], dict]:
    """Transcribe + diarize locally using the 'WhisperX recipe' --
    faster-whisper for transcription + pyannote.audio for speaker diarization.

    Implementation note: we DON'T depend on the `whisperx` PyPI package. That
    wrapper hard-pins to old versions of its deps (faster-whisper==1.0.0,
    pyannote.audio==3.1.1, ctranslate2==4.4.0) and assumes deprecated
    torchaudio APIs, so it's broken on modern Python (3.14) ecosystems.
    Instead we call the underlying libraries directly:

      1. faster-whisper for transcription WITH word-level timestamps
         (word_timestamps=True; 1.2+ does this natively, removing the need
         for WhisperX's separate wav2vec2 alignment step).
      2. pyannote.audio Pipeline for diarization (gated HF model;
         needs HF_TOKEN + accepted terms).
      3. Walk the diarized segments and each transcribed word; assign each
         word to the speaker who was active at its midpoint timestamp;
         group consecutive same-speaker words into utterance-shaped segments.

    Same speakers.json schema as transcribe_deepgram(), so
    write_outputs() / write_speakers_json() / relabel_speakers.py all
    work against either provider unchanged.

    Trade-off vs --whisper deepgram:
      - This path: $0/min forever, runs offline, but ~500 MB-1 GB of
        pyannote model weights cached on first run; needs HF token +
        accepting terms on the gated diarization repo.
      - Deepgram: ~$0.0043/min, zero install, no token gating.
    """
    # faster-whisper is already needed for --whisper local; keep the
    # import deferred so callers using other providers don't pay it.
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "faster-whisper not installed. Run: pip install faster-whisper",
            dependency="faster-whisper")

    # pyannote.audio is the heavy optional dep -- only required for the
    # whisperx path, never imported on other --whisper providers.
    #
    # Compat shim: torchaudio>=2.10 removed `list_audio_backends()`, which
    # pyannote.audio 4.x still calls internally. Monkey-patch it back to a
    # no-op-ish sensible default BEFORE importing pyannote so the import
    # succeeds. Returning ['soundfile'] (a backend pyannote knows about)
    # is the least surprising stub. Remove this shim if/when pyannote
    # updates to the new torchaudio API.
    try:
        import torchaudio  # type: ignore[import-not-found]
        if not hasattr(torchaudio, "list_audio_backends"):
            torchaudio.list_audio_backends = lambda: ["soundfile"]
    except ImportError:
        pass  # pyannote import below will surface a clearer error
    try:
        from pyannote.audio import Pipeline  # type: ignore[import-not-found]
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "pyannote.audio not installed. Run: pip install pyannote.audio\n"
            "After install, also accept terms at all three gated models:\n"
            "  https://huggingface.co/pyannote/speaker-diarization-3.1\n"
            "  https://huggingface.co/pyannote/segmentation-3.0\n"
            "  https://huggingface.co/pyannote/speaker-diarization-community-1\n"
            "(pyannote 4.x splits weights across multiple gated repos)\n"
            "and provide an HF token (HF_TOKEN env var or hf_token field "
            "in ~/.watch-video/credentials.json).",
            dependency="pyannote.audio")

    # torch only used to pick CUDA vs CPU.
    try:
        import torch  # type: ignore[import-not-found]
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"

    audio_path = workdir / "audio.wav"
    if not audio_path.exists():
        die(ExitCode.BAD_INPUT,
            f"audio.wav missing at {audio_path}; watch_video.py extracts "
            f"this from --video before calling transcribe.")

    emit("start", step="transcribe",
         provider="whisperx", model=model_name, device=device,
         audio_bytes=audio_path.stat().st_size)
    t0 = time.time()

    # Step 1: faster-whisper with word-level timestamps. compute_type=int8
    # on CPU (smaller + faster); float16 on CUDA.
    compute_type = "int8" if device == "cpu" else "float16"
    try:
        wmodel = WhisperModel(model_name, device=device,
                              compute_type=compute_type)
        segments_iter, info = wmodel.transcribe(
            str(audio_path), language=language, word_timestamps=True,
        )
        # Materialise the generator so we can iterate twice (once for word
        # collection, once for fallback segment construction).
        fw_segments = list(segments_iter)
    except Exception as e:
        die(ExitCode.IO_FAIL, f"faster-whisper transcribe failed: {e}")

    detected_lang = info.language or language or "unknown"

    # Collect (start, end, text) for every word across all segments.
    words: list[dict] = []
    for s in fw_segments:
        for w in (s.words or []):
            if w.word is None or w.start is None or w.end is None:
                continue
            words.append({
                "start": float(w.start),
                "end": float(w.end),
                "text": w.word.strip(),
            })

    # Step 2: diarize with pyannote. The pipeline returns a SlidingWindowFeature
    # (annotation timeline) over which we iterate to get (start, end, speaker).
    # pyannote 4.x renamed `use_auth_token` -> `token`; we target the 4.x API
    # since that's what installs cleanly on modern (Python 3.13+) ecosystems.
    #
    # Two-stage attempt: try the chosen device, but if CUDA fails with a
    # kernel-image error (e.g. RTX 50-series sm_120 on a PyTorch built for
    # sm_90 only), transparently fall back to CPU. Diarization is fast
    # enough on CPU for most workloads.
    # Pre-load audio as an in-memory tensor dict. pyannote 4.x defaults to
    # decoding via torchcodec which on Windows requires a "full-shared"
    # FFmpeg install (DLLs alongside the binary). Most ffmpeg installs
    # (winget Gyan.FFmpeg, chocolatey ffmpeg, etc.) ship the binary only.
    # Sidestepping torchcodec by feeding a pre-decoded tensor avoids the
    # "Could not load libtorchcodec" / "AudioDecoder not defined" failure.
    try:
        import soundfile as sf  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
        data, sample_rate = sf.read(str(audio_path), dtype="float32",
                                    always_2d=True)
        # soundfile returns (time, channel); pyannote wants (channel, time)
        waveform_tensor = torch.from_numpy(data.T)
        audio_in = {"waveform": waveform_tensor, "sample_rate": sample_rate}
    except ImportError:
        # soundfile is a faster-whisper transitive dep; this branch should
        # be unreachable. If hit, fall back to the file-path form and hope
        # the user's torchcodec works.
        audio_in = str(audio_path)

    diar_device = device
    try:
        diar = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token,
        )
        if diar_device == "cuda":
            try:
                diar.to(torch.device("cuda"))
                diar_annotation = diar(audio_in)
            except Exception as cuda_e:
                err_str = str(cuda_e)
                if "no kernel image" in err_str.lower() or "sm_" in err_str:
                    emit("warning", step="transcribe",
                         msg=f"CUDA fell over ({type(cuda_e).__name__}); "
                             f"retrying diarization on CPU. To use this GPU, "
                             f"install a PyTorch build with matching sm_* "
                             f"compute capability (see "
                             f"https://pytorch.org/get-started/locally/).",
                         cuda_error=err_str[:200])
                    diar_device = "cpu"
                    diar.to(torch.device("cpu"))
                    diar_annotation = diar(audio_in)
                else:
                    raise
        else:
            diar_annotation = diar(audio_in)
    except Exception as e:
        err_str = str(e)
        is_auth = "401" in err_str or "gated" in err_str.lower() or "token" in err_str.lower()
        die(ExitCode.AUTH_FAIL if is_auth else ExitCode.IO_FAIL,
            f"pyannote diarization failed: {e}. "
            f"Common causes: (1) HF token missing or invalid; "
            f"(2) terms not accepted at all three gated repos "
            f"(speaker-diarization-3.1, segmentation-3.0, "
            f"speaker-diarization-community-1); "
            f"(3) network error fetching the model on first run.")

    # pyannote 4.x returns a DiarizeOutput wrapper; the Annotation timeline
    # is on .speaker_diarization. (3.x returned the Annotation directly.)
    # Support both: unwrap when the wrapper is present.
    if hasattr(diar_annotation, "speaker_diarization"):
        diar_timeline = diar_annotation.speaker_diarization
    else:
        diar_timeline = diar_annotation

    # Build a flat list of (start, end, speaker_id) intervals from pyannote.
    # speaker_id strings from pyannote look like 'SPEAKER_00', 'SPEAKER_01'...
    diar_intervals: list[tuple[float, float, str]] = []
    for turn, _track, speaker in diar_timeline.itertracks(yield_label=True):
        diar_intervals.append((float(turn.start), float(turn.end), str(speaker)))

    def _speaker_at(ts: float) -> str | None:
        """Pick the speaker whose interval contains ts. Used at each word's
        midpoint for the word->speaker mapping. Falls back to None if no
        diarization interval covers the timestamp (rare; usually means a
        word landed in a non-speech gap)."""
        for start, end, spk in diar_intervals:
            if start <= ts <= end:
                return spk
        # Nearest fallback so we don't drop words that landed on a boundary.
        if not diar_intervals:
            return None
        nearest = min(diar_intervals,
                      key=lambda iv: min(abs(ts - iv[0]), abs(ts - iv[1])))
        return nearest[2]

    # Step 3: assign each word to a speaker, then group consecutive same-
    # speaker words into utterance-shaped segments. This is the same shape
    # transcribe_deepgram() returns.
    segments: list[Segment] = []
    if not words:
        # No word timestamps came out of faster-whisper (e.g. very short or
        # silent clip). Fall back to coarse segment-level diarization.
        for s in fw_segments:
            mid = (s.start + s.end) / 2.0
            raw_speaker = _speaker_at(mid)
            segments.append(Segment(
                start=float(s.start), end=float(s.end),
                text=str(s.text).strip(),
                speaker=_normalise_speaker(raw_speaker),
            ))
    else:
        current_speaker: str | None = None
        current_start: float = words[0]["start"]
        current_end: float = words[0]["end"]
        current_words: list[str] = []
        for w in words:
            mid = (w["start"] + w["end"]) / 2.0
            raw_speaker = _speaker_at(mid)
            if raw_speaker != current_speaker and current_words:
                segments.append(Segment(
                    start=current_start, end=current_end,
                    text=" ".join(current_words).strip(),
                    speaker=_normalise_speaker(current_speaker),
                ))
                current_words = []
                current_start = w["start"]
            current_speaker = raw_speaker
            current_words.append(w["text"])
            current_end = w["end"]
        if current_words:
            segments.append(Segment(
                start=current_start, end=current_end,
                text=" ".join(current_words).strip(),
                speaker=_normalise_speaker(current_speaker),
            ))

    emit("complete", step="transcribe",
         duration_seconds=round(time.time() - t0, 2),
         segment_count=len(segments),
         distinct_speakers=len({s.speaker for s in segments if s.speaker}),
         detected_language=detected_lang,
         provider="whisperx",
         device=device)
    return segments, {"language": detected_lang,
                      "language_probability": float(info.language_probability or 1.0)}


def _normalise_speaker(raw: str | None) -> str | None:
    """pyannote emits 'SPEAKER_00'/'SPEAKER_01'/...; normalise to 'S0'/'S1'/...
    so the rest of the pipeline sees the same format Deepgram produces.
    Returns None if the input is None (e.g. word landed in a non-speech gap
    with no diarization fallback)."""
    if raw is None:
        return None
    tail = str(raw).rsplit("_", 1)[-1]
    try:
        return f"S{int(tail)}"
    except ValueError:
        return f"S{tail}"


# ---- Output formatting (shared) ------------------------------------------

def _format_ts(seconds: float) -> str:
    mm = int(seconds // 60)
    ss = int(seconds % 60)
    return f"{mm:02d}:{ss:02d}"


def write_outputs(workdir: Path, segments: Iterable[Segment], offset: float) -> None:
    seg_list = list(segments)
    has_speakers = any(s.speaker for s in seg_list)

    # Granular transcript -- timestamps offset to original video time. When
    # speakers are present, include the speaker id inline: `[00:15] S0: ...`
    txt_lines: list[str] = []
    for s in seg_list:
        ts = _format_ts(s.start + offset)
        if has_speakers:
            spk = s.speaker or "S?"
            txt_lines.append(f"[{ts}] {spk}: {s.text}")
        else:
            txt_lines.append(f"[{ts}] {s.text}")

    # Prose transcript -- merge consecutive segments. Break on >2s gap OR
    # >8s elapsed. With diarization also force a break on speaker change so
    # each paragraph belongs to exactly one speaker.
    PARA_GAP_SECONDS = 2.0
    PARA_MAX_LENGTH_SECONDS = 8.0
    paragraphs: list[str] = []
    current_text: list[str] = []
    current_start: float | None = None
    current_speaker: str | None = None
    last_end: float | None = None

    def _emit_paragraph(start_ts: float, text_parts: list[str], speaker: str | None) -> str:
        ts_part = f"(_{_format_ts(start_ts + offset)}_)"
        body = " ".join(text_parts)
        if has_speakers:
            return f"**{speaker or 'S?'}** {ts_part} {body}"
        return f"{ts_part} {body}"

    for s in seg_list:
        # When the run is diarized, force a paragraph break on ANY speaker
        # mismatch -- including None->Sx and Sx->None transitions. Without
        # this, a Deepgram segment with speaker=None (rare but possible)
        # would get folded into the previous speaker's paragraph and the
        # `**S0**` / `**S1**` label would no longer reflect the actual
        # content of the paragraph.
        speaker_changed = (
            has_speakers and current_start is not None
            and s.speaker != current_speaker
        )
        force_break = (
            last_end is not None and (
                (s.start - last_end) > PARA_GAP_SECONDS
                or (s.start - current_start) > PARA_MAX_LENGTH_SECONDS
                or speaker_changed
            )
        )
        if current_start is None:
            current_start = s.start
            current_speaker = s.speaker
        elif force_break:
            paragraphs.append(_emit_paragraph(current_start, current_text, current_speaker))
            current_text = []
            current_start = s.start
            current_speaker = s.speaker
        current_text.append(s.text)
        last_end = s.end
    if current_text and current_start is not None:
        paragraphs.append(_emit_paragraph(current_start, current_text, current_speaker))

    txt_dest = workdir / "transcript.txt"
    md_dest = workdir / "transcript.md"
    txt_staging = atomic_path(txt_dest)
    md_staging = atomic_path(md_dest)
    txt_staging.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    md_staging.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    finalize(txt_staging, txt_dest)
    finalize(md_staging, md_dest)


def write_speakers_json(workdir: Path, segments: Iterable[Segment],
                        offset: float) -> list[dict] | None:
    """Write <workdir>/speakers.json summarizing each unique speaker.

    Each entry has:
      - id: "S0", "S1", ...  (anonymous, assigned by the diarizer)
      - first_utterance_ts: when the speaker first speaks (HH:MM-style str)
      - first_utterance_text: their opening line (sample for relabeling)
      - segment_count: how many utterances they have
      - total_duration_seconds: total airtime

    Returns the list for inclusion in transcribe's result dict; or None when
    no diarization happened (no file written in that case).
    """
    seg_list = [s for s in segments if s.speaker]
    if not seg_list:
        return None
    per_speaker: dict[str, dict] = {}
    for s in seg_list:
        info = per_speaker.setdefault(s.speaker, {
            "id": s.speaker,
            "first_utterance_ts": _format_ts(s.start + offset),
            "first_utterance_start_seconds": round(s.start + offset, 2),
            "first_utterance_text": s.text,
            "segment_count": 0,
            "total_duration_seconds": 0.0,
        })
        info["segment_count"] += 1
        info["total_duration_seconds"] = round(
            info["total_duration_seconds"] + max(0.0, s.end - s.start), 2)
    # Sort by first appearance time so S0 is usually first chronologically.
    summary = sorted(per_speaker.values(),
                     key=lambda d: d["first_utterance_start_seconds"])
    speakers_path = workdir / "speakers.json"
    staging = atomic_path(speakers_path)
    staging.write_text(json.dumps({"speakers": summary}, indent=2),
                       encoding="utf-8")
    finalize(staging, speakers_path)
    return summary


# ---- Entry point ---------------------------------------------------------

def run_inproc(
    workdir: Path,
    video: str | None = None,
    whisper: str = "local",
    captions_vtt: str | None = None,
    whisper_api_key: str | None = None,
    whisper_credentials: str | None = None,
    model_name: str | None = None,
    lang: str | None = None,
    start_spec: str | None = None,
    end_spec: str | None = None,
) -> dict:
    """Pure function for in-process invocation. See probe.run_inproc docstring."""
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    audio_wav = workdir / "audio.wav"
    if whisper_credentials is None:
        whisper_credentials = str(DEFAULT_CREDS_PATH)

    start = parse_time_spec(start_spec)
    end = parse_time_spec(end_spec)
    offset = start or 0.0

    if whisper == "captions":
        if not captions_vtt:
            die(ExitCode.BAD_INPUT,
                "whisper=captions requires captions_vtt path")
    elif video:
        emit("start", step="audio_extract", window_start=start, window_end=end)
        t0 = time.time()
        try:
            extract_audio(Path(video), audio_wav, start, end)
        except subprocess.CalledProcessError as e:
            die(ExitCode.IO_FAIL, f"audio extraction failed: {e}")
        emit("complete", step="audio_extract",
             duration_seconds=round(time.time() - t0, 2),
             output=str(audio_wav))
    elif not audio_wav.exists():
        die(ExitCode.BAD_INPUT, "audio.wav missing and video not provided")

    model, language = pick_model_for_provider(whisper, model_name, lang)

    if whisper == "captions":
        segments, info = transcribe_from_captions(
            workdir, Path(captions_vtt), start, end,
        )
        write_offset = 0.0
    elif whisper == "local":
        segments, info = transcribe_local(workdir, model, language)
        write_offset = offset
    elif whisper == "deepgram":
        api_key = resolve_api_key(whisper, whisper_api_key,
                                  Path(whisper_credentials))
        segments, info = transcribe_deepgram(workdir, model, language, api_key)
        write_offset = offset
    elif whisper == "whisperx":
        hf_token = resolve_api_key(whisper, whisper_api_key,
                                   Path(whisper_credentials))
        segments, info = transcribe_whisperx(workdir, model, language, hf_token)
        write_offset = offset
    else:
        api_key = resolve_api_key(whisper, whisper_api_key,
                                  Path(whisper_credentials))
        segments, info = transcribe_hosted(workdir, whisper, model, language, api_key)
        write_offset = offset

    write_outputs(workdir, segments, write_offset)
    speakers_summary = write_speakers_json(workdir, segments, write_offset)

    result = {
        "transcript_txt": str(workdir / "transcript.txt"),
        "transcript_md": str(workdir / "transcript.md"),
        "segments": len(segments),
        "language": info["language"],
        "language_probability": info["language_probability"],
        "offset_seconds": offset,
        "provider": whisper,
        "model": model,
    }
    if speakers_summary:
        result["speakers"] = speakers_summary
        result["speakers_json"] = str(workdir / "speakers.json")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--video", help="source video (extracts audio if audio.wav missing)")
    ap.add_argument("--whisper",
                    choices=("captions", "local", "groq", "openai",
                             "deepgram", "whisperx"),
                    default="local",
                    help="transcription source. 'captions' reads a VTT file "
                         "(free, requires --captions-vtt). 'local'/'groq'/"
                         "'openai' run Whisper (transcription only). "
                         "'deepgram' runs Nova-3 with speaker diarization "
                         "(hosted; ~$0.0043/min). 'whisperx' runs local "
                         "Whisper + pyannote.audio diarization (free + "
                         "offline; requires `pip install pyannote.audio` + "
                         "HF token + accepting terms on the three gated "
                         "pyannote repos; first run downloads ~500 MB-1 GB "
                         "of model weights into ~/.cache/huggingface/). "
                         "Default: local faster-whisper.")
    ap.add_argument("--captions-vtt", default=None,
                    help="path to VTT file for the captions provider. Usually "
                         "supplied by the orchestrator when yt-dlp pulled "
                         "captions during fetch.")
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
    result = run_inproc(
        workdir=Path(args.workdir),
        video=args.video,
        whisper=args.whisper,
        captions_vtt=args.captions_vtt,
        whisper_api_key=args.whisper_api_key,
        whisper_credentials=args.whisper_credentials,
        model_name=args.model,
        lang=args.lang,
        start_spec=args.start,
        end_spec=args.end,
    )
    print(json.dumps(result))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
