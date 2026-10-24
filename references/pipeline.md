# Pipeline, input modes and all flags

Read when you need a flag that the core SKILL.md does not show, want to know what each step does, or hit a known limit.

## Input modes

| Mode | Input | What happens |
|---|---|---|
| **Local file** | `/path/to/video.mp4` | Used in place — no copy |
| **Public URL** | `https://youtu.be/...`, Loom, Vimeo, TikTok, X, ~1500 others | `yt-dlp` downloads into workdir |
| **Jira (full auto)** | `PROJ-1234` or full Jira issue URL — **requires API token configured** (see [jira.md](jira.md#jira-token-setup)) | Skill reads token from `credentials.json`, enumerates video attachments via Atlassian REST API, range-downloads the MP4 directly into the workdir. Zero manual steps. |
| **Jira attachments that are not video** | `PROJ-1234` + a request about screenshots / a PDF | `scripts/fetch_attachment.py PROJ-1234` downloads them to `<tmp>/jira-<KEY>/` and prints the paths; read those files directly. Not the video pipeline. |
| **Jira (semi-auto fallback)** | Same input, no token configured | Skill enumerates attachments via Atlassian MCP → asks user to click-download → auto-picks the file from `~/Downloads/` |
| **"I just downloaded it"** | (no path given) | Auto-picks the most recently modified video in `~/Downloads/` from the last 5 minutes |

## When NOT to use

- Audio-only files — just transcribe directly with `transcribe.py`, no frame extraction needed
- Videos behind auth that *aren't* Jira (private Loom, gated SaaS) — fail gracefully, ask user to download
- User wants to *generate* video — different skill domain entirely

## How it works

A single orchestrator `scripts/watch_video.py` does everything. Sub-scripts can also be run independently.

1. **Resolve input → local path** via `fetch.py` (path / URL / Jira key / auto-downloads).
2. **Probe** via `probe.py` (`ffprobe` + volumedetect): duration, dims, has-audio, mean_volume_db, is_silent.
3. **Pick frame budget** by *window* duration (or full duration if no window) — see table. Caps: 2 fps; the automatic budget tops out at 80 frames.
4. **Extract frames** via `frames.py` (`ffmpeg`): uniform sampling at computed fps (or scene-change if `--scene-mode`), JPEGs at 960 px wide → `<workdir>/frames/t_NNN.jpg`. Per-frame timestamps stored in `meta.json` (so non-uniform spacing works downstream). Atomic: stages to a sibling dir, renames on success.
5. **Decide on audio.** If `is_silent` or `--no-audio` → skip transcription. Otherwise extract mono 16 kHz `audio.wav` and run `transcribe.py`.
6. **Whisper model** picked by `--lang`:
   - `en` (default) → `small.en` — English-only, ~5% better English accuracy
   - `auto` → `small` (multilingual) — auto-detects 99 languages
   - explicit code (`pl`, `es`, `de`, ...) → `small` multilingual with language forced
   - `--model NAME` overrides everything
7. **`--start`/`--end` window** applies consistently across frames AND audio. Transcript timestamps are offset by `--start` so they map to the *original* video timeline (not 0:00 of the window).
8. **Smart dedup (if `--dedup`)** via `dedup.py` — runs AFTER transcribe so it can use paragraph timestamps as protection. A frame is kept if EITHER it is visually distinct (pHash > threshold), OR enough time has passed since the last keeper (`--dedup-min-interval`), OR it falls within `--dedup-protect-window` of a transcript paragraph start. Updates `meta.json` with the surviving frames. **Skipped when nothing protects a frame** -- no transcript at all (`--no-audio`, no audio stream, a silent track) or a transcript that parsed to zero paragraphs. The temporal floor is then the only guard left, which on a screen recording deletes exactly the small UI changes the run exists to capture. The skip is announced as a `warning` event with the reason. `dedup.py` enforces the paragraph case itself, so a direct call is covered too.
9. **Write `meta.json`** atomically. This is the durable contract for the agent.
10. **Generate `report.md`** evidence bundle (transcript paragraphs interleaved with frame thumbnails at matched timestamps).
11. **Read everything:** Claude `Read`s a strategic subset of frames (~6–10 spaced across duration, more if the user asks about a specific moment) plus `transcript.md` (or `transcript.txt` for fine-grained mapping).
12. **Answer.** Map narrated moments to visual frames. Cite frame filenames + transcript timestamps.

## Frame budget

| Duration | Frames | fps |
|---|---|---|
| ≤30 s | 25 | ≥0.83 |
| 30–60 s | 30 | 0.5–1 |
| 1–3 min | 45 | 0.25–0.75 |
| 3–10 min | 60 | 0.1–0.33 |
| > 10 min | 80 sparse — warn user | auto |

Bump `--resolution 1280` when frames contain hard-to-read on-screen text (code, terminals, tiny UI labels).

## Flags / variants (all on `watch_video.py`)

| Flag | Effect |
|---|---|
| `--workdir <PATH>` | Override the default `${TMPDIR:-/tmp}/watch-<slug>/` workdir (Windows: `c:\tmp\watch-<slug>\`) |
| `--no-audio` | Skip transcription even if audio is present |
| `--frames N` | Override default frame budget |
| `--resolution W` | Frame width in px (default 960; use 1280 for dense UI text) |
| `--whisper PROVIDER` | Transcription provider: `auto` (default: `captions` when yt-dlp pulled a VTT, else `local`) / `captions` / `local` (faster-whisper, offline, no API key) / `groq` / `openai` / `deepgram` / `whisperx`. See [whisper-providers.md](whisper-providers.md). |
| `--whisper-api-key KEY` | One-shot API key. **WARNING:** visible in shell history, process listings, and agent transcripts. Do not use on shared machines or recorded sessions. Prefer env var or credentials file. |
| `--whisper-credentials PATH` | Override default credentials file path (default: `~/.watch-video/credentials.json`). |
| `--model NAME` | Whisper model. Local: `tiny.en` / `small.en` (default for English) / `small` / `medium` / `large-v3`. Groq: `whisper-large-v3` (default) / `whisper-large-v3-turbo` / `distil-whisper-large-v3-en`. OpenAI: `whisper-1` (only option). |
| `--lang CODE` | Language code (`en`, `pl`, `es`, `de`, ...) or `auto` for detection. Default `en`. Auto-switches to multilingual model when not `en` (local only). |
| `--start MM:SS --end MM:SS` | Scope to a window; denser per-second frame budget; transcript timestamps offset to original video |
| `--scene-mode` | Extract frames at scene cuts instead of uniform sampling. Falls back to uniform automatically if too few scene changes (typical for screen recordings). Best for movie clips, slide decks, multi-screen demos. |
| `--scene-threshold F` | Scene-change sensitivity 0.0-1.0 (default 0.3). Lower = more sensitive |
| `--dedup` | Smart dedup: drop near-duplicate frames via perceptual hash, BUT preserve frames that fall near transcript-paragraph timestamps and keep at least one frame per `--dedup-min-interval`. Runs AFTER transcribe so it can use narrated moments as protection. Typical: ~50% token reduction with no loss of narrated content. **Recordings with no transcript paragraphs are not deduped at all** (nothing to protect frames with); use `--frames N` to bound the budget there instead. Requires `pip install --user Pillow imagehash`. |
| `--dedup-threshold N` | pHash Hamming distance for dedup (default 5). Higher = more aggressive. |
| `--dedup-min-interval S` | Minimum seconds between consecutive keepers regardless of pHash similarity (default 5.0). Set lower for movie clips with fast cuts; higher to compress aggressively. |
| `--dedup-protect-window S` | Frames within this many seconds of a transcript paragraph start are kept regardless of pHash similarity (default 1.5). Prevents narrated moments from being deduped away. |
| `--attachment-id <ID>` | (Jira mode) disambiguate when multiple video attachments exist |
| `--credentials <PATH>` | (Jira mode) override default credentials file location |
| `--since-seconds N` | (auto mode) max age of Downloads file (default 300) |
| `--no-report` | Skip both `report.md` AND `report.html` generation (saves <100ms; rarely worth it). To skip only HTML, pass `--no-html` through to `report.py` directly. |
| `--no-cache` | Bypass per-step cache; re-run everything from scratch. |
| `--force-step NAME[,...]` | Invalidate specific steps (and their downstream). Useful for forcing one step while keeping upstream cache. |
| `--ocr` | Run Tesseract OCR over kept frames and write `ocr.txt`. **Best for UI bug videos** -- lets you grep on-screen text instead of re-reading every JPEG. ~0.8 s per frame from the CLI; 1.7-2.3 s per frame plus a cold start of up to a minute under an MCP host on Windows. Requires Tesseract binary + pytesseract + Pillow installed; without Tesseract the run continues with a `warnings` entry. |
| `--highlights-prompt "..."` | Optional, paid, for headless runs. Calls an LLM API (Anthropic by default) with the transcript + prompt and writes `highlights.json`, `highlights.md`, `highlights.html`; `post_to_jira.py --style summary` then uses those picks. Needs a transcript and an API key; without either the run continues with a `warnings` entry. In a Claude Code session, pick the moments yourself instead (see [post-to-jira.md → Two ways to generate highlights](post-to-jira.md#two-ways-to-generate-highlights)). |
| `--highlights-max-n N` | Max highlights the LLM may pick (default 5). |
| `--highlights-provider P` | `anthropic` (default) / `openai` / `groq` / `deepseek` / `gemini` / `openai-compat` (+ `--highlights-base-url`). |
| `--highlights-model NAME` | Model id for the highlights provider (default per provider, see `scripts/highlights.py`). |
| `--highlights-api-key KEY` / `--highlights-credentials PATH` | Key for the highlights provider; otherwise the provider's env var (`ANTHROPIC_API_KEY`, ...) or `~/.watch-video/credentials.json`. |
| `--ocr-lang LANG` | Tesseract language code(s). `eng` (default), `pol`, `deu`, or combos like `eng+pol`. Each non-English language requires its tessdata pack installed. |
| `--ocr-min-text-length N` | Frames with fewer than N non-whitespace OCR chars are skipped (default 10). |

Sub-scripts (`fetch.py`, `probe.py`, `frames.py`, `transcribe.py`) can be run independently with their own subset of these flags — useful for incremental re-runs.

## Known limits

- **No private platform auth.** Public URLs, local files and Jira (fully automatic with an API token, semi-auto download without one). No private Loom, no internal video CDNs without manual download.
- **Long videos.** Past ~10 min, frame sampling gets sparse. Prefer `--start`/`--end` on the part you actually care about.
- **Non-English audio.** Multilingual `small` model is good but not great. For high accuracy in non-English, use `--model medium` (~3× slower, ~2× the model size).
- **Heavy on-screen text.** Default 960 px frames may not render small labels legibly. Bump to 1280 or 1536 when needed.
- **`--dedup` needs a transcript.** With `--no-audio`, a silent track or a failed transcription, dedup is skipped (a `warnings` entry says so) because only the temporal floor would protect frames. Bound the budget with `--frames N` instead.
