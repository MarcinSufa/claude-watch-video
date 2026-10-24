---
name: watch-video
description: Make Claude watch a video - a local file, a public URL (YouTube, Loom, Vimeo and ~1500 other sites via yt-dlp) or a Jira attachment (issue key or URL). Extracts timestamped keyframes with ffmpeg and a transcript (local faster-whisper by default, offline, no key), then Claude reads the frames and transcript to answer the question. Use for screen recordings, bug repro videos, demos, meetings and talks. Triggers - "watch this video", "what happens in this recording", "analyze the bug repro video on PROJ-1234", a video URL with "summarize this"; Polish - "obejrzyj to nagranie", "co jest na tym wideo", "przeanalizuj nagranie błędu", "streść ten film". Not for screenshots or PDFs on a ticket (scripts/fetch_attachment.py downloads those) and not for making videos.
argument-hint: "<video path or URL> [question]"
---

# /watch-video

Arguments: `$ARGUMENTS` (the video first: path, URL, Jira key/URL or `auto`; then the question). If empty, take both from the conversation. Don't ask clarifying questions: infer.

One command turns the video into files on disk: `frames/t_NNN.jpg` (timestamped JPEGs), `transcript.md` (prose paragraphs with `(_MM:SS_)` stamps), `transcript.txt` (one line per Whisper segment) and `meta.json`. You read a chosen subset with the Read tool and answer from what was on screen and what was said.

## 1. Pick flags

| User's intent | Flags |
|---|---|
| "summarize", "transcribe", "what's in this video" | none |
| Specific question ("why X", "what's the bug", "when does Y happen") | none; then grep the transcript and read only the matching moments |
| Long video and you know which part matters | `--start MM:SS --end MM:SS` (frames and audio both; timestamps stay on the original timeline) |
| Video over 20 min | chunks of `--start/--end`; on CPU add `--model tiny.en` (English) or `--model base` (other languages) |
| Polish / non-English / mixed audio | `--lang pl` (or the ISO code); the default is `en`, auto-detect is unreliable on short clips |
| Screen recording with small on-screen text | `--resolution 1280`; `--ocr` only to grep text across many frames (needs Tesseract) |
| Silent screen recording | none (a silent track skips transcription by itself); bound the budget with `--frames N` |
| Narrated video, want fewer frames | `--dedup` (keeps frames near narrated moments; skipped without a transcript) |
| Podcast / interview / meeting | `--whisper deepgram` (key) or `--whisper whisperx` (local, HF token), then relabel speakers |
| User asked in THIS message to post to the ticket | `--post-to-jira` (asks for confirmation; never on your own initiative) |

Transcription defaults to `--whisper auto`: the URL's captions when yt-dlp got a VTT, else local faster-whisper (free, offline, ~25 s cold start then ~0.1 s per second of audio on CPU). Hosted providers are never picked from env vars; pass `--whisper groq|openai|deepgram` yourself only when the key is set and sending audio off the machine is fine.

