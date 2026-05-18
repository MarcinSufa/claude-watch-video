# Anthropic Marketplace Submission — claude-watch-video

Submission form: <https://claude.ai/settings/plugins/submit> or <https://platform.claude.com/plugins/submit>.
Repository: <https://github.com/MarcinSufa/claude-watch-video>

The fields below are typical of plugin-submission forms. If a field isn't asked, skip it. If asked but not drafted here, refer to the public README.

---

## Plugin name

watch-video

## Marketplace name

claude-watch-video

## Repository URL

<https://github.com/MarcinSufa/claude-watch-video>

## Install command

```
/plugin marketplace add MarcinSufa/claude-watch-video
/plugin install watch-video@claude-watch-video
```

## Short tagline (under 90 chars)

Give Claude eyes and ears for any video — local files, public URLs, or Jira attachments.

## Full description (3-5 paragraphs)

`watch-video` turns "watch this video and tell me what matters" into a single command. Point it at a local MP4, a public URL (YouTube, Loom, Vimeo, TikTok, or ~1,500 sites via `yt-dlp`), or a Jira issue key with a video attachment, and the skill produces a paste-ready evidence bundle: timestamped JPEG frames, a Whisper transcript (local or hosted), an optional OCR pass for on-screen text, and three formats of report — `report.md`, self-contained `report.html` (base64-embedded frames), and corporate-friendly `report.docx`.

Beyond the raw transcript, the skill includes an **LLM-driven highlights step**: you pass a prompt like *"summarize the rate decision, inflation outlook, and rate-path forecast"* or *"identify the actual bug and the moment it occurs"*, and Claude (via the Anthropic API) picks the N most relevant moments and emits `highlights.md`, `highlights.html`, and `highlights.json` — frame thumbnails + bold "why this matters" + verbatim transcript quote per pick. The README walks through two real runs: a Powell FOMC press conference (5:30, full macro analysis) and a Claude Code release-notes video (54s, personal-workflow analysis), both with actual rendered artifacts.

For Jira-native workflows, the skill posts the analysis back to the source ticket as a comment **only when explicitly asked** — opt-in by design, with a confirmation gate that runs *before* any attachment uploads (declining leaves the ticket completely untouched, no orphan state). Three post styles let you tailor for compactness: `collapsed` (default, full timeline wrapped in an expand panel), `inline` (legacy, full timeline expanded), and `summary` (key moments + downloadable `report.html`). Batch mode processes a full sprint of bug tickets in one command via `--jira-keys` or `--jira-jql`.

Under the hood: per-step content-hash cache (re-runs are nearly free), perceptual-hash dedup with transcript-aware protection (preserves narrated moments while typically dropping 40-60% of redundant frames on screen recordings), OCR via Tesseract with 2x upscale + auto-invert + PSM 6 (works on dark UI screen recordings), atomic writes via `.partial-<uuid>` staging, and structured JSON-line stderr events with deterministic exit codes for scripted use.

## Category

productivity

## Keywords / tags

video, transcription, whisper, ffmpeg, jira, bug-repro, screen-recording, yt-dlp, multimodal, ocr, llm-summary, highlights

## Prerequisites

- Python 3.10+
- `ffmpeg` + `ffprobe` (`winget install Gyan.FFmpeg` / `brew install ffmpeg` / `apt install ffmpeg`)
- `faster-whisper` (`pip install --user faster-whisper`) for local transcription

Optional dependencies unlock more features:
- `yt-dlp` — URL ingestion
- `Pillow` + `imagehash` — smart dedup
- `pytesseract` + Tesseract binary — OCR layer
- `anthropic` Python SDK — LLM-driven highlights (one of the two demoed flows)
- `python-docx` — Word-compatible report.docx (degrades gracefully if missing)
- Atlassian API token — Jira ingestion / Jira posting

## Screenshots / examples

The repository README has two real end-to-end walkthroughs with embedded frame images:

- **Powell FOMC March 2026 statement** (5:30 video, 65s wall-clock end-to-end): <https://github.com/MarcinSufa/claude-watch-video/blob/main/docs/walkthrough-fomc.md>
- **Claude Code v2.1.142 release-notes video** (54s video, 29s wall-clock): <https://github.com/MarcinSufa/claude-watch-video/blob/main/docs/walkthrough-claude-code-release.md>

Hero rendered frame (FOMC 00:22 rate-decision moment): <https://github.com/MarcinSufa/claude-watch-video/blob/main/docs/images/fomc/00-22-rate-decision.jpg>

## Latest version

v1.12.0 (commit `63088e1`) — released 2026-05-15. See <https://github.com/MarcinSufa/claude-watch-video/releases/tag/v1.12.0> for the changelog.

## Safety / permissions

- **No unsolicited Jira writes.** The `--post-to-jira` flag is opt-in and never default. The confirmation gate runs *before* any attachment upload, so declining leaves the ticket completely untouched.
- **Idempotency check** — refuses to post a duplicate comment if a signature from a prior `/watch-video` post is found in the last N comments (overridable with `--force`).
- **Tokens never logged.** Credentials are read from env vars or local JSON files, never echoed. Two separate credential stores, each scoped to its own provider:
  - Atlassian/Jira fetch + post: `~/.atlassian-token/credentials.json` (or `--credentials <path>`).
  - Anthropic (LLM highlights): `ANTHROPIC_API_KEY` env, or `anthropic_api_key` field in `~/.watch-video/credentials.json` (or `--highlights-credentials <path>`).
  - Hosted Whisper (Groq / OpenAI): own keys via `~/.watch-video/credentials.json` or `--whisper-credentials <path>`. Local `faster-whisper` needs no credentials.
- **Atomic writes** — `.partial-<uuid>` staging so a `Ctrl-C` mid-pipeline never leaves a corrupt artifact in the workdir.
- **Batch mode rejects write flags** — `watch_batch.py` refuses `--post-to-jira` to prevent fan-out misfires.

## Author

Marcin Sufa — <sufa.marcin@gmail.com>

## License

MIT
