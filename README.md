# claude-watch-video

> **Give Claude eyes and ears for any video** — local files, public URLs (YouTube, Loom, Vimeo, TikTok…), or Jira attachments. Pipeline runs locally on your machine; produces a paste-ready Markdown evidence bundle.

[![smoketest](https://github.com/MarcinSufa/claude-watch-video/actions/workflows/smoketest.yml/badge.svg)](https://github.com/MarcinSufa/claude-watch-video/actions/workflows/smoketest.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macOS%20%7C%20linux-lightgrey.svg)](#prerequisites-any-path)
[![Plugin](https://img.shields.io/badge/Claude%20Code-plugin-purple.svg)](https://docs.claude.com/en/docs/claude-code/plugins)

Turn *"watch CON-1234 and tell me what broke"* into a single command. The skill downloads the video, extracts keyframes with ffmpeg, transcribes audio with local or hosted Whisper, deduplicates near-identical frames while preserving narrated moments, optionally OCRs on-screen text, and writes a paste-ready `report.md`. Costs **$0 to a few cents** per video. See [what it costs](#what-it-costs) for the breakdown.

---

## What it produces

A 5:30 FOMC press conference distilled to one moment, picked by Claude against the prompt *"summarize the rate decision, inflation outlook, and rate-path forecast"*:

> **00:22 — the rate decision**
>
> <img src="docs/images/fomc/00-22-rate-decision.jpg" width="420" alt="Powell at 00:22 announcing the rate hold">
>
> *"Today, the FOMC decided to leave our policy rate unchanged. We see the current stance of monetary policy as appropriate to promote progress toward our maximum employment..."* Committee judges current policy stance appropriate for the dual mandate.

That's one card from a real `highlights.md` rendered inline. The pipeline also produces a full `report.md` evidence bundle (transcript + all timestamped frames), `report.html`, `report.docx`, and machine-readable `highlights.json`. **65 seconds end-to-end, $0** with local Whisper. See the [Powell FOMC walkthrough](docs/walkthrough-fomc.md) for the full artifacts, dedup metrics, and per-step timings.

---

## Install — pick one

### Claude Code (recommended)

```bash
/plugin marketplace add MarcinSufa/claude-watch-video
/plugin install watch-video@claude-watch-video
```

Then ask Claude: *"Watch https://youtu.be/… and tell me what's in it."* or *"Watch PROJ-1234 and identify the bug."*

Works on every Claude Code surface — CLI, IDE extensions, desktop. The plugin auto-installs the same underlying skill (see [`SKILL.md`](SKILL.md) for the manual install path if you prefer to clone into `~/.claude/skills/` directly).

### CLI direct (CI / scripting / power users)

```bash
git clone https://github.com/MarcinSufa/claude-watch-video.git
python claude-watch-video/scripts/watch_video.py "<url>" --workdir /tmp/test --dedup --verbose
```

Zero-host, zero-MCP, fastest path on any platform. Produces `transcript.md`, `frames/`, `report.md` / `.html` / `.docx`. Good for CI pipelines, batch processing, or driving the analysis yourself.

### Other MCP hosts (Claude Desktop, Cursor, Cline, Codex CLI, …)

An MCP server install is available — full instructions in [`mcp-server/README.md`](mcp-server/README.md).

> ⚠️ **Honest warning:** on Windows + Claude Desktop the first run is **slow (~2-3 minutes)** because Windows Defender scans the freshly-spawned Python subprocess. The Claude Code plugin path above bypasses that entirely, and the CLI bypasses MCP entirely. **If you have the choice, use one of those.** The MCP path exists for environments where neither is an option.

> **Codex CLI / Agent Skills note:** OpenAI's [openai/skills](https://github.com/openai/skills) framework presents skills as portable Agent Skills, but the install mechanism is still evolving. For Codex CLI today, **the CLI direct path above works without any agent-side install** (just `python scripts/watch_video.py ...` from your shell). If you specifically need MCP integration, the MCP path is available; if a future Codex skill-installer convention lands, this README will be updated.

### Prerequisites (any path)

Required: **Python 3.10+** and **`ffmpeg`** (which bundles `ffprobe`).

Install ffmpeg per platform:

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux (Debian/Ubuntu) | `sudo apt install ffmpeg` |
| Linux (Fedora/RHEL) | `sudo dnf install ffmpeg` |

Optional (unlock features as you need them):

| Adds | Install |
|---|---|
| URL mode (YouTube, Loom, …) | `pip install --user yt-dlp` |
| Local transcription (`--whisper local`) | `pip install --user faster-whisper` |
| Smart dedup (`--dedup`) | `pip install --user Pillow imagehash` |
| OCR (`--ocr`) | `pip install --user pytesseract Pillow` + Tesseract binary ([SKILL.md → OCR setup](SKILL.md#on-screen-text-search-with---ocr)) |
| Hosted Whisper (Groq/OpenAI/Deepgram) | API key — env var or `~/.watch-video/credentials.json` |
| LLM highlights (`--highlights-prompt`) | Anthropic/OpenAI/Groq/DeepSeek/Gemini API key |
| Jira auto-fetch + opt-in posting | Atlassian token at `~/.atlassian-token/credentials.json` |

---

## Pipeline

```
            input
              │
              ▼
            fetch          (URL → yt-dlp · path → as-is · Jira key → REST API)
              │
              ▼
            probe          (ffprobe metadata + audio volume detection)
             / \
            /   \
        frames   transcribe   (captions · local Whisper · Groq · OpenAI · Deepgram+diarization)
            \   /
             \ /
            dedup           (perceptual hash + transcript-aware protection)
              │
              ▼
             ocr            (Tesseract on kept frames; optional)
              │
              ▼
           report           (report.md / .html / .docx)
              │
              ╎  (opt-in, never default)
              ▼
         Jira comment
```

Every step is independently cached — re-run with a tweaked flag and only the affected tail of the pipeline executes (~0.25 s for a no-op re-run vs ~30 s cold).

---

## What it costs

> ⚠️ The numbers below have caused confusion. The first table is what **this skill costs**. The second is what **alternatives charge** for the same job. They are not the same thing.

### What this skill costs (per ~40-min video)

| Setup | Cost | Time |
|---|---|---|
| **Pipeline only** (no agent reads the artifacts) | **$0** | ~15 s |
| Pipeline + Claude Haiku 4.5 reads the artifacts | **~$0.04** | ~4-5 min |
| Pipeline + Claude Sonnet 4.6 reads the artifacts | ~$0.16 | ~4-5 min |

The pipeline runs locally — no API calls for the download, frame extraction, transcription (with `--whisper local`, the default), dedup, OCR, or report generation. Video data never leaves your computer.

The only token cost is when an *agent* reads the generated artifacts. Hosted transcription (Groq/OpenAI/Deepgram) is optional and costs a fraction of a cent per minute.

### What alternatives charge (for context)

Same 40-min video, same output goal (transcript + visual context + structured analysis):

| Alternative tool | Cost | Where the video goes |
|---|---|---|
| Gemini 3 Flash native video upload | ~$0.30 | Google |
| Gemini 3 Pro native video upload | ~$0.80 | Google |
| OpenAI Whisper + GPT-4o vision DIY | ~$4.50 | OpenAI |
| Microsoft Video Indexer (advanced) | ~$8 | Microsoft |
| Anthropic Claude Haiku raw video upload | ~$17 | Anthropic |
| Anthropic Claude Sonnet raw video upload | ~$66 | Anthropic |
| Anthropic Claude Opus raw video upload | ~$331 | Anthropic |

The Anthropic raw-upload column is the cleanest apples-to-apples on output quality (same Claude model reads the same content) — and the 30 fps frame tokenization makes that path uneconomic by ~400-8,000×. By preprocessing locally and feeding only the transcript + deduped frames into the agent, this skill replaces hundreds of dollars of frame tokens with a $0 pipeline + a few cents of structured text.

Full per-tier breakdown + replication commands: [docs/cost-study-atlassian-video.md](docs/cost-study-atlassian-video.md).

---

## Features

- **Multi-source input** — local file path, public URL (`yt-dlp` supports 1500+ sites), Jira issue key (`PROJ-1234`), or `auto` (newest video in `~/Downloads/`)
- **Five transcription modes** — `captions` (free from YouTube VTTs), `local` faster-whisper (offline, default), `groq` Whisper-large-v3, `openai` Whisper-1, and `deepgram` Nova-3 with speaker diarization (`**S0**` / `**S1**` paragraph labels for podcasts and multi-speaker recordings)
- **Smart frame dedup** — pHash + temporal protection + transcript-aware keep rules. ~50% token reduction on screen recordings *without* losing the moment the user typed the wrong value
- **OCR you can grep** — Tesseract on kept frames; `grep -i "unload" ocr.txt` answers "when did the user enter 90?" in milliseconds
- **Per-step cache** — re-run with a tweaked flag and only the affected tail of the pipeline executes
- **Paste-ready evidence bundle** — `report.md` interleaves transcript paragraphs with frame thumbnails; drop into Jira / PR / design-review doc as-is
- **Bulk mode** — process a sprint's worth of bug tickets in one command
- **Opt-in Jira posting** — explicit `--post-to-jira` + confirmation prompt; never default, never silent. See [safety model](#safety-model)
- **LLM highlights** — six providers (Anthropic / OpenAI / Groq / DeepSeek / Gemini / generic openai-compat) for picking the moments that match your prompt

---

## End-to-end walkthroughs

Two real runs with all artifacts and timings captured verbatim — separate docs so they don't bloat this page:

- 📊 **[Walkthrough — Powell's FOMC statement](docs/walkthrough-fomc.md)** — 5:30 Federal Reserve press conference distilled to 5 quantitative highlights. **/usr/bin/bash** (local Whisper), **65s** end-to-end. Demonstrates: continuous-narration source, zero-cost transcription, structured macro analysis.
- 🆕 **[Walkthrough — Claude Code release-notes](docs/walkthrough-claude-code-release.md)** — 54s product release video reduced to 5 actionable workflow changes. **$0**, **29s** end-to-end, **44%** dedup reduction. Demonstrates: fast-cut B-roll, screen-recording context, agent-readable timestamps.

---

## Use cases

- **Bug triage from Jira screen recordings** — `python scripts/watch_video.py PROJ-1234 --dedup --ocr` gives you the transcript + key frames + on-screen text in under a minute. Add `--post-to-jira` to attach the analysis back to the ticket
- **Sprint retro on bug videos** — `python scripts/watch_batch.py --jira-jql "project = PROJ AND labels = video-bug AND created >= -7d"` processes a week's videos at once
- **Researching public content** — point at any YouTube/Loom URL; captions-first means free + sub-5-second on most YouTube videos
- **Podcast / multi-speaker transcription** — `--whisper deepgram` gives anonymous speaker labels (`S0`, `S1`) and a `speakers.json` summary you can later relabel with real names
- **Compliance / privacy-first** — default `--whisper local` runs entirely on your machine; nothing is uploaded
- **CI integration** — the CLI is pipeline-friendly: shell out from a GitHub Actions / GitLab CI / Jenkins step on test-failure videos, parse `meta.json` for the result. Per-step caching means re-runs after a flag tweak are near-instant.

---

## Examples

```bash
# Most common: full-auto Jira workflow
python scripts/watch_video.py PROJ-1234 --dedup --ocr

# YouTube clip with bumped resolution for tiny on-screen text
python scripts/watch_video.py "https://youtu.be/abc" --resolution 1280

# Scope to a 10-second window of a long video
python scripts/watch_video.py PROJ-1234 --start 0:30 --end 0:40

# Fast cold-start with hosted Whisper
python scripts/watch_video.py PROJ-1234 --whisper groq

# Multi-speaker podcast with diarization (transcript tagged S0/S1/...)
python scripts/watch_video.py "https://youtu.be/joe-vs-naval" --whisper deepgram

# Relabel anonymous speakers with real names (v2.3.1+)
# Read speakers.json first to see who said what; then:
python scripts/relabel_speakers.py /tmp/watch-joe-vs-naval \
  --names "S0=Joe Rogan,S1=Naval Ravikant"
# transcript.md and report.md/.html/.docx are atomically rewritten in place.

# LLM-driven highlight selection (default model is Claude Haiku 4.5)
python scripts/watch_video.py PROJ-1234 \
  --highlights-prompt "highlight only bug-related parts"

# Post analysis back to the ticket (opt-in, confirmation prompt)
python scripts/watch_video.py PROJ-1234 --dedup --ocr --post-to-jira

# Bulk: process a sprint's worth of bug tickets
python scripts/watch_batch.py \
  --jira-jql "project = PROJ AND labels = video-bug AND created >= -7d" \
  --dedup --ocr
```

Every flag is documented in [docs/configuration.md](docs/configuration.md).

---

## Safety model

The skill ships with safety guardrails for the only mutating action it can take — posting to Jira:

- **`--post-to-jira` is opt-in.** Default behavior never writes anything anywhere.
- **Interactive confirmation prompt.** When `--post-to-jira` is set, the CLI prints the planned comment and asks for `y/N` before sending. The default is `N`.
- **`--post-to-jira-dry-run`** previews the comment without sending — for sanity-checking the formatting in CI or before a real run.
- **No unsolicited Jira writes from any context.** Skill / plugin / MCP / direct API: all paths require explicit per-invocation consent. The MCP `post_to_jira` tool defaults to `confirm=False` (dry-run); MCP hosts MUST surface the planned action to the user before passing `confirm=True`.

---

## Architecture

CLI scripts are the canonical implementation. Everything else (plugin, skill, MCP server) is a thin adapter that calls these scripts under the hood.

```
scripts/                  ← canonical implementation
├── watch_video.py        ← top-level orchestrator
├── fetch.py              ← URL / file / Jira input
├── probe.py              ← ffprobe metadata + audio analysis
├── frames.py             ← ffmpeg keyframe extraction
├── transcribe.py         ← captions / local / Groq / OpenAI / Deepgram
├── dedup.py              ← pHash + transcript-aware protection
├── ocr.py                ← Tesseract on kept frames
├── report.py             ← report.md / .html / .docx generation
├── highlights.py         ← LLM-driven moment picker (6 providers)
└── post_to_jira.py       ← opt-in, confirmation-gated Jira posting

mcp-server/server.py      ← thin async wrapper over the CLI scripts
```

Each step writes to disk atomically (staged then `os.replace`'d). Per-step fingerprint cache; re-run with a tweaked flag and only the affected tail executes. Workdir is a contract — every artifact has a known filename and shape ([SKILL.md → file layout](SKILL.md)).

---

## Versioning + Roadmap

Latest release: **v2.3.1** ([changelog](https://github.com/MarcinSufa/claude-watch-video/releases)) — adds `scripts/relabel_speakers.py` for swapping `S0` / `S1` for real names after a Deepgram run.

What's queued:
- **v2.3.2** — WhisperX local diarization (free + offline alternative to Deepgram; same `speakers.json` schema → same relabel flow)
- **v2.4.0+** — OCR cross-correlation for screen-recording name overlays (auto-label speakers from Zoom/Teams/Meet name tags, eliminating the manual relabel step entirely on those sources)

Full roadmap: [ROADMAP.md](ROADMAP.md).

---

## License

MIT — see [LICENSE](LICENSE). Free for any use, commercial OK, just keep the copyright notice.

## Acknowledgements

Built on [ffmpeg](https://ffmpeg.org/), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Tesseract](https://github.com/tesseract-ocr/tesseract), [Pillow](https://pillow.readthedocs.io/), [imagehash](https://github.com/JohannesBuchner/imagehash), [python-docx](https://github.com/python-openxml/python-docx), and the [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python). LLM highlight picking optionally uses [OpenAI](https://github.com/openai/openai-python), [Groq](https://groq.com/), [DeepSeek](https://api.deepseek.com), or [Google Gemini](https://ai.google.dev/) via their OpenAI-compatibility endpoints. Speaker diarization via [Deepgram Nova-3](https://deepgram.com).
