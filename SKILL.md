---
name: watch-video
description: Make Claude "watch" a video — local file, public URL (yt-dlp), or Jira attachment (fully automated when an Atlassian API token is configured). Extracts keyframes with ffmpeg and transcribes audio with faster-whisper (multilingual), then Claude reads frames + transcript to answer the user's question. Use when the user wants to analyze a screen recording, bug repro video, demo capture, or any video they point at. Triggers - "watch this video", "analyze this video", "what happens in <file>.mp4", "transcribe and watch", "process this bug repro", a Jira ticket key followed by "watch the video", a YouTube/Loom/TikTok/etc. URL with "watch this" or "summarize this".
---

# /watch-video

Give Claude eyes and ears for a video. Output: a directory of timestamped JPEG frames + a timestamped transcript (granular `.txt` + prose `.md`). Claude `Read`s them as multimodal input and answers grounded in what was on screen and what was said.

## Input modes

| Mode | Input | What happens |
|---|---|---|
| **Local file** | `c:\path\video.mp4` | Used in place — no copy |
| **Public URL** | `https://youtu.be/...`, Loom, Vimeo, TikTok, X, ~1500 others | `yt-dlp` downloads into workdir |
| **Jira (full auto)** | `PROJ-1234` or full Jira issue URL — **requires API token configured** (see [Jira token setup](#jira-token-setup) below) | Skill reads token from `credentials.json`, enumerates video attachments via Atlassian REST API, range-downloads the MP4 directly into the workdir. Zero manual steps. |
| **Jira (semi-auto fallback)** | Same input, no token configured | Skill enumerates attachments via Atlassian MCP → asks user to click-download → auto-picks the file from `~/Downloads/` |
| **"I just downloaded it"** | (no path given) | Auto-picks the most recently modified video in `~/Downloads/` from the last 5 minutes |

## When to use

- User shares a video path, URL, or Jira key with words like "watch", "analyze", "transcribe", "summarize", "what's the bug"
- Screen recordings, demo videos, narrated walk-throughs, bug repros
- Use early in a debugging session — frames + narration usually pin down the bug faster than reading the ticket text

## When NOT to use

- Audio-only files — just transcribe directly with `transcribe.py`, no frame extraction needed
- Videos behind auth that *aren't* Jira (private Loom, gated SaaS) — fail gracefully, ask user to download
- User wants to *generate* video — different skill domain entirely

## How it works

A single orchestrator `scripts/watch_video.py` does everything. Sub-scripts can also be run independently.

1. **Resolve input → local path** via `fetch.py` (path / URL / Jira key / auto-downloads).
2. **Probe** via `probe.py` (`ffprobe` + volumedetect): duration, dims, has-audio, mean_volume_db, is_silent.
3. **Pick frame budget** by *window* duration (or full duration if no window) — see table. Hard caps: 2 fps, 100 frames.
4. **Extract frames** via `frames.py` (`ffmpeg`): uniform sampling at computed fps (or scene-change if `--scene-mode`), JPEGs at 960 px wide → `<workdir>/frames/t_NNN.jpg`. Per-frame timestamps stored in `meta.json` (so non-uniform spacing works downstream). Atomic: stages to a sibling dir, renames on success.
5. **Decide on audio.** If `is_silent` or `--no-audio` → skip transcription. Otherwise extract mono 16 kHz `audio.wav` and run `transcribe.py`.
6. **Whisper model** picked by `--lang`:
   - `en` (default) → `small.en` — English-only, ~5% better English accuracy
   - `auto` → `small` (multilingual) — auto-detects 99 languages
   - explicit code (`pl`, `es`, `de`, ...) → `small` multilingual with language forced
   - `--model NAME` overrides everything
7. **`--start`/`--end` window** applies consistently across frames AND audio. Transcript timestamps are offset by `--start` so they map to the *original* video timeline (not 0:00 of the window).
8. **Smart dedup (if `--dedup`)** via `dedup.py` — runs AFTER transcribe so it can use paragraph timestamps as protection. A frame is kept if EITHER it is visually distinct (pHash > threshold), OR enough time has passed since the last keeper (`--dedup-min-interval`), OR it falls within `--dedup-protect-window` of a transcript paragraph start. Updates `meta.json` with the surviving frames.
9. **Write `meta.json`** atomically. This is the durable contract for the agent.
10. **Generate `report.md`** evidence bundle (transcript paragraphs interleaved with frame thumbnails at matched timestamps).
11. **Read everything:** Claude `Read`s a strategic subset of frames (~6–10 spaced across duration, more if the user asks about a specific moment) plus `transcript.md` (or `transcript.txt` for fine-grained mapping).
12. **Answer.** Map narrated moments to visual frames. Cite frame filenames + transcript timestamps.

## Frame budget

| Duration | Frames | fps |
|---|---|---|
| ≤30 s | ~25 | 0.83 |
| 30–60 s | ~25 | 0.6 |
| 1–3 min | ~40 | 0.3 |
| 3–10 min | ~60 | 0.15 |
| > 10 min | ~80 sparse — warn user | auto |

Bump `--resolution 1280` when frames contain hard-to-read on-screen text (code, terminals, tiny UI labels).

## Workspace

Default workdir: `c:\tmp\watch-<slug>\` where `<slug>` is derived from the input (Jira key → `con-8970`; local path → filename stem; URL → last URL segment). Override with `--workdir`.

```
c:\tmp\watch-<slug>\
├── frames\
│   ├── t_001.jpg
│   ├── t_002.jpg
│   └── ...
├── <attachment-or-source>.mp4   # downloaded by fetch.py (jira/url modes)
├── audio.wav                    # extracted when transcribing
├── transcript.txt               # granular, one line per Whisper segment
├── transcript.md                # prose paragraphs, ~8s max
├── ocr.txt                      # only if --ocr ran — per-frame OCR text with timestamps
├── report.md                    # evidence bundle (transcript paragraphs + matched frame thumbs)
└── meta.json                    # durable contract — see Schema below
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
  "elapsed_seconds": 20.77
}
```

- When `transcript` is `null`, `skipped_audio_reason` tells you why (silent track, no audio stream, or `--no-audio`).
- When `report` is `null`, the user passed `--no-report` (or the orchestrator hasn't reached the report step due to a prior failure).
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

## Prereqs (one-time)

- **ffmpeg** on PATH (`winget install Gyan.FFmpeg`)
- **Python 3.10+** on PATH
- **faster-whisper**: `pip install --user faster-whisper`
- **yt-dlp**: `pip install --user yt-dlp` (for URL mode)
- **Pillow + imagehash**: `pip install --user Pillow imagehash` (for `--dedup`)
- **Tesseract + pytesseract** (for `--ocr`):
  - Windows: `winget install UB-Mannheim.TesseractOCR`
  - macOS: `brew install tesseract`
  - Linux: `apt install tesseract-ocr` (or `dnf`/`pacman` equivalent)
  - Plus: `pip install --user pytesseract Pillow`
  - Non-English languages: install the matching tessdata pack (Windows: rerun installer with language options; Linux: `apt install tesseract-ocr-<code>`)

First whisper run downloads the model (~250 MB for `small`/`small.en`) to `~/.cache/huggingface/hub/`. Cached after that.

## Jira token setup

The `--jira-key PROJ-1234` mode is fully automatic (no manual download click) **if** an Atlassian API token is configured. Setup is a one-time, ~3-minute process.

### Step 1 — Create the token

1. Open https://id.atlassian.com/manage-profile/security/api-tokens
2. Sign in with your Atlassian **login email** (this matters — see Step 3)
3. Click **Create API token**
4. Label it something memorable, e.g. `claude-code-watch-video (YYYY-MM)`
5. Copy the token (starts with `ATATT3xFf...`, ~190 chars). **You can only see it once.**

If the **Create API token** button is greyed out or you see a "Restricted by your organization" banner, your admin has disabled user API tokens — the full-auto mode is not available; the skill falls back to semi-auto.

### Step 2 — Save credentials to disk

Create `c:\Users\<you>\.atlassian-token\credentials.json`:

```json
{
  "email": "you@yourcompany.com",
  "token": "ATATT3xFf...",
  "site": "yoursite.atlassian.net"
}
```

The token in this file is plaintext, so **lock down the file ACL** (PowerShell):

```powershell
$path = "$env:USERPROFILE\.atlassian-token\credentials.json"
icacls $path /inheritance:r /grant:r "${env:USERNAME}:R"
```

### Step 3 — Verify

```powershell
python -c "
import json, urllib.request, base64
c = json.load(open(r'$env:USERPROFILE\.atlassian-token\credentials.json'))
auth = base64.b64encode(f\"{c['email']}:{c['token']}\".encode()).decode()
req = urllib.request.Request(f\"https://{c['site']}/rest/api/3/myself\", headers={'Authorization': f'Basic {auth}'})
print(json.load(urllib.request.urlopen(req)).get('displayName'))
"
```

Should print your display name. If it returns **401 Unauthorized**:
- The `email` in `credentials.json` must match the Atlassian **login** account. This is sometimes different from the email shown on your Jira *profile* (e.g. `name@company.com` for login vs `name@subsidiary.com` for profile). Confirm at https://id.atlassian.com/manage-profile/email and update the JSON if needed.
- Verify the token was copied without truncation or extra whitespace (use a "show token" toggle if your editor hides it).
- Confirm `site` is the Jira hostname without `https://` prefix.

### Security notes

- The credentials file is plaintext. ACL lockdown limits damage if your account is compromised, but anyone with admin/system rights on the machine can still read it.
- **Don't share the file via screen recording or email** — the token grants your full Jira/Confluence access.
- **Rotate periodically.** Revoke unused tokens at the same URL where you created them.
- **If the token leaks** (e.g. echoed into a shell log, chat transcript, screenshot), revoke immediately and create a new one.
- Tokens never expire automatically. Old tokens you forget about stay valid forever — set a calendar reminder to audit yearly.

### How the skill uses it

`fetch.py` reads `credentials.json`, calls Atlassian REST API `/rest/api/3/issue/<KEY>?fields=attachment`, picks the (chosen) `video/*` attachment, then range-downloads the binary to `<workdir>/<filename>`. Range requests in 4 MB chunks with retries handle the CDN's early-connection-close behavior on large files. No browser, no manual click.

When multiple video attachments exist, the script exits with code `5` (AMBIGUOUS) and prints the candidates JSON on stdout — the agent then asks the user to pick, and re-runs with `--attachment-id <ID>`.

## Procedure (what I should do when invoked)

The orchestrator does steps 1–6 in one command. The agent (me) handles steps 7–8.

```bash
# Single command — auto-detects input kind. Works for path, URL, Jira key, Jira URL, or "auto".
# Plugin install (recommended):
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_video.py" <input> [flags]
# Manual install at ~/.claude/skills/watch-video/:
python ~/.claude/skills/watch-video/scripts/watch_video.py <input> [flags]
```

`${CLAUDE_PLUGIN_ROOT}` is set by Claude Code when the plugin is installed via `/plugin install`. For manual installs (clone into `~/.claude/skills/<name>/`), substitute the install path. The two forms are otherwise identical.

1. **Run the orchestrator.** Pass `<input>` and any of these optional flags:
   - `--workdir <PATH>` — override default `c:\tmp\watch-<slug>\`
   - `--start MM:SS --end MM:SS` — focus on a window (applies to frames AND audio consistently)
   - `--frames N` — override auto budget
   - `--resolution 1280` — bump for dense on-screen text
   - `--lang auto` (or `pl`, `es`, ...) — non-English audio
   - `--model medium` — better accuracy at 3× cost
   - `--no-audio` — frames only
   - `--attachment-id <ID>` — Jira disambiguation (see ambiguous case below)
   - `--credentials <PATH>` — non-default Atlassian credentials file

2. **Parse stdout** as JSON — this is `meta.json` contents. Check:
   - `transcript` not null → read `transcript.md` (or `.txt` for fine-grained timestamps)
   - `skipped_audio_reason` if no transcript
   - `frames.frame_count` and `frames.frames_dir` for the JPEGs

3. **Handle ambiguous Jira attachments.** If exit code is `5` (AMBIGUOUS), the JSON on stdout contains `"ambiguous": true`, the `issue_summary`, and a `"candidates"` array (each item: `id`, `filename`, `size_bytes`, `created`, `author`). The agent should:

   - List the candidates to the user as a numbered selection: filename, size, who attached it, when. Brief — one line each.
   - Wait for the user's pick.
   - Re-run with `--attachment-id <ID>` (where `<ID>` is the `id` field from the chosen candidate).

   Example:

   ```
   PROJ-1234 has 3 video attachments:
   1. before-fix.mp4 (12 MB, by Jane Doe on 2026-04-10)
   2. after-fix.mp4 (8 MB, by Jane Doe on 2026-04-10)
   3. customer-repro.mov (45 MB, by Support Bot on 2026-04-09)

   Which one should I watch?
   ```

4. **Handle errors.** Non-zero exit codes (see Exit codes section). Auth failures (4) → tell the user to check `credentials.json`. Missing deps (3) → tell them what to install.

5. **Fall back to semi-auto for Jira if no token.** If `--jira-key` exits with code `2` (BAD_INPUT) and the message mentions `credentials not found`, fall back to:
   - Call MCP `getJiraIssue` with `fields=["attachment", "summary"]`
   - Filter attachments to `mimeType` starting with `video/`. If 0 → tell user, stop. If 1 → use it. If >1 → ask user.
   - Print the attachment's web UI URL, ask user to click-download in the browser
   - Run the orchestrator again with `auto` as input — it picks the freshly downloaded MP4
   - Suggest: "to skip the manual download next time, see SKILL.md → Jira token setup"

6. **Read.** Pick frames strategically (default: deciles 1, 2, 4, 6, 8, 9 + first + last → 8 frames). If the user is asking about a specific moment, sample densely around it.

7. **Answer.** Always include:
   - One-line summary of what the video shows
   - Bulleted timeline mapping transcript timestamps to frame evidence
   - The actual answer (bug cause, repro steps, summary)
   - Citation: `t_012.jpg @ ~0:20`, `transcript.txt:14`

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | OK | success |
| 2 | BAD_INPUT | missing arg, file not found, invalid value (incl. missing `credentials.json` for Jira mode) |
| 3 | MISSING_DEP | `ffmpeg`/`faster_whisper`/`yt_dlp` not on PATH |
| 4 | AUTH_FAIL | Atlassian 401 — check email/token in `credentials.json` |
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

## Flags / variants (all on `watch_video.py`)

| Flag | Effect |
|---|---|
| `--workdir <PATH>` | Override the default `c:\tmp\watch-<slug>\` workdir |
| `--no-audio` | Skip transcription even if audio is present |
| `--frames N` | Override default frame budget |
| `--resolution W` | Frame width in px (default 960; use 1280 for dense UI text) |
| `--whisper PROVIDER` | Transcription provider: `local` (faster-whisper, default, offline, no API key) / `groq` / `openai`. See [Whisper providers](#whisper-providers) below. |
| `--whisper-api-key KEY` | One-shot API key. **WARNING:** visible in shell history, process listings, and agent transcripts. Do not use on shared machines or recorded sessions. Prefer env var or credentials file. |
| `--whisper-credentials PATH` | Override default credentials file path (default: `~/.watch-video/credentials.json`). |
| `--model NAME` | Whisper model. Local: `tiny.en` / `small.en` (default for English) / `small` / `medium` / `large-v3`. Groq: `whisper-large-v3` (default) / `whisper-large-v3-turbo` / `distil-whisper-large-v3-en`. OpenAI: `whisper-1` (only option). |
| `--lang CODE` | Language code (`en`, `pl`, `es`, `de`, ...) or `auto` for detection. Default `en`. Auto-switches to multilingual model when not `en` (local only). |
| `--start MM:SS --end MM:SS` | Scope to a window; denser per-second frame budget; transcript timestamps offset to original video |
| `--scene-mode` | Extract frames at scene cuts instead of uniform sampling. Falls back to uniform automatically if too few scene changes (typical for screen recordings). Best for movie clips, slide decks, multi-screen demos. |
| `--scene-threshold F` | Scene-change sensitivity 0.0-1.0 (default 0.3). Lower = more sensitive |
| `--dedup` | Smart dedup: drop near-duplicate frames via perceptual hash, BUT preserve frames that fall near transcript-paragraph timestamps and keep at least one frame per `--dedup-min-interval`. Runs AFTER transcribe so it can use narrated moments as protection. Typical: ~50% token reduction with no loss of narrated content. Requires `pip install --user Pillow imagehash`. |
| `--dedup-threshold N` | pHash Hamming distance for dedup (default 5). Higher = more aggressive. |
| `--dedup-min-interval S` | Minimum seconds between consecutive keepers regardless of pHash similarity (default 5.0). Set lower for movie clips with fast cuts; higher to compress aggressively. |
| `--dedup-protect-window S` | Frames within this many seconds of a transcript paragraph start are kept regardless of pHash similarity (default 1.5). Prevents narrated moments from being deduped away. |
| `--attachment-id <ID>` | (Jira mode) disambiguate when multiple video attachments exist |
| `--credentials <PATH>` | (Jira mode) override default credentials file location |
| `--since-seconds N` | (auto mode) max age of Downloads file (default 300) |
| `--no-report` | Skip `report.md` generation (saves <100ms; rarely worth it) |
| `--no-cache` | Bypass per-step cache; re-run everything from scratch. |
| `--force-step NAME[,...]` | Invalidate specific steps (and their downstream). Useful for forcing one step while keeping upstream cache. |
| `--ocr` | Run Tesseract OCR over kept frames and write `ocr.txt`. **Best for UI bug videos** -- lets you grep on-screen text instead of re-reading every JPEG. ~0.8 s per frame. Requires Tesseract binary + pytesseract + Pillow installed. |
| `--ocr-lang LANG` | Tesseract language code(s). `eng` (default), `pol`, `deu`, or combos like `eng+pol`. Each non-English language requires its tessdata pack installed. |
| `--ocr-min-text-length N` | Frames with fewer than N non-whitespace OCR chars are skipped (default 10). |

Sub-scripts (`fetch.py`, `probe.py`, `frames.py`, `transcribe.py`) can be run independently with their own subset of these flags — useful for incremental re-runs.

## Auth note (Jira)

Two-tier support:

- **Full auto (recommended):** Configure an Atlassian API token once (see [Jira token setup](#jira-token-setup) above). After that, `--jira-key PROJ-1234` does everything — enumerate attachments, pick the video, download. No browser, no manual click. Works because Atlassian Cloud REST API accepts personal API tokens via HTTP Basic Auth, which the skill handles via `urllib` (range requests + retries for CDN robustness on large files).

- **Semi-auto fallback:** If no token is configured (or the org blocks API tokens), the skill uses the Atlassian MCP (OAuth via Claude) to enumerate attachments + print the attachment URL, then asks you to click-download in the browser. `fetch.py --auto-downloads` picks the freshly downloaded MP4 up automatically. Removes the "tell me the path" and "find the attachment" steps, but the click stays.

Note: Atlassian Cloud disabled password-based REST API auth in 2019 — a personal API token is the only practical way to authenticate from scripts. Most orgs allow personal tokens (they're scoped only to *your* permissions), but some admins disable them via the "User API tokens" policy; in that case, semi-auto is the only path.

## Whisper providers

`watch-video` supports three transcription providers via `--whisper`:

| Provider | Latency | Cost | Privacy | API key |
|---|---|---|---|---|
| `local` (default) | Cold start ~25 s (one-time model download) + ~0.1 s per second of audio. Cached after first run. | Free | Offline (audio never leaves your machine) | None |
| `groq` | ~1-2 s for any short clip, often faster than real time | Free tier covers ~14400 s/day; paid is very cheap | Audio sent to Groq servers (deleted per their policy) | `GROQ_API_KEY` |
| `openai` | ~3-5 s per minute of audio | $0.006 / minute | Audio sent to OpenAI (deleted per their policy) | `OPENAI_API_KEY` |

**When to choose what:**

- **Default `local`** — works out of the box, no setup, offline. Best for repeated runs against the same video (cache hit). Slower on cold start.
- **`groq`** — best balance of speed and price. Use when you want fast iteration on many videos. Free tier is generous.
- **`openai`** — choose only if you already have an OpenAI key and prefer their stack. Slower and pricier than Groq for the same task.

### Setting up a hosted provider (one-time, ~2 minutes)

1. Get a key:
   - Groq: <https://console.groq.com/keys> (free signup, free tier covers most personal usage)
   - OpenAI: <https://platform.openai.com/api-keys> (paid; needs billing set up)

2. Choose how to provide it. The skill checks these in order:

   **a) Environment variable (recommended for CI / shared dev environments):**

   ```powershell
   [Environment]::SetEnvironmentVariable('GROQ_API_KEY', 'gsk_...', 'User')
   # or
   [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', 'sk-...', 'User')
   ```

   **b) Credentials file at `~/.watch-video/credentials.json` (recommended for desktop, ACL-lockable):**

   ```json
   {
     "groq_api_key": "gsk_...",
     "openai_api_key": "sk-..."
   }
   ```

   ACL-lock it just like the Atlassian credentials:

   ```powershell
   $path = "$env:USERPROFILE\.watch-video\credentials.json"
   New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
   # ... write the JSON via your editor, then:
   icacls $path /inheritance:r /grant:r "${env:USERNAME}:R"
   ```

   **c) One-shot `--whisper-api-key <KEY>` flag** — for ad-hoc use, not persisted.

3. Verify with a tiny test:

   ```powershell
   python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_video.py" PROJ-1234 --whisper groq --no-report
   ```

   If auth fails you'll get `exit_code: 4` with a helpful message listing all three setup options.

### Security notes (mirror of the Atlassian token guidance)

- The credentials file is plaintext; ACL-lock it.
- Don't commit, screenshot, or paste keys into shared chats. (Same risk as Atlassian.)
- Both providers' keys never expire automatically -- rotate yearly.
- If a key leaks: revoke at the provider's console URL above, generate a new one.

### How the skill picks a model

Default model depends on provider:

- `local --lang en` → `small.en` (English-only, ~5% better accuracy on English)
- `local --lang auto` or other → `small` (multilingual, 99 languages)
- `groq` → `whisper-large-v3` (large-v3 is Groq's free-tier default and very fast)
- `openai` → `whisper-1` (only option)

Override with `--model NAME` -- the skill passes it through to the chosen provider unchanged.

## Posting `report.md` to Jira (opt-in only)

`scripts/post_to_jira.py` takes a workdir and posts the generated `report.md` as a comment on the source Jira issue, via the same Atlassian REST API the skill uses for downloads.

**This feature is opt-in everywhere and never runs automatically.** Specifically:

- `watch_video.py --post-to-jira` defaults OFF. Without the flag, no comment is ever posted.
- `post_to_jira.py` is a standalone tool. It runs only when you explicitly invoke it.
- Even with the flag, the script confirms interactively unless you pass `--post-to-jira-yes`.
- An idempotency check scans recent comments on the ticket; if a prior `/watch-video` analysis is already there, the script aborts (unless `--force`).

The agent guidance is reinforced in memory: never invoke this path silently. If the user hasn't explicitly asked for "post" / "comment on the ticket" / "send to PROJ-XXXX" in the current message, do not pass these flags.

### Usage

Single video, post the analysis after generation:

```bash
# Generate then post in one command (with confirmation prompt):
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_video.py" PROJ-1234 --dedup --ocr --post-to-jira

# Generate first, post later (standalone tool):
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_video.py" PROJ-1234 --dedup --ocr
# ...review report.md, decide it's good...
python "${CLAUDE_PLUGIN_ROOT}/scripts/post_to_jira.py" c:/tmp/watch-proj-1234

# Dry-run preview (no POST):
python "${CLAUDE_PLUGIN_ROOT}/scripts/post_to_jira.py" c:/tmp/watch-proj-1234 --dry-run

# Bypass confirmation (only with explicit prior authorization):
python "${CLAUDE_PLUGIN_ROOT}/scripts/post_to_jira.py" c:/tmp/watch-proj-1234 --yes

# Post to a different ticket than the source (override):
python "${CLAUDE_PLUGIN_ROOT}/scripts/post_to_jira.py" c:/tmp/watch-foo --jira-key PROJ-9999
```

### Flags

| Flag | Effect |
|---|---|
| `--jira-key KEY` | Override the issue key from `meta.json`'s `video.issue_key` (useful for posting to a related ticket). |
| `--dry-run` | Print preview + ADF block summary; DO NOT POST. |
| `--yes` | Skip the interactive confirmation prompt. Use only when you've explicitly authorized this specific post. |
| `--force` | Bypass the idempotency check; post even if a prior `/watch-video` comment already exists. |
| `--no-embed-images` | Skip image embedding (`mediaSingle` ADF nodes); reference frames as italic text. Default behavior is to embed. Useful when the API token user lacks attachment permissions. |
| `--credentials PATH` | Override default credentials JSON location. |

### What gets posted

`report.md` is converted to Atlassian Document Format (ADF) with a minimal converter:

- `# heading` → ADF heading block (level 1-6)
- `> blockquote` → ADF blockquote
- `---` → ADF horizontal rule
- `![alt](path)` → **embedded image** (`mediaSingle` node). Each referenced frame is first uploaded to the ticket as an attachment via `POST /rest/api/3/issue/<KEY>/attachments`, then its media-services UUID is resolved and embedded in the ADF. Pass `--no-embed-images` to fall back to italic text references (the v1 behavior).
- Plain text → ADF paragraphs

The resulting comment renders cleanly on Atlassian Cloud's UI with proper headings, blockquote styling, and inline frame thumbnails. If the API token user lacks the *Add Attachments* permission on the project, the script logs a warning, falls back to text-only references, and still posts the comment.

### Idempotency signature

`report.md`'s footer includes the literal string `Generated by `/watch-video``. The idempotency check walks recent comments looking for that signature. So if you re-run `watch_video.py --post-to-jira` on the same ticket, the second attempt detects the prior comment and skips. To force a duplicate post (e.g., after editing report.md), pass `--force`.

## Per-step cache (resumeable re-runs)

`watch_video.py` records a fingerprint for each step in `meta.json`'s `cache` block. On re-run against the same workdir, steps whose fingerprint AND expected output files match are skipped. Result: re-running on a populated workdir is typically <1 second.

### What gets cached

| Step | Fingerprint inputs | Skipped if... |
|---|---|---|
| `fetch` | input string, kind, attachment-id, credentials path, since-seconds | video file exists at the cached path |
| `probe` | (always re-runs — cheap, <1s) | n/a |
| `frames` | video file fingerprint (size:mtime), `--frames`, `--resolution`, `--start`, `--end`, `--scene-mode`, `--scene-threshold` | `frames/` dir exists |
| `transcribe` | video fingerprint, `--start`, `--end`, `--whisper`, `--model`, `--lang` | `transcript.txt` + `transcript.md` exist |
| `dedup` | frames step fp, transcribe step fp, dedup thresholds | `frames/` dir exists |
| `ocr` | frames step fp, dedup step fp, `--ocr-lang`, `--ocr-min-text-length` | `ocr.txt` exists |
| `report` | frames step fp, transcribe step fp, dedup step fp, ocr step fp | `report.md` exists |

### Dependency-aware invalidation

When a step's fingerprint changes, its downstream consumers are auto-invalidated:

| Changed step | Re-runs (in addition to itself) |
|---|---|
| `fetch` | everything |
| `frames` | dedup, ocr, report |
| `transcribe` | dedup, report (ocr is independent) |
| `dedup` | ocr, report |
| `ocr` | report |
| `report` | nothing |

This means flag tuning is cheap. Tweaking `--dedup-threshold` re-runs only dedup + ocr + report, not the expensive fetch/transcribe.

### CLI flags

| Flag | Effect |
|---|---|
| `--no-cache` | Bypass cache entirely; re-run every step from scratch and overwrite stored fingerprints. |
| `--force-step NAME[,NAME,...]` | Invalidate specific steps. Downstream is invalidated automatically per the table above. Example: `--force-step transcribe` re-runs transcribe + dedup + report. |

### Benchmarks (typical short video)

| Scenario | Elapsed |
|---|---|
| Cold run (no cache) | ~30 s |
| Cached re-run (same flags) | ~0.25 s |
| One downstream flag changed (e.g. `--dedup-threshold`) | ~12-15 s (skips fetch/transcribe; re-runs dedup/ocr/report) |

### meta.json `cache` block

```json
{
  "cache": {
    "schema": 1,
    "steps": {
      "fetch":      {"fingerprint": "abc...", "completed_at": 1234567890, "outputs": ["..."]},
      "frames":     {"fingerprint": "def...", "completed_at": 1234567891, "outputs": ["..."]},
      "transcribe": {"fingerprint": "ghi...", "completed_at": 1234567892, "outputs": ["...", "..."]},
      "dedup":      {"fingerprint": "jkl...", "completed_at": 1234567893, "outputs": ["..."]},
      "ocr":        {"fingerprint": "mno...", "completed_at": 1234567894, "outputs": ["..."]},
      "report":     {"fingerprint": "pqr...", "completed_at": 1234567895, "outputs": ["..."]}
    }
  }
}
```

Note: `probe` doesn't appear in `cache.steps` because it's always re-run (cheap and its result gates transcription).

## Bulk mode: `watch_batch.py`

For processing many videos in one go (sprint retro, weekly support triage, multi-attachment review), use the batch orchestrator:

```bash
# Three input modes -- mutually exclusive
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_batch.py" --jira-keys PROJ-1234,PROJ-1235,PROJ-1236
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_batch.py" --jira-jql "project=PROJ AND labels=video-bug AND created >= -7d"
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_batch.py" --inputs "C:/a.mp4,https://youtu.be/abc,PROJ-9999"
```

Any flag not consumed by `watch_batch.py` is forwarded to each `watch_video.py` invocation:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/watch_batch.py" --jira-keys PROJ-1234,PROJ-1235 --dedup --ocr --whisper groq
```

### Layout

Each item gets its own workdir under `<batch-dir>/<slug>/`:

```
c:\tmp\watch-batch-<timestamp>\
├── batch.json              # per-item results + aggregate summary
├── proj-1234\              # workdir for PROJ-1234 (same layout as single-video)
│   ├── frames\
│   ├── transcript.md
│   ├── ocr.txt
│   ├── report.md
│   └── meta.json
├── proj-1235\
│   └── ...
└── proj-1236\
    └── ...
```

### batch.json schema

```json
{
  "schema_version": 1,
  "batch_dir": "...",
  "started_at": 1778800000,
  "elapsed_seconds": 45.2,
  "forwarded_args": ["--dedup", "--ocr"],
  "summary": {"total": 3, "ok": 2, "ambiguous": 0, "failed": 1},
  "items": [
    {"input": "PROJ-1234", "status": "ok", "workdir": "...", "meta_path": "...",
     "summary": {"issue_key": "PROJ-1234", "duration_seconds": 41.7, "frame_count": 13,
                 "transcript_segments": 14, "language": "en"}},
    {"input": "PROJ-1235", "status": "failed", "error": "No video attachments on PROJ-1235"},
    {"input": "PROJ-1236", "status": "ambiguous", "candidates": [...],
     "hint": "re-run individually with --attachment-id <ID>"}
  ]
}
```

### Failure handling

- **Continue-on-error is the default.** Per-item failures don't abort the batch. Each item's status (`ok` / `ambiguous` / `failed` / `ok_no_meta`) is recorded.
- **`--strict`** flips that: any item failure or ambiguity makes the whole batch return non-zero.
- **Default exit code:** `0` if at least one item succeeded; non-zero only when ALL items failed.

### Ambiguous items

If a Jira ticket has multiple video attachments, that item lands in the `ambiguous` bucket with the candidates list. To resolve: re-run that one ticket directly with `watch_video.py --attachment-id <ID>`. The other items in the batch are unaffected.

### What it does NOT do

- **Does not post anything to Jira.** No comments, no transitions, no edits. Read-only API usage.
- **Sequential by default.** Parallel processing is a future flag; today batch runs items one at a time. For 20 short videos this is typically <5 minutes; for hour-long videos you may want to chunk manually.

## On-screen text search with `--ocr`

For UI bug videos, the bug *is* an on-screen value (e.g. "Unload field shows 10 instead of 90"). Re-reading 13 JPEG frames to find that value is expensive. `--ocr` runs Tesseract over the kept frames and writes `ocr.txt` with `[t_001.jpg @ MM:SS]` headers per frame. Then `grep -i "unload" ocr.txt` pinpoints the moment instantly.

### Pipeline

OCR runs *after* `--dedup`, so it only OCRs the frames that survived. Order: fetch -> probe -> frames -> transcribe -> dedup -> **OCR** -> report.

### Quality tuning

The defaults are tuned for screen recordings:

- **2x upscale** before OCR (Tesseract's sweet spot is ~30px-tall glyphs; 960p UI labels at ~10px confuse it).
- **Auto-invert dark-mode frames** (mean luminance < 128). Tesseract prefers dark-text-on-light.
- **PSM 6** (uniform block of text) outperforms the default PSM 3 on dense UI screens in empirical testing.

Override with `--ocr-lang`, `--ocr-min-text-length`, and (on `ocr.py` directly) `--psm`.

### What to expect

Realistic output quality at default 960px resolution:

- **Big text** (dialog titles, headers, browser chrome) - clean
- **Form labels and values** - mostly readable; minor character substitutions
- **Tiny table cells** - hit and miss; words come through but characters can be wrong
- **Icons and decorative chrome** - sometimes detected as garbage characters; filtered by `--ocr-min-text-length 10`

For dense tables with small text, bump `--resolution 1280` or `--resolution 1536` for both frame extraction and OCR clarity.

## Token optimization with `--dedup`

For long narrated videos (especially screen recordings of UI workflows), `--dedup` is the big lever for cutting Claude's image-token cost while preserving narrated content:

- **Naive pHash dedup** drops near-identical frames -- but UI screen recordings have many large unchanged areas (browser chrome, sidebars), so a small but important change (a typed value, a lock icon flip) can be wrongly classified as a duplicate.
- **Smart dedup** (this skill) adds two protections:
  1. **Temporal** -- never drop two frames more than `--dedup-min-interval` seconds apart, regardless of pHash similarity. Guarantees coverage of slow-changing periods.
  2. **Transcript-aware** -- never drop a frame within `--dedup-protect-window` of a paragraph start in `transcript.md`. The narrator said something important at that moment; visual context must be preserved.

On a typical narrated UI bug repro (~40s screen recording, 4 distinct moments of action):

| Mode | Frames | Coverage of edit moments |
|---|---|---|
| Uniform only | 25 | All edits captured (oversampled) |
| Uniform + naive pHash dedup (no protection) | 4 | Misses 21.7s gap including the entire editing sequence |
| Uniform + **smart dedup** (default settings) | 13 | All 4 narrated moments preserved + edit-in-progress frames visible |

The smart-dedup output is the right balance for narrated UI videos. For movie-style content with fast cuts, set `--dedup-min-interval 0 --dedup-protect-window 0` for maximum compression.

## Known limits

- **No private platform auth.** Public URLs and local files only (Jira via the semi-automated flow). No private Loom, no internal video CDNs without manual download.
- **Long videos.** Past ~10 min, frame sampling gets sparse. Prefer `--start`/`--end` on the part you actually care about.
- **Non-English audio.** Multilingual `small` model is good but not great. For high accuracy in non-English, use `--model medium` (~3× slower, ~2× the model size).
- **Heavy on-screen text.** Default 960 px frames may not render small labels legibly. Bump to 1280 or 1536 when needed.
- **`--dedup` requires transcript for full benefit.** If you pass `--no-audio` together with `--dedup`, transcript protection is unavailable -- you fall back to threshold + temporal only. Still useful, but less precise on narrated content.

## Comparable plugins on the marketplace (for reference)

- `bradautomates/claude-video` (1.1k★) — URL+local, Whisper API (Groq/OpenAI), polished. We chose not to depend on it: needs API key for audio, can't auth Jira either.
- `jordanrendric/claude-video-vision` (592★) — similar premise
- `vusallyv/video-context-plugin` — nearly identical scope; smaller community
- `orbruno/gemini-workflows-ccplugin` — true multimodal via Gemini API; same auth/policy concerns

Our skill is **local by default, hosted optional**: the default `--whisper local` path needs no API keys and works offline. `--whisper groq` and `--whisper openai` are opt-in for users who want faster cold-starts and don't mind sending audio to a hosted service. Either way, the skill is fully auditable and the only recurring cost is your own choice.
