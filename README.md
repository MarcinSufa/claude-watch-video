# claude-watch-video

> **Give Claude eyes and ears for any video** — local files, public URLs (YouTube, Loom, Vimeo, TikTok…), or Jira attachments. Fully automated, from input to a paste-ready Markdown evidence bundle.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macOS%20%7C%20linux-lightgrey.svg)](#prerequisites)
[![Skill](https://img.shields.io/badge/Claude%20Code-skill-purple.svg)](https://docs.claude.com/en/docs/claude-code)
[![Plugin](https://img.shields.io/badge/Claude%20Code-plugin-purple.svg)](https://docs.claude.com/en/docs/claude-code/plugins)

This skill turns "watch CON-1234 and tell me what broke" into a single command. It downloads the video, extracts keyframes with ffmpeg, transcribes audio with local or hosted Whisper, deduplicates near-identical frames while preserving narrated moments, optionally OCRs on-screen text, and writes a paste-ready `report.md` — all in under a minute.

---

## Table of contents

- [The 30-second pitch](#the-30-second-pitch)
- [Pipeline](#pipeline)
- [Quick start](#quick-start)
- [Features](#features)
- [How it compares](#how-it-compares)
- [Use cases](#use-cases)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Examples](#examples)
- [Configuration reference](#configuration-reference)
- [Architecture](#architecture)
- [Safety model](#safety-model)
- [Roadmap](#roadmap)
- [License](#license)

---

## The 30-second pitch

Most "watch a video" skills can handle YouTube and call it a day. Bug-triage workflows need more:

- **Jira-native fetch.** Issue tracker hands you `CON-1234`; the skill downloads the video attachment via API token. No clicking through browser, no manual save.
- **Smart dedup that doesn't drop the bug.** Perceptual hash + temporal protection + transcript-aware keep rules. ~50% token reduction on screen recordings *without* losing the moment the user typed the wrong value.
- **OCR you can grep.** Run Tesseract once; from there `grep -i "unload" ocr.txt` answers "when did the user enter 90?" in milliseconds instead of re-reading JPEGs.
- **Per-step cache.** Re-run with a tweaked flag and only the affected tail of the pipeline executes (~0.25s for a full no-op re-run vs ~30s cold).
- **Paste-ready evidence bundle.** `report.md` interleaves transcript paragraphs with frame thumbnails. Drop it into a Jira comment, PR description, or design-review doc as-is.
- **Bulk mode + opt-in Jira posting.** Process a sprint's worth of bug tickets in one command. Optionally — and only when you ask — post the analysis back to each ticket.

---

## Pipeline

```
            input
              │
              ▼
            fetch
              │
              ▼
            probe
             / \
            /   \
        frames   transcribe
            \   /
             \ /
            dedup
              │
              ▼
             ocr
              │
              ▼
           report
              │
              ╎  (opt-in, never default)
              ▼
         Jira comment
```

| Step | What it does | Cached? |
|---|---|---|
| `fetch` | URL → yt-dlp · path → as-is · Jira key → REST API + range download | yes |
| `probe` | ffprobe metadata + audio volume detection | always re-runs (cheap) |
| `frames` | ffmpeg keyframes (uniform or scene-change) | yes |
| `transcribe` | faster-whisper / Groq / OpenAI | yes |
| `dedup` | perceptual hash + transcript-aware protection | yes |
| `ocr` | Tesseract on kept frames (auto-invert, 2x upscale, PSM 6) | yes |
| `report` | `report.md` evidence bundle | yes |
| Jira post | **opt-in only**, never default | n/a |

---

## Quick start

### As a Claude Code plugin

```bash
/plugin marketplace add MarcinSufa/claude-watch-video
/plugin install watch-video@claude-watch-video
```

Then ask Claude:

> Watch PROJ-1234 and tell me what's broken.

### As a Claude Code skill (manual install)

```bash
git clone https://github.com/MarcinSufa/claude-watch-video.git \
  ~/.claude/skills/watch-video
```

Restart Claude Code; the skill auto-loads.

### Direct CLI use

```bash
python scripts/watch_video.py PROJ-1234 --dedup --ocr
```

The full set of capabilities is documented in [`SKILL.md`](SKILL.md). What follows in this README is the marketing tour.

---

## Features

### Multi-source input — auto-detected from a single arg

| Input form | Example | Behavior |
|---|---|---|
| Local file | `c:/path/video.mp4` | Used in place |
| Public URL | `https://youtu.be/...` | `yt-dlp` downloads (1500+ sites supported) |
| Jira issue key | `PROJ-1234` | REST API: list attachments → download MP4 |
| Jira URL | `https://yoursite.atlassian.net/browse/PROJ-1234` | Same as above |
| `auto` | (no arg) | Picks newest video in `~/Downloads/` modified in last 5 min |

### Three Whisper providers, your choice

| `--whisper local` *(default)* | `--whisper groq` | `--whisper openai` |
|---|---|---|
| `faster-whisper` runs on your CPU | Hosted Whisper-large-v3 | OpenAI Whisper-1 |
| Offline, free, no API key | ~10× faster cold-start | Pay-per-second, 25 MB limit |
| Best when you process many videos and don't want recurring cost | Best when you iterate on long videos and want speed | Best if you already have an OpenAI key for other things |

### Smart frame dedup

```
Without dedup       : 25 frames extracted, 21 of them near-identical → wasted tokens
Naive pHash dedup   : 25 → 4 frames, but you LOST the editing sequence (false win)
Smart dedup (ours)  : 25 → 13 frames, all narrated moments preserved + edit-in-progress visible
```

Three keep rules, OR'd together:

1. **Visually distinct** — pHash Hamming distance > threshold
2. **Temporal coverage** — minimum seconds between consecutive keepers
3. **Transcript-aware** — frames near a transcript paragraph start are always kept

The third rule is the key insight: the narrator's voice tells you which moments matter. Don't drop those frames even if they look similar to neighbors.

### OCR layer for on-screen text

For UI bug videos, the bug *is* an on-screen value (e.g. "shows 10 instead of 90"). The OCR step writes `ocr.txt` with one section per frame, so you can grep:

```bash
grep -i "unload" ocr.txt    # find every frame mentioning Unload
```

Tuned for screen recordings: 2× upscale, auto-invert dark-mode UIs, page-segmentation mode 6. Took our test video's OCR quality from unusable noise → bug-grep ready in three lines of preprocessing.

### Per-step cache — re-runs are nearly free

```
Cold run:                  ~30 s
Cached re-run (same flags): ~0.25 s   (120× speedup)
One flag changed:           ~12 s     (only affected steps re-run)
```

Each step records a fingerprint of its inputs + relevant flags. Re-runs check fingerprints + output-file existence; matches skip. A dependency DAG ensures changes propagate correctly — change `--dedup-threshold` and you re-run dedup + ocr + report, not the expensive fetch + transcribe.

### Bulk mode for sprint-scale workflows

```bash
# Comma list of issue keys
python scripts/watch_batch.py --jira-keys PROJ-1234,PROJ-1235,PROJ-1236 --dedup --ocr

# Or a JQL query
python scripts/watch_batch.py --jira-jql "project = PROJ AND labels = video-bug AND created >= -7d" --dedup
```

Each item gets its own workdir; `batch.json` aggregates the per-item meta paths and failure reasons. Continue-on-error by default. **Read-only by design** — bulk mode rejects any Jira-write flag, so a typo can never mass-post to twenty tickets.

### Paste-ready `report.md`

```markdown
# PROJ-1234 — Schedule loads not saving when Spacing is updated

> Evidence bundle — frames + narration captured by /watch-video.

## Source
- Issue: PROJ-1234 (Jira)
- Duration: 41.7s (1280×720)
- Audio: en (p=1.00)

## Timeline

### 00:00
![00:00](frames/t_001.jpg)
> I have an order. Spacing is at 10. I open the schedule.

### 00:13
![00:13](frames/t_005.jpg)
> Going to change Unload to 90. Lock the fields.

### 00:32
![00:32](frames/t_011.jpg)
> The spacing is updated to 20, but my locked Unload values are gone.
> That should not happen.
```

Drop it into a Jira comment, a PR description, or your design doc. Frame paths are relative so Markdown viewers (GitHub, GitLab, Jira) render them inline.

---

## How it compares

| Capability | `claude-watch-video` (this) | `bradautomates/claude-video` (the reference) |
|---|---|---|
| Local file + URL inputs | yes | yes |
| Local Whisper (offline, free) | yes | yes |
| Hosted Whisper (Groq + OpenAI) | yes | yes |
| **Jira attachment auto-download** | **yes** (REST + range-request CDN handling) | no |
| **Multi-attachment disambiguation** | **yes** (AMBIGUOUS exit + candidates JSON) | no |
| Scene-change frame extraction | yes (with uniform-mode fallback) | no |
| **Smart dedup with transcript protection** | **yes** | no |
| **OCR layer** | **yes** (Tesseract, tuned for screen UIs) | no |
| **Per-step cache** | **yes** (~120× re-run speedup) | no |
| **Bulk mode** | **yes** (JQL or comma-list) | no |
| **Opt-in Jira posting** | **yes** (safety stack: confirm, dry-run, idempotency) | no |
| `report.md` evidence bundle | yes (transcript + frame thumbs) | n/a |
| Plugin install (`/plugin install`) | yes | yes |
| Community / battle-testing | new | 1.1k+ stars |

**Honest summary:** for general "watch this YouTube video" tasks, both work fine. For internal bug-triage workflows where the video lives in Jira and the analysis should land back on the ticket, this skill closes the loop end-to-end.

---

## Use cases

### Bug triage from a Jira screen recording

```
Ask Claude:  "Watch PROJ-1234 and tell me what's wrong."

Claude:      Runs the pipeline. Reads frames + transcript. Identifies:
             > The bug is at 0:32 — after changing Spacing on the item row,
             > previously locked Unload values are wiped. Root cause likely in
             > schedule.facade.ts's spacing-change watcher (regenerating loads
             > without respecting the lock state).
```

### Sprint retro on bug videos

```bash
python scripts/watch_batch.py \
  --jira-jql "project = PROJ AND labels = video-bug AND resolved >= -14d" \
  --dedup --ocr --whisper groq
```

20 tickets → 20 reports in ~3 minutes (parallel API for Whisper, sequential ffmpeg). Read them at your retro instead of re-watching each video.

### Researching public content

```bash
python scripts/watch_video.py https://www.youtube.com/watch?v=... --whisper groq
```

The classic. Hands you a transcript + scene-marked screenshot timeline.

### Demo / walkthrough analysis

```bash
python scripts/watch_video.py demo.mp4 --start 2:30 --end 3:00 --ocr
```

Scope to a 30-second window. Transcript timestamps stay offset to the *original* video timeline, not 0-based to the window, so your citations match.

---

## Prerequisites

| Required | Purpose | Install |
|---|---|---|
| Python 3.10+ | runtime | [python.org](https://www.python.org/) |
| `ffmpeg` + `ffprobe` | frame extraction, audio extraction, probing | `winget install Gyan.FFmpeg` · `brew install ffmpeg` · `apt install ffmpeg` |
| `faster-whisper` | local transcription (default) | `pip install --user faster-whisper` |

| Optional | Unlocks | Install |
|---|---|---|
| `yt-dlp` | URL mode (YouTube, Loom, Vimeo, TikTok, ...) | `pip install --user yt-dlp` |
| `Pillow` + `imagehash` | `--dedup` (perceptual hash) | `pip install --user Pillow imagehash` |
| `pytesseract` + Tesseract binary | `--ocr` | See [SKILL.md → OCR setup](SKILL.md#on-screen-text-search-with---ocr) |
| Atlassian API token | Jira auto-fetch / opt-in posting | See [SKILL.md → Jira token setup](SKILL.md#jira-token-setup) |
| `GROQ_API_KEY` or `OPENAI_API_KEY` | hosted Whisper | See [SKILL.md → Whisper providers](SKILL.md#whisper-providers) |

---

## Setup

The plugin / skill install (see [Quick start](#quick-start)) handles the file placement. After that, the optional features are unlocked one at a time as you install their dependencies. Full step-by-step instructions in [`SKILL.md`](SKILL.md).

### Tiered onboarding

```
  1. Install plugin/skill
            │
            ▼
  2. What do you need next?
            │
            ├─ Just local files          → ffmpeg + faster-whisper        (the baseline)
            ├─ YouTube / Loom / etc.     → + yt-dlp
            ├─ Jira auto-fetch           → + Atlassian API token
            ├─ Token efficiency          → + Pillow + imagehash  (--dedup)
            ├─ On-screen text grep       → + Tesseract            (--ocr)
            └─ Faster cold-start         → + GROQ_API_KEY  (hosted Whisper)
```

Each tier is independent. Start with the baseline, add others as your workflow needs them.

---

## Examples

```bash
# Most common: full-auto Jira workflow with all the bells
python scripts/watch_video.py PROJ-1234 --dedup --ocr

# Scope to a 10-second window of a long video
python scripts/watch_video.py PROJ-1234 --start 0:30 --end 0:40

# Fast cold-start with hosted Whisper
python scripts/watch_video.py PROJ-1234 --whisper groq

# Maximum token efficiency for a UI bug repro
python scripts/watch_video.py PROJ-1234 --dedup --ocr

# YouTube clip with bumped resolution for tiny on-screen text
python scripts/watch_video.py "https://youtu.be/abc" --resolution 1280

# Process a sprint's worth of bug tickets at once
python scripts/watch_batch.py \
  --jira-jql "project = PROJ AND labels = video-bug AND created >= -7d" \
  --dedup --ocr

# Tweak one flag — only affected steps re-run (~5s vs ~30s cold)
python scripts/watch_video.py PROJ-1234 --workdir c:/tmp/watch-proj-1234 \
  --dedup --dedup-threshold 3 --ocr
```

---

## Configuration reference

| Flag | Purpose |
|---|---|
| `--workdir <PATH>` | Override the default `c:\tmp\watch-<slug>\` workdir |
| `--start MM:SS --end MM:SS` | Scope to a window; transcript timestamps stay original-video-time |
| `--resolution W` | Frame width in px (default 960; use 1280 for dense UI text) |
| `--frames N` | Override default frame budget (auto by duration) |
| `--scene-mode` | Extract frames at scene cuts (auto fallback to uniform when no scenes) |
| `--dedup` | Smart pHash dedup with transcript-aware protection |
| `--ocr` | Run Tesseract OCR over kept frames |
| `--whisper local\|groq\|openai` | Transcription provider |
| `--model NAME` | Whisper model id |
| `--lang en\|pl\|...\|auto` | Audio language |
| `--no-audio` | Skip transcription |
| `--no-cache` | Bypass the per-step output cache |
| `--force-step NAME[,...]` | Force a specific step (downstream auto-invalidates) |
| `--post-to-jira` | **Opt-in only**: post `report.md` as a Jira comment |
| `--no-report` | Skip `report.md` generation |
| `--attachment-id <ID>` | (Jira) disambiguate multiple video attachments |
| `--credentials <PATH>` | (Jira) override credentials JSON path |

Full reference with every flag, default value, and per-script invocation pattern: [`SKILL.md`](SKILL.md).

---

## Architecture

```
 ┌────────────────────────────────────────────────┐
 │  User input                                    │
 │  path  /  URL  /  Jira key                     │
 └────────────────────┬───────────────────────────┘
                      │
            ┌─────────┴─────────────────┐
            │                           │
            ▼                           ▼
 ┌──────────────────────┐    ┌─────────────────────────┐
 │ watch_video.py       │◀───│ watch_batch.py          │
 │ (single-item)        │    │ (bulk: JQL or key list) │
 └──────────┬───────────┘    └─────────────────────────┘
            │                fan-out: one child per item
            ▼
 ┌────────────────────────────────────────────────┐
 │  Pipeline steps                                │
 │                                                │
 │       fetch.py                                 │
 │          │                                     │
 │          ▼                                     │
 │       probe.py                                 │
 │          │                                     │
 │          ▼                                     │
 │       frames.py                                │
 │          │                                     │
 │          ▼                                     │
 │       transcribe.py                            │
 │          │                                     │
 │          ▼                                     │
 │       dedup.py                                 │
 │          │                                     │
 │          ▼                                     │
 │       ocr.py                                   │
 │          │                                     │
 │          ▼                                     │
 │       report.py                                │
 │          │                                     │
 │          ╎  (user-authorized)                  │
 │          ▼                                     │
 │       post_to_jira.py                          │
 │                                                │
 └────────────────────┬───────────────────────────┘
                      │
                      ▼
 ┌────────────────────────────────────────────────┐
 │  Shared helpers                                │
 │                                                │
 │  _common.py   structured events · exit codes   │
 │               · atomic writes                  │
 │                                                │
 │  _cache.py    step fingerprints · dependency   │
 │               DAG                              │
 └────────────────────┬───────────────────────────┘
                      │
                      ▼
 ┌────────────────────────────────────────────────┐
 │  workdir/                                      │
 │                                                │
 │    frames/                                     │
 │    audio.wav                                   │
 │    transcript.txt  + transcript.md             │
 │    ocr.txt                                     │
 │    report.md                                   │
 │    meta.json                                   │
 └────────────────────────────────────────────────┘
```

Each step is a standalone Python script that can be invoked directly. The orchestrators (`watch_video.py` for single videos, `watch_batch.py` for many) thread them together with caching and structured event emission. Sub-scripts write to `meta.json` as the durable schema-versioned contract; downstream tools (the agent, the orchestrator, future analyzers) read it.

### Engineering guarantees

- **Cross-platform** — pure Python, no PowerShell. Tested on Windows; should run unchanged on macOS/Linux.
- **Atomic writes** — every output stages to `.partial-<uuid>` and renames on success. Failures don't leave half-written artifacts.
- **Deterministic exit codes** — `2` BAD_INPUT, `3` MISSING_DEP, `4` AUTH_FAIL, `5` AMBIGUOUS, `6` IO_FAIL, `7` TIMEOUT.
- **Structured stderr events** — every script emits one JSON object per line for orchestration / progress UIs.
- **Schema-versioned `meta.json`** — durable contract for downstream tooling.
- **Self-contained smoke test** — `scripts/smoketest.py` generates a synthetic 5-second video and validates the full pipeline. Suitable for CI.

---

## Safety model

Some actions affect shared state (Jira tickets) and need to be deliberate. The skill is layered:

```
   ┌──────────────────────┐
   │ watch_video.py runs  │
   └─────────┬────────────┘
             │
   ┌─────────┴───────────────────┐
   │                             │
   ▼  (defaults)                 ▼  (--post-to-jira, opt-in)
                                 │
   Files on disk                 ├─ Confirmation prompt
   in your local workdir         ├─ Idempotency check (skip if already posted)
   (nothing leaves the           │
    machine unless you           ▼
    use a URL / hosted          Jira comment
    Whisper)
```

| Layer | Behavior |
|---|---|
| Default operation | All outputs go to disk in your local workdir. Nothing leaves your machine unless you pass URL mode (which downloads) or hosted Whisper (which uploads audio). |
| Jira fetch (auto-download) | Uses your personal API token, read-only on the Jira side. Stored token is ACL-locked to your user. |
| Jira posting | **Opt-in via `--post-to-jira` flag, never default.** Confirmation prompt unless `--post-to-jira-yes`. Idempotency check skips if a prior `/watch-video` analysis is already commented. Standalone `post_to_jira.py` is available for separate manual posting. |
| Bulk mode | Read-only. **Rejects** `--post-to-jira*` flags with an error to prevent mass-posting accidents. |

For agents driving the skill (Claude itself): the rule is "no Jira writes without explicit user request for that specific action." This is documented inside `SKILL.md` and reinforced in the user's memory if running under Claude Code.

---

## Roadmap

Shipped:
- Multi-source input, three Whisper providers, smart dedup, OCR, per-step cache, bulk mode, opt-in Jira posting

Possible future versions:
- **Image embedding in Jira comments** — upload frame thumbs as attachments and reference them inline in the ADF body (currently rendered as text references)
- **Annotated frames** — timestamp watermark overlaid on each JPG for standalone sharing
- **Speaker diarization** — `Speaker A: ... / Speaker B: ...` labeling for meeting / interview audio (would require `whisperx` or `pyannote.audio`)
- **Confluence video support** — fetch from embedded video macros on Confluence pages

If you want any of these, [open an issue](https://github.com/MarcinSufa/claude-watch-video/issues) — or send a PR.

---

## License

[MIT](LICENSE) — use it, fork it, ship it.

## Acknowledgements

- [`ffmpeg`](https://ffmpeg.org/) — media processing
- [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) — local CPU transcription
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) — URL ingestion across 1500+ sites
- [`Pillow`](https://python-pillow.org/) + [`imagehash`](https://github.com/JohannesBuchner/imagehash) — perceptual hashing
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) — on-screen text extraction
- [Groq](https://groq.com/) and [OpenAI](https://openai.com/) — hosted Whisper APIs
- [`bradautomates/claude-video`](https://github.com/bradautomates/claude-video) — the reference plugin that set the bar