## 2. Run it

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_video.py" <input> [flags]
```

Windows: `py` instead of `python3`. Workdir defaults to `${TMPDIR:-/tmp}/watch-<slug>/` (`--workdir` overrides it); a re-run with the same flags hits the per-step cache and takes under a second. Stderr carries one JSON progress event per line: don't read it. The JSON block at the end of stdout is `meta.json`.

## 3. Read meta.json

- `transcript`: paths to `transcript.md` / `.txt`, `language`, `provider`, `model`. When it is `null`, `skipped_audio_reason` says why (no audio stream, silent track, `--no-audio`, or `transcription failed (<provider>): <reason>`).
- `frames.frames_dir`, `frames.frame_count`, and `frames.timestamps_by_frame` (`{"t_001.jpg": 0.0, ...}`, the authoritative frame-to-time map).
- `probe.duration` (seconds), `video` (source, Jira issue summary, URL title).
- `warnings`: a list of `{step, msg, exit_code?}` for steps that failed or were skipped (transcription, dedup, OCR, report, highlights). Read it on every run and tell the user what was skipped, e.g. "no transcript: faster-whisper failed, answered from frames only".

| Exit | Meaning | Do |
|---|---|---|
| 0 | done; check `warnings` | answer |
| 2 | bad input (incl. no Jira `credentials.json`) | fix the input, or use the Jira semi-auto fallback |
| 3 | `ffmpeg` (or `yt_dlp` for URLs) missing | tell the user what to install |
| 4 | Atlassian 401 on Jira fetch or `--post-to-jira` | check `credentials.json` |
| 5 | several videos on the Jira issue; stdout lists `candidates` | ask which one, re-run with `--attachment-id <ID>` |
| 6 / 7 | I/O failure / timeout | report it |

Once frames exist, stdout carries meta.json even on a non-zero exit, with an `error: {exit_code, step, msg}` block, so you can still answer from the frames.

## 4. Pick frames and answer

Frames are the expensive part. A 960 px 16:9 frame (the default) costs about 690 tokens, a 1280 px one about 1230. The auto budget is 25 frames for a clip up to 30 s and up to 80 for a long video, so reading every frame of an 80-100 frame run costs 55-70k tokens. Read selectively:

1. Grep `transcript.md` for the words in the question; note the `(_MM:SS_)` stamps of the paragraphs that match. Read whole `transcript.md` only for a summary.
2. Map those times to frames with `frames.timestamps_by_frame` and read the frames around each one, densely near the moment that matters.
3. No question-specific moment (summary, silent video): start with about 8 frames - first, last, and deciles 1, 2, 4, 6, 8, 9 - and add more only where something changes.
4. With `--ocr`, grep `ocr.txt` (`[t_012.jpg @ 00:20]` headers) for on-screen values instead of opening frames.

Answer with:
- a one-line summary of what the video shows;
- a short timeline mapping transcript times to frame evidence;
- the answer itself (bug cause, repro steps, summary);
- citations such as `t_012.jpg @ ~0:20` and `transcript.txt:14`.

Pick highlights and write summaries yourself from the frames and transcript. `--highlights-prompt` sends the transcript to a paid LLM API and is for headless runs (CI, scripts) only.

## 5. Don't

- Don't read the whole transcript and every frame for a targeted question.
- Don't post to Jira (`--post-to-jira`, MCP `post_to_jira(confirm=True)`) unless the user asked for it in the current message.
- Don't run the video pipeline for screenshots or PDFs on a ticket: `python3 "${CLAUDE_SKILL_DIR}/scripts/fetch_attachment.py" PROJ-1234` downloads them and prints their paths.
- Don't answer with `S0`/`S1` after `--whisper deepgram|whisperx`: relabel the speakers first.
- Don't delete the workdir; the user may want to re-run. Clean it up at the end of the session only if they agree.

## References (read only when the case comes up)

- `references/pipeline.md`: every `watch_video.py` flag, input modes, what each step does, frame budget table, known limits. Read when you need a flag not shown above.
- `references/meta-schema.md`: workdir layout, full meta.json schema, report.md, all exit codes, stderr events. Read when you need a field or code not covered above.
- `references/setup.md`: install commands for ffmpeg, faster-whisper (`av<19`), yt-dlp, Pillow, Tesseract. Read on exit 3 or a missing-dependency warning.
- `references/jira.md`: Atlassian token setup, ambiguous attachments, the semi-auto fallback without a token, non-video attachments. Read when the input is a Jira key and the run fails with exit 2, 4 or 5.
- `references/whisper-providers.md`: provider order, hosted key setup, default models, speaker relabeling. Read before passing a non-default `--whisper` or `--model`, or when the transcript has `**S0**` tags.
- `references/post-to-jira.md`: posting the report as a Jira comment, styles, idempotency, headless highlights. Read only after the user asked to post.
- `references/ocr-dedup.md`: how `--ocr` and `--dedup` work and how to tune them. Read when deciding on either or when OCR output looks wrong.
- `references/cache.md`: per-step cache, `--no-cache`, `--force-step`. Read when a re-run ignored a change.
- `references/batch.md`: `watch_batch.py` for many videos or tickets at once (keys, JQL, path lists). Read when the user asks for more than one video.
