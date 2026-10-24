# Workdir layout, meta.json, exit codes, stderr events

Read when you need a meta.json field the core SKILL.md does not cover, an exit code's exact meaning, or to parse the stderr progress events.

## Workspace

Default workdir: `${TMPDIR:-/tmp}/watch-<slug>/` (Windows: `c:\tmp\watch-<slug>\`) where `<slug>` is derived from the input (Jira key → `con-8970`; local path → filename stem; URL → last URL segment). Override with `--workdir`.

```
${TMPDIR:-/tmp}/watch-<slug>/
├── frames/
│   ├── t_001.jpg
│   ├── t_002.jpg
│   └── ...
├── <attachment-or-source>.mp4   # downloaded by fetch.py (jira/url modes)
├── audio.wav                    # extracted when transcribing
├── transcript.txt               # granular, one line per Whisper segment
├── transcript.md                # prose paragraphs, ~8s max
├── ocr.txt                      # only if --ocr ran — per-frame OCR text with timestamps
├── report.md                    # evidence bundle (Markdown, relative frame paths)
├── report.html                  # same content, base64-embedded frames — open in any browser
├── report.docx                  # Office-compatible Word doc with native image embedding
├── highlights.md                # only if highlights ran — LLM-picked moments with reasons
├── highlights.html              # same, self-contained with base64 frames
├── highlights.json              # raw picks (consumed by post_to_jira summary mode)
└── meta.json                    # durable contract — see schema below
```

Cleanup: leave the workdir in place after analysis so the user can rerun. Remove at end of session if the user agrees.

### meta.json schema (v2)

```json
{
  "schema_version": 2,
  "workdir": "...",
  "input": {"raw": "PROJ-1234", "kind": "jira|url|path|auto", "value": "..."},
  "video": {
    "path": "...",
    "source": "jira|url|path|auto-downloads",
    "size_bytes": 32241434,
    "mtime": 1778763363,
    // Jira mode additional fields:
    "issue_key": "...", "issue_summary": "...", "attachment_id": "...",
    "attachment_name": "...", "mime_type": "video/mp4", "site": "...",
    // URL mode additional fields:
    "source_url": "...", "title": "...", "uploader": "...",
    "extractor": "youtube", "upload_date": "20050424"
  },
  "probe": {
    "duration": 41.7, "width": 1280, "height": 720,
    "video_codec": "h264", "bit_rate": 6180466, "size_bytes": 32241434,
    "has_audio": true, "audio_codec": "aac", "audio_channels": 2, "audio_sample_rate": 48000,
    "mean_volume_db": -23.8, "is_silent": false
  },
  "frames": {
    "frames_dir": "...",
    "frame_count": 13,
    "mode": "uniform|uniform-fallback|scene-change",
    "resolution": 960,
    "window_start": null, "window_end": null, "window_seconds": 41.73,
    "scene_threshold": null,
    "timestamps_by_frame": {"t_001.jpg": 0.0, "t_002.jpg": 5.01, "...": "..."},
    "dedup": {
      "threshold": 5, "min_interval": 5.0, "protect_window": 1.5,
      "before": 25, "after": 13, "dropped": 12,
      "kept_by_temporal_protection": 5, "kept_by_transcript_protection": 5,
      "protected_timestamps": [0.0, 13.0, 21.0, 32.0]
    }
  },
  "transcript": {
    "transcript_txt": "...", "transcript_md": "...",
    "segments": 14, "language": "en", "language_probability": 1.0,
    "offset_seconds": 0.0, "provider": "local|groq|openai", "model": "small.en"
  },
  "skipped_audio_reason": null,
  "window": {"start": null, "end": null},
  "ocr": {
    "path": "...",
    "frames_total": 13, "frames_with_text": 13,
    "language": "eng", "min_text_length": 10,
    "elapsed_seconds": 10.55
  },
  "report": {"report_path": "..."},
  "warnings": [],
  "elapsed_seconds": 20.77
}
```

- When `transcript` is `null`, `skipped_audio_reason` tells you why (silent track, no audio stream, `--no-audio`, or `transcription failed (<provider>): <reason>`).
- `warnings` lists non-fatal problems of this run as `{step, msg, exit_code?}`: dedup skipped (no transcript), dedup/OCR/report/highlights failed, `--highlights-prompt` ignored for lack of a transcript, transcription failed. Reset on every run.
- `error` (`{exit_code, step, msg}`) appears only on a non-zero exit after frames existed; stdout still carries the partial meta.
- When `report` is `null`, the user passed `--no-report` (or the report step failed, see `warnings`).
- `frames.timestamps_by_frame` is the authoritative source for per-frame timestamps (works for both uniform and scene-change modes; needed because non-uniform spacing breaks fps-based math).
- `frames.dedup` only present when `--dedup` ran.

**Schema changes from v1:**
- Added `report` field.
- Added optional `ocr` field (populated when `--ocr` is enabled).
- `frames.fps` removed in favor of `frames.timestamps_by_frame` (durable per-frame timestamps).
- `frames.mode`, `frames.dedup`, `frames.scene_threshold` added.
- `transcript.provider` and `transcript.model` added.
- `video.source_url`, `video.title`, `video.uploader`, `video.site` added.
- `schema_version` bumped to 2.

### report.md — evidence bundle

A standalone Markdown document interleaving transcript prose with embedded frame thumbnails (one frame per paragraph, picked by closest timestamp). Generated by `report.py`, written atomically.

- Designed to be **paste-ready into Jira comments / PR descriptions / dev handoffs** — frames render inline in any Markdown-aware viewer (Jira renders relative-path images, GitHub does too).
- Contains: title (Jira issue summary if applicable), source block (duration, dims, audio, language), timeline (paragraphs + frames).
- **Does NOT contain analysis** — the agent's bug summary / root cause is meant to live above the "Evidence bundle" marker (or in a separate doc). `report.md` is *what happened in the video*, not *what's wrong with the system*.
- Silent videos: timeline falls back to a frame grid (each frame timestamped).

Skip generation with `--no-report` if you only need raw artifacts.

## Exit codes

For `watch_video.py`, a non-zero exit means fetch, probe or frames failed (nothing usable on disk), or `--post-to-jira` failed. Once frames exist, stdout always carries meta.json, also on a non-zero exit (then with an `error: {exit_code, step, msg}` block). Optional steps after frames never change the exit code: a failed transcription continues frames-only with `skipped_audio_reason`, and failed dedup / OCR (e.g. no Tesseract) / report / highlights (e.g. no API key) become entries in meta.json `warnings`. Always read `warnings`.

| Code | Name | Meaning |
|---|---|---|
| 0 | OK | success (check `warnings` and `skipped_audio_reason`) |
| 2 | BAD_INPUT | missing arg, file not found, invalid value (incl. missing `credentials.json` for Jira mode) |
| 3 | MISSING_DEP | `ffmpeg` (or `yt_dlp` for URLs) not on PATH. Missing faster-whisper / Tesseract / Pillow after frames is a warning, not an exit |
| 4 | AUTH_FAIL | Atlassian 401 on Jira fetch or `--post-to-jira` — check email/token in `credentials.json`. (A missing highlights or hosted-Whisper key is a warning, not an exit) |
| 5 | AMBIGUOUS | multiple matches (e.g. multiple video attachments on a Jira issue). Stdout has `candidates`. |
| 6 | IO_FAIL | disk full, write failed, corrupted download |
| 7 | TIMEOUT | network/subprocess timeout |

## Structured stderr events

Each script emits one JSON object per line on stderr. The orchestrator (`watch_video.py`) propagates these so the parent (me) sees the full timeline. Useful events:

```json
{"ts": ..., "event": "start", "step": "download", "filename": "...", "size_bytes": 32241434}
{"ts": ..., "event": "progress", "step": "download", "bytes": 16777216, "total": 32241434}
{"ts": ..., "event": "complete", "step": "transcribe", "duration_seconds": 4.19, "segment_count": 14}
{"ts": ..., "event": "warning", "step": "jira_attachments", "msg": "...", "count": 2}
{"ts": ..., "event": "error", "exit_code": 4, "msg": "Atlassian auth failed..."}
```

If you need to display progress to the user during a long run (large download, slow whisper model), parse these.
