# claude-watch-video

> **Give Claude eyes and ears for any video** — local files, public URLs (YouTube, Loom, Vimeo, TikTok…), or Jira attachments. Fully automated, from input to a paste-ready Markdown evidence bundle.

[![smoketest](https://github.com/MarcinSufa/claude-watch-video/actions/workflows/smoketest.yml/badge.svg)](https://github.com/MarcinSufa/claude-watch-video/actions/workflows/smoketest.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macOS%20%7C%20linux-lightgrey.svg)](#prerequisites)
[![Skill](https://img.shields.io/badge/Claude%20Code-skill-purple.svg)](https://docs.claude.com/en/docs/claude-code)
[![Plugin](https://img.shields.io/badge/Claude%20Code-plugin-purple.svg)](https://docs.claude.com/en/docs/claude-code/plugins)
[![MCP](https://img.shields.io/badge/MCP-server-orange.svg)](mcp-server/README.md)

This skill turns "watch CON-1234 and tell me what broke" into a single command. It downloads the video, extracts keyframes with ffmpeg, transcribes audio with local or hosted Whisper, deduplicates near-identical frames while preserving narrated moments, optionally OCRs on-screen text, and writes a paste-ready `report.md` — all in under a minute.

### What it produces

A 5:30 FOMC press conference distilled to one moment, picked by Claude against the prompt *"summarize the rate decision, inflation outlook, and rate-path forecast"*:

> **00:22 — the rate decision**
>
> <img src="docs/images/fomc/00-22-rate-decision.jpg" width="420" alt="Powell at 00:22 announcing the rate hold">
>
> The headline rate decision: *"Today, the FOMC decided to leave our policy rate unchanged."* Committee judges current policy stance appropriate for the dual mandate.

That's one card from a real `highlights.md` rendered inline. The skill also produces a full `report.md` evidence bundle (transcript + all timestamped frames), a self-contained `report.html`, a Word `report.docx`, and a machine-readable `highlights.json` — see the [Powell FOMC walkthrough](#end-to-end-walkthrough-1-powells-fomc-statement-macro-analysis) and the [Claude Code release-notes walkthrough](#end-to-end-walkthrough-2-claude-code-release-notes-video-personal-workflow) below for the full artifacts and timings from real runs.

---

## Table of contents

- [The 30-second pitch](#the-30-second-pitch)
- [Pipeline](#pipeline)
- [Quick start](#quick-start)
- [Features](#features)
- [How it compares](#how-it-compares)
- [What it costs](#what-it-costs)
- [Walkthrough #1: Powell FOMC statement](#end-to-end-walkthrough-1-powells-fomc-statement-macro-analysis)
- [Walkthrough #2: Claude Code release-notes](#end-to-end-walkthrough-2-claude-code-release-notes-video-personal-workflow)
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
- **$0 pipeline.** Local processing, no API costs. Optional hosted Whisper or LLM highlights cost cents per video. Video stays on your machine. See [What it costs](#what-it-costs) for the full breakdown — including the **~300× cheaper than raw Claude video upload** comparison.

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

### As an MCP server (Claude Desktop, Codex CLI, Cursor, Continue.dev, Cline, Windsurf, Zed, VS Code Copilot Chat)

```bash
git clone https://github.com/MarcinSufa/claude-watch-video
cd claude-watch-video/mcp-server
pip install -e .  # or pip install -e ".[full]" for all underlying CLI deps
```

Then register `claude-watch-video-mcp` as an MCP server in your host. For Claude Desktop:

```json
{
  "mcpServers": {
    "watch-video": {
      "command": "claude-watch-video-mcp"
    }
  }
}
```

For Codex CLI: `codex mcp add watch-video --command claude-watch-video-mcp`.

Full host-by-host setup + tool reference + safety contract for `post_to_jira`: [mcp-server/README.md](mcp-server/README.md).

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

## What it costs

**The pipeline itself is $0.** Download, frame extraction, transcription (captions or local Whisper), smart dedup, OCR, report generation — every step runs on your machine with no API calls. Video data never leaves your computer.

The only token cost is when an agent (Claude Code, Claude Desktop, Cursor, etc.) *reads* the generated artifacts to answer your question. That scales with the model:

| Per ~1-minute video, agent reads transcript + ~4 frames | Cost |
|---|---|
| Claude Haiku 4.5 | **~$0.015** |
| Claude Sonnet 4.6 | ~$0.05 |
| Claude Opus 4.7 | ~$0.25 |

100 videos/month on Haiku: **~$1.50/month** total. The pipeline contributes nothing.

### How that compares

Three tiers depending on what equivalent work you're actually buying. All costs normalized to **one minute of video**.

**Tier 1 -- transcription only (audio → text, no frames, no summary)**

| Service | $/min | Where it goes |
|---|---|---|
| **watch-video (captions-first or local Whisper)** | **$0.000** | **Stays local** |
| Groq Whisper API | ~$0.002 | Groq |
| Deepgram Nova-3 | ~$0.004 | Deepgram |
| OpenAI Whisper-1 API | ~$0.006 | OpenAI |
| AssemblyAI Universal-2 | ~$0.006 | AssemblyAI |
| AWS Transcribe | ~$0.024 | AWS |
| Google Cloud Speech-to-Text | ~$0.024 | Google |
| Azure Speech-to-Text | ~$0.017 | Microsoft |

**Tier 2 -- transcript + visual frames (no language synthesis)**

| Service | $/min | Notes |
|---|---|---|
| **watch-video pipeline (frames + transcript + dedup + OCR)** | **$0.000** | All local; produces report.md / .html / .docx |
| Twelve Labs Pegasus 1.2 (embedding) | ~$0.004 | Vectors only, no text output |
| Microsoft Video Indexer (basic) | ~$0.050 | Includes transcription + face + scene + basic OCR |
| AWS Rekognition Video (labels only) | ~$0.010 | Object/label detection, no transcript |

**Tier 3 -- full pipeline (transcript + frames + structured LLM summary)**

This is what the [Atlassian case study](docs/cost-study-atlassian-video.md) actually delivers.

| Service | $/min | What you get | Where the video goes |
|---|---|---|---|
| **watch-video + Claude Haiku 4.5** | **~$0.001** | Local pipeline + structured analysis | **Local pipeline; transcript only to Anthropic** |
| **watch-video + Claude Sonnet 4.6** | **~$0.004** | Same, better narrative quality | **Local pipeline; transcript only to Anthropic** |
| Gemini 3 Flash native video upload | ~$0.008-0.013 | One API call, multimodal answer | Google |
| Gemini 3 Pro native video upload | ~$0.013-0.025 | Same, larger model | Google |
| Anthropic Claude Haiku raw video upload | ~$0.42 | 30 fps frame tokenization | Anthropic |
| Anthropic Claude Sonnet raw video upload | ~$1.65 | Same, $3/M input | Anthropic |
| **Anthropic Claude Opus raw video upload** | **~$8.30** | 30 fps × $15/M Opus input | Anthropic |
| OpenAI Whisper + GPT-4o vision DIY | ~$0.10 | Multi-API: Whisper + per-frame GPT-4o | OpenAI |
| Microsoft Video Indexer (advanced) | ~$0.20 | Full enterprise analysis | Microsoft |
| Symbl.ai conversation intelligence | ~$0.10 | Conversation-focused | Symbl |

### The headline numbers

Compared per-tier to the cheapest credible alternative:

| Compare watch-video + Haiku ($0.001/min) to... | They charge | Cheaper by |
|---|---|---|
| Cheapest dedicated transcription API (Groq Whisper) | $0.002/min | ~2× — *and we deliver transcript + frames + analysis, not just transcript* |
| Cheapest multimodal video LLM (Gemini 3 Flash) | $0.008/min | **~7×** |
| OpenAI Whisper + GPT-4o vision DIY pipeline | $0.10/min | **~95×** |
| Microsoft Video Indexer (advanced) | $0.20/min | **~190×** |
| Anthropic Claude Sonnet raw video upload | $1.65/min | **~1,570×** |
| Anthropic Claude Opus raw video upload | $8.30/min | **~7,900×** |

Even comparing **Sonnet-tier output quality** (watch-video + Sonnet at $0.004/min) to peers, you save 2-410× depending on the alternative.

The Anthropic-raw-upload comparison is the cleanest apples-to-apples on output quality (same Claude model reads the same content). The 30 fps frame tokenization makes that path uneconomic by 400-8,000×.

### Real-world case study: 40-minute Atlassian engineering talk

A measured end-to-end run against a 40-minute technical talk about Atlassian's edge infrastructure ("I was laid off by Atlassian", 1,032 transcript segments, 79 frames after dedup). The pipeline + agent produced a [structured architecture report with 5 Mermaid diagrams and 4 Chart.js charts](docs/examples/atlassian-architecture-report.html).

**Direct cost AND time comparison vs alternatives** — same 40-minute video, same output goal (transcript + visual context + structured analysis):

| Service / pipeline | Cost | Time | Output |
|---|---|---|---|
| **watch-video + Haiku read (this run)** | **~$0.04** | **~4-5 min** | Pipeline + full architecture report |
| **watch-video + Sonnet read** | ~$0.16 | ~4-5 min | Same, better narrative |
| **watch-video pipeline only (no agent)** | **$0** | **15.66 s** | Transcript + frames + report.md/.html/.docx |
| Gemini 3 Flash native video upload | ~$0.30 | ~30 s - 2 min | One-shot multimodal answer |
| Gemini 3 Pro native video upload | ~$0.80 | ~1-3 min | Same, larger model |
| OpenAI Whisper + GPT-4o vision (DIY) | ~$4.50 | ~5-15 min | Multi-API pipeline |
| Microsoft Video Indexer (advanced) | ~$8 | ~15-30 min | Enterprise analysis |
| Anthropic Claude Haiku raw video upload | ~$17 | ~2-5 min | 30 fps frame tokenization |
| Anthropic Claude Sonnet raw video upload | ~$66 | ~2-5 min | Same, $3/M input |
| **Anthropic Claude Opus raw video upload** | **~$331** | ~2-5 min | 22M input tokens × $15/M |

**Headline ratios (Haiku vs the field):**

| Compare to | Cost ratio | Time ratio |
|---|---|---|
| Gemini 3 Flash native | **~7× cheaper** | comparable |
| OpenAI Whisper + GPT-4o DIY | **~112× cheaper, ~2-3× faster** | |
| Microsoft Video Indexer | **~200× cheaper, ~3-6× faster** | |
| Anthropic Claude Opus raw | **~8,275× cheaper** | comparable |

The 15.66-second pipeline-only time is the surprising number — it works because YouTube's CDN already serves the captions for free, so we skip Whisper entirely. Any Whisper-based service has to actually transcribe 40 minutes of audio. On YouTube content, **watch-video is structurally faster** than every dedicated transcription API, not just cheaper.

**Artifacts you can open right now:**
- 📊 **[docs/examples/atlassian-architecture-report.html](docs/examples/atlassian-architecture-report.html)** — the rendered report (218 KB, self-contained)
- 📈 **[docs/cost-study-atlassian-video.md](docs/cost-study-atlassian-video.md)** — full methodology, per-step timing, cost math, replication commands, citation format

### Why it's this cheap

- **yt-dlp + ffmpeg + faster-whisper run locally** — zero per-video API cost
- **Captions-first transcription** prefers free YouTube VTTs when available (most YouTube content costs $0 to transcribe)
- **Smart dedup with transcript-aware protection** drops 40-60% of redundant frames on screen recordings without losing narrated moments
- **Strategic frame sampling at read time** — the agent reads ~4-8 frames out of 16-80 kept, never every frame
- Local Whisper is offline + free by default; hosted Whisper opt-in for speed (Groq ~$0.002/min, OpenAI ~$0.006/min)

Per-video cost ends up dominated by *how much you ask the agent to reason*, not by the pipeline.

---

## End-to-end walkthrough #1: Powell's FOMC statement (macro analysis)

A real walkthrough, not a hypothetical. The skill is run against the [Federal Reserve's official FOMC Introductory Statement, March 18, 2026](https://www.youtube.com/watch?v=SVrdJINZGIM) — Powell delivering the rate-decision opening remarks. Every artifact shown below is captured verbatim from the actual run; numbers and quotes are real.

### The command

```bash
python scripts/watch_video.py "https://www.youtube.com/watch?v=SVrdJINZGIM" \
  --workdir c:\tmp\fomc-demo --dedup --verbose
```

Then ran `highlights.py` against the prompt **"summarize the rate decision, inflation outlook, and rate-path forecast"** to pick the 5 most relevant moments.

### What landed on disk

```
c:\tmp\fomc-demo\
├── FOMC_Introductory_Statement_March_18_2026.mp4   13.8 MB  (329.7s, 640×360)
├── audio.wav                                       10.5 MB  (mono, 16 kHz, mean -23.4 dB)
├── frames/                                         60 JPEGs (3.1 MB total)
├── transcript.txt                                  5.4 KB   (56 Whisper segments)
├── transcript.md                                   5.3 KB   (29 prose paragraphs)
├── report.md                                       7.0 KB   (29 timeline blocks)
├── report.html                                     2.1 MB   (base64-embedded, browser-ready)
├── report.docx                                     1.6 MB   (Word, editable)
├── highlights.json                                 1.6 KB   (LLM-format picks)
├── highlights.md                                   2.5 KB   (paste-ready)
├── highlights.html                                 374 KB   (browser-ready)
└── meta.json                                       5.9 KB   (versioned schema)
```

### Smart dedup on a talking-head video

```
"dedup": { "before": 60, "after": 60, "dropped": 0,
           "kept_by_temporal_protection": 47,
           "kept_by_transcript_protection": 15 }
```

Zero frames dropped — every uniform-interval frame fell within either the 5-second min-interval window or the ±1.5s transcript-paragraph protection window. This is the *correct* behavior for a continuous-narration source: nothing redundant to remove. (For a screen recording with long static stretches, dedup typically removes 40–60%; see the second walkthrough below.)

### Sample of `transcript.md`

Real Whisper output, unedited:

```
(_00:22_) elevated. Today, the FOMC decided to leave our policy rate unchanged.
We see the current stance of monetary policy as appropriate to promote progress
toward our maximum employment

(_02:21_) and other data indicate that total PCE prices rose 2.8 percent over
the 12 months ending in February, and that excluding the volatile food and
energy categories, core PCE prices

(_03:23_) At today's meeting, the Committee decided to maintain the target
range for the federal funds rate at 3.5 to 3.75 percent. From last September
through December, we lowered

(_04:28_) each participant judges to be the most likely scenario for the
economy. The median participant projects that the appropriate level of the
federal funds rate will be 3.4 percent at
```

### `highlights.md` for the prompt *"summarize the rate decision, inflation outlook, and rate-path forecast"*

This is the actual rendered file from the run:

```markdown
# Highlights — FOMC Introductory Statement, March 18, 2026

> Generated for prompt: **"summarize the rate decision, inflation outlook, and rate-path forecast"**
> Model: `claude-opus-4-7` · 5 moments picked of max 5

## 00:22
![00:22](frames/t_005.jpg)
**Why this matters:** The headline rate decision: "Today, the FOMC decided to
leave our policy rate unchanged." Committee judges current policy stance
appropriate for the dual mandate.
> elevated. Today, the FOMC decided to leave our policy rate unchanged. We see
> the current stance of monetary policy as appropriate to promote progress
> toward our maximum employment

## 01:07
![01:07](frames/t_013.jpg)
**Why this matters:** Growth outlook: median SEP participant projects real GDP
+2.4% in 2026 and +2.3% in 2027 — both somewhat stronger than the December
projection. Housing remains the weak link.
> In contrast, activity in the housing sector has remained weak. In our summary
> of economic projections, the median participant projects that real GDP will
> rise 2.4 percent this

## 02:21
![02:21](frames/t_027.jpg)
**Why this matters:** Inflation snapshot: total PCE +2.8% YoY (Feb), core PCE
+3.0%. Goods-sector inflation boosted by tariffs; near-term expectations
elevated by oil supply disruptions in the Middle East.
> and other data indicate that total PCE prices rose 2.8 percent over the 12
> months ending in February, and that excluding the volatile food and energy
> categories, core PCE prices

## 03:23
![03:23](frames/t_038.jpg)
**Why this matters:** Target range confirmed: federal funds rate held at
3.50%–3.75%. Powell notes 3.4 percentage points of cuts from last September
through December bring policy within plausible estimates of neutral.
> At today's meeting, the Committee decided to maintain the target range for
> the federal funds rate at 3.5 to 3.75 percent. From last September through
> December, we lowered

## 04:28
![04:28](frames/t_050.jpg)
**Why this matters:** Rate-path forecast (the dot plot): median SEP
participant sees the fed funds rate at 3.4% end-of-2026 and 3.1% end-of-2027,
unchanged from December's projection. Powell stresses meeting-by-meeting
decisions, not a preset course.
> each participant judges to be the most likely scenario for the economy. The
> median participant projects that the appropriate level of the federal funds
> rate will be 3.4 percent at

---
_Generated by `/watch-video` skill `highlights` step._
```

A 5:30 monetary-policy address distilled to the 5 quantitative bullets a fixed-income analyst, IR lead, or financial-services LLM agent actually needs. Frame + verbatim quote + analysis context. Drop into a research note, a Slack thread, a desk-readout email, or feed straight into a downstream model. (See the rendered preview of the 00:22 rate-decision moment at the top of this README under [What it produces](#what-it-produces). The other moments — [02:21](docs/images/fomc/02-21-inflation-snapshot.jpg), [03:23](docs/images/fomc/03-23-fed-funds-target.jpg), [04:28](docs/images/fomc/04-28-dot-plot.jpg) — render the same way.)

### Total wall-clock time

Real numbers, from this exact run (`elapsed_seconds: 65.05` in `meta.json`):

| Phase | Time |
|---|---|
| Download (13.8 MB / yt-dlp) | ~5 s |
| Probe (ffprobe + volumedetect) | <1 s |
| Frame extraction (60 uniform frames) | ~2 s |
| Audio extract (mono 16 kHz) | ~1 s |
| Transcribe (faster-whisper `small.en`, **local**) | ~55 s |
| Smart dedup | <1 s |
| Report (md + html + docx) | <1 s |
| Highlights (rendered by `highlights.py`) | <1 s |
| **Total** | **65 s** |

Transcribe is the bottleneck on a 5-minute speech with local Whisper. Swap `--whisper groq` and the same input drops under ~10s for the transcribe step on Whisper-large-v3 hosted.

---

## End-to-end walkthrough #2: Claude Code release-notes video (personal workflow)

Different shape of input: a 54-second product-release video that you'd otherwise need to actually watch to know whether anything in it changes how you work. The skill is run against ["Claude Code v2.1.142 — Full Control Over Background Agents"](https://www.youtube.com/watch?v=O664gH_szoY) with the prompt **"show me how this will improve how i work with claude"**. Same pipeline, different value: research/learning instead of macro analysis.

### The command

```bash
python scripts/watch_video.py "https://www.youtube.com/watch?v=O664gH_szoY" \
  --workdir c:\tmp\claude-features-demo --dedup --verbose
```

Then `highlights.py` against the prompt above.

### What landed on disk

```
c:\tmp\claude-features-demo\
├── Claude_Code_v2.1.142_Full_Control_Over_Background_Agents.mp4   1.0 MB
├── audio.wav                                                       1.7 MB
├── frames/                                                         14 JPEGs
├── transcript.txt                                                  943 B
├── transcript.md                                                   884 B
├── report.md                                                       1.8 KB
├── report.html                                                     188 KB
├── report.docx                                                     152 KB
├── highlights.json                                                 2.2 KB
├── highlights.md                                                   2.9 KB
├── highlights.html                                                 171 KB
└── meta.json                                                       4.5 KB
```

### Smart dedup on a fast-cut release video

```
"dedup": { "before": 25, "after": 14, "dropped": 11,
           "kept_by_temporal_protection": 4,
           "kept_by_transcript_protection": 5 }
```

44% reduction (25 → 14 frames) without losing any narrated moment. The 5 transcript paragraphs each had a ±1.5s protected window; another 4 frames survived because of the 5-second min-interval rule; the remaining 11 redundant frames were dropped. This is the dedup story for fast-cut B-roll-heavy content.

### Full `transcript.md` (54 seconds, 6 paragraphs)

```
(_00:00_) Claude code 2.1.100 and 42. Background agents just got a full
configuration API, right from the command line.

(_00:08_) Eight new flags for Claude agents. Pass-model, dash effort, dash
permission mode, dash mckpconfig, dash settings, dash add dir,

(_00:19_) dash plugin dir, or dash dangerously skip permissions directly at
dispatch time. Every background session is now fully configurable before it
ever starts.

(_00:28_) Fast mode now defaults to opus 4.7. Set cloud underscore code
underscore opus underscore four underscore six underscore fast

(_00:36_) underscore mode underscore override. Equal sign one to pin the
previous version. And on mac, background sessions no longer vanish after sleep.

(_00:46_) The demon now detects clock jumps instead of treating them as idle
time. Full release notes at the link below. Subscribe so you never miss a drop.
```

Whisper picks up the spoken flag names imperfectly — "dash mckpconfig" is `--mcp-config`, "the demon" is "the daemon". Highlights step recovers the canonical names by treating the transcript as a hint to be reasoned over, not a literal source.

### `highlights.md` for the prompt *"show me how this will improve how i work with claude"*

This is the actual rendered file from the run, no edits:

```markdown
# Highlights — Claude Code v2.1.142 — Full Control Over Background Agents

> Generated for prompt: **"show me how this will improve how i work with claude"**
> Model: `claude-opus-4-7` · 5 moments picked of max 5

## 00:08
![00:08](frames/t_004.jpg)
**Why this matters:** Eight new dispatch-time flags (`--model`, `--effort`,
`--permission-mode`, `--mcp-config`, `--settings`, `--add-dir`, `--plugin-dir`,
`--dangerously-skip-permissions`) let you tune each background agent for the
specific task instead of relying on one shared profile. Workflow win: spin up
a high-effort agent for a hard refactor in one terminal and a fast cheap agent
for routine triage in another, without ever editing your global settings.
> Eight new flags for Claude agents. Pass-model, dash effort, dash permission
> mode, dash mckpconfig, dash settings, dash add dir,

## 00:19
![00:19](frames/t_007.jpg)
**Why this matters:** "Every background session is now fully configurable
before it ever starts" — this is the headline. The old flow required editing
settings, restarting, and hoping the agent picked them up. Now config is part
of the dispatch command, so scripts and aliases can encode entire workflows
(e.g. an 'overnight refactor' alias that pins model + permission mode + extra
dirs in one shot).
> dash plugin dir, or dash dangerously skip permissions directly at dispatch
> time. Every background session is now fully configurable before it ever
> starts.

## 00:28
![00:28](frames/t_009.jpg)
**Why this matters:** Fast mode now defaults to Opus 4.7 — you get the newest,
smartest model under the fast-output path without having to remember a flag.
Pin the previous version via the env override if you need deterministic
behavior for benchmarks.
> Fast mode now defaults to opus 4.7. Set cloud underscore code underscore
> opus underscore four underscore six underscore fast

## 00:36
![00:36](frames/t_011.jpg)
**Why this matters:** Mac fix: background sessions no longer vanish after the
laptop sleeps. If you dispatch a long-running agent (codegen pass, test suite,
doc build) and close the lid, the session is still there when you wake the
machine — no lost work, no restart.
> underscore mode underscore override. Equal sign one to pin the previous
> version. And on mac, background sessions no longer vanish after sleep.

## 00:46
![00:46](frames/t_013.jpg)
**Why this matters:** The daemon now detects clock jumps instead of treating
them as idle time. Practical impact: docking/undocking, VPN switches, and
timezone changes during travel no longer kill in-flight agents — your sessions
survive the same environment churn that used to silently break them.
> The demon now detects clock jumps instead of treating them as idle time.
> Full release notes at the link below. Subscribe so you never miss a drop.

---
_Generated by `/watch-video` skill `highlights` step._
```

A 54-second release video distilled to the 5 things a power user would actually change about how they work. Each pick is anchored to a frame and the verbatim transcript paragraph, so the user can verify the analysis against the source in two clicks. Drop into a team-wide "what's new" message, paste into your weekly notes, or have Claude reason over `highlights.json` to update your shell aliases. (Frame previews: [00:08 — eight new dispatch flags](docs/images/claude-features/00-08-eight-flags.jpg), [00:28 — fast-mode → Opus 4.7](docs/images/claude-features/00-28-fast-mode.jpg), [00:46 — clock-jump fix](docs/images/claude-features/00-46-clock-jumps.jpg).)

### Total wall-clock time

Real numbers, from this exact run (`elapsed_seconds: 29.16` in `meta.json`):

| Phase | Time |
|---|---|
| Download (1.0 MB / yt-dlp) | ~2 s |
| Probe + audio extract + frames (25 uniform) | ~2 s |
| Transcribe (faster-whisper `small.en`, **local**) | ~24 s |
| Smart dedup (25 → 14 frames) | <1 s |
| Report (md + html + docx) | <1 s |
| Highlights (rendered by `highlights.py`) | <1 s |
| **Total** | **29 s** |

Half a minute to know whether a release video changes anything about how you work — instead of either (a) watching 54 seconds + 5 minutes of re-watching to find the flag names, or (b) skipping it and being 6 weeks behind.

### Same pipeline, also great for Jira bug-repro screen-recordings

The same `watch_video.py` accepts a Jira issue key directly:

```bash
python scripts/watch_video.py PROJ-2145 --dedup --ocr \
  --highlights-prompt "what is the actual bug and at what moment does it occur" \
  --post-to-jira
```

For a screen-recording attached to a Jira ticket, `--ocr` extracts on-screen text (button labels, field contents, error toasts) into `ocr.txt` which you can `grep` — so "when did the user enter 90?" becomes a sub-second lookup instead of a re-watch. Smart dedup on screen recordings typically removes 40–60% of frames (long static stretches collapse to one frame, narrated moments are preserved). Add `--post-to-jira` to write the analysis back to the same ticket with the explicit-confirmation safety stack ([safety model](#safety-model)).

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

### Researching public content (free, sub-5-second)

```bash
python scripts/watch_video.py https://www.youtube.com/watch?v=...
```

Default `--whisper auto` picks `captions` when yt-dlp pulled a VTT (most YouTube content), falling back to local Whisper otherwise. End-to-end on a 1-minute video: ~4 seconds wall-clock. Zero API cost. Transcript + frame timeline + report.md/.html/.docx.

### Force Whisper transcription instead of captions

```bash
python scripts/watch_video.py https://youtu.be/XYZ --whisper local
# or for cleaner punctuation on hosted Whisper:
python scripts/watch_video.py https://youtu.be/XYZ --whisper groq
```

Use this when caption quality is poor (e.g. heavily-accented speech, auto-captions only), or when the source has no captions at all.

### Cost-conscious LLM highlights on OpenAI / Groq

```bash
python scripts/watch_video.py https://youtu.be/XYZ \
  --highlights-prompt "summarize the key technical announcements" \
  --highlights-provider openai --highlights-model gpt-4o-mini
```

Pick highlights using OpenAI or Groq instead of Anthropic. Useful when you have credit on one but not the other. Same JSON shape, same paste-ready Markdown / HTML output. Defaults: `claude-haiku-4-5-20251001` (anthropic), `gpt-4o-mini` (openai), `llama-3.1-70b-versatile` (groq).

### Compliance / privacy-first call review

```bash
python scripts/watch_video.py confidential-call.mp4 --whisper local --no-html --no-docx
```

100% local. No video data leaves the machine. Pair with `--no-html` and `--no-docx` if your downstream system needs only the bare transcript.

### CI integration: auto-analyze Playwright/Cypress failure videos

```yaml
# .github/workflows/e2e-bug-triage.yml (sketch)
- name: Triage failed E2E runs
  if: failure()
  run: |
    for vid in test-results/**/video.webm; do
      python scripts/watch_video.py "$vid" --dedup --ocr \
        --highlights-prompt "what failed and at what UI state"
    done
```

Every test failure that uploads a video gets a 30-second auto-report. Frame + transcript + OCR'd UI state, ready to paste into the failure comment.

### Knowledge-base ingestion

```bash
python scripts/watch_batch.py --inputs "$(find ./training-videos -name '*.mp4' | paste -sd, -)"
```

Process a backlog of internal training videos in one command. Each gets searchable transcript.md + ocr.txt — drop into Notion / Confluence / Algolia for full-text search.

### Onboarding video → process checklist

```bash
python scripts/watch_video.py loom-release-walkthrough.mp4 \
  --highlights-prompt "extract the deploy steps as a numbered checklist"
```

Record a Loom of "how we ship X", get a Markdown checklist out. The `highlights.md` lists each step with the frame showing the click + the narrator's instruction quoted.

### Lecture / classroom notes

```bash
python scripts/watch_video.py "https://youtu.be/some-lecture" \
  --highlights-prompt "extract definitions, key formulas, and worked examples"
```

A 45-minute lecture distilled to 5 anchor moments + a clean transcript. Drop the `highlights.html` into your notes app — frames + reason + quote per moment.

### Loom alternative for async dev demos

```bash
python scripts/watch_video.py my-demo.mp4 --highlights-prompt "summarize the demo for someone who won't watch"
# Share the resulting report.html (self-contained, base64 frames) with your team.
```

Record once, share the `report.html` link instead of "I'll send a 6-minute Loom." Recipients see a paste-ready summary + click-through to the original frames.

### Demo / walkthrough analysis (windowed)

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

# Post analysis back to the ticket (opt-in, confirmation prompt)
# Default style is `collapsed`: short ticket comment with click-to-expand timeline
python scripts/watch_video.py PROJ-1234 --dedup --ocr --post-to-jira

# Post a *summary* comment with 5 key moments + report.html attached as download
# (good for long videos where a 60-section comment would be a wall)
python scripts/watch_video.py PROJ-1234 --dedup --ocr --post-to-jira \
  --post-to-jira-style summary --post-to-jira-summary-key-frames 5

# Preview a Jira post without sending anything
python scripts/watch_video.py PROJ-1234 --dedup --ocr --post-to-jira \
  --post-to-jira-dry-run

# LLM-driven highlight selection — pick the moments that match your intent
python scripts/watch_video.py PROJ-1234 --dedup --ocr \
  --highlights-prompt "highlight only bug-related parts" \
  --post-to-jira --post-to-jira-style summary
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
| `--whisper auto\|captions\|local\|groq\|openai` | Transcription source. Default `auto`: use VTT captions if yt-dlp pulled one (free, sub-5-second), else `local` faster-whisper. Force any explicit provider to override. |
| `--model NAME` | Whisper model id |
| `--lang en\|pl\|...\|auto` | Audio language |
| `--no-audio` | Skip transcription |
| `--no-html` | Skip `report.html` (Markdown + DOCX still produced) |
| `--no-docx` | Skip `report.docx` (degrades gracefully if `python-docx` is missing anyway) |
| `--highlights-prompt "..."` | Enable LLM-driven highlight selection (requires an Anthropic / OpenAI / Groq key) |
| `--highlights-provider anthropic\|openai\|groq` | Which LLM to call for highlights. Default `anthropic`. Groq uses Llama via the OpenAI-compatible endpoint. |
| `--highlights-model NAME` | Model id (defaults vary by provider) |
| `--highlights-api-key KEY` | API key for the chosen highlights provider (env vars also work) |
| `--highlights-credentials PATH` | JSON file path for the highlights API key (separate from Atlassian creds) |
| `--no-cache` | Bypass the per-step output cache |
| `--force-step NAME[,...]` | Force a specific step (downstream auto-invalidates) |
| `--post-to-jira` | **Opt-in only**: post `report.md` as a Jira comment. By default the Timeline section is wrapped in an ADF expand panel (click-to-show on the ticket). Pass `--post-to-jira-style summary` for a short comment with key moments + `report.html` attached; `--post-to-jira-style inline` for the legacy v1.5.0 full-inline layout. |
| `--post-to-jira-style {collapsed,inline,summary}` | Jira comment layout. Default `collapsed`. |
| `--post-to-jira-summary-key-frames N` | Number of key moments in `--style summary` (default 3, evenly distributed). |
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
- **Native PDF generation** — current recommended PDF workflow is to open `report.html` in any browser and use **Ctrl+P → Save as PDF** (zero deps, high-quality output). If a fully-automated PDF is wanted, future versions could integrate `wkhtmltopdf` (external Windows installer) or `weasyprint` (heavy system libs). ~1 hr if we commit to one of those.
- **Intelligent highlights** — replace the current "evenly distributed N moments" heuristic for summary mode with an LLM-driven selection. Coming soon as `--highlights-prompt "..."` with user-supplied criteria like *"highlight only bug-related parts"* — feeds the transcript to Claude API and gets back semantic moment picks. ~2 hr.
- **Translation** — auto-translate transcript to a target language during transcribe (`--translate-to pl` etc.), so a Spanish-language video can land in a Polish-language report. Underlying Whisper supports English translation today; arbitrary-target translation would need a follow-up step (DeepL, Google, or LLM). ~2 hr.
- **Annotated frames** — timestamp watermark overlaid on each JPG for standalone sharing. ~1 hr.
- **Speaker diarization** — `Speaker A: ... / Speaker B: ...` labeling for meeting / interview audio (would require `whisperx` or `pyannote.audio`). ~2 hr.
- **Confluence video support** — fetch from embedded video macros on Confluence pages. ~2 hr.

### Recently shipped

- **Intelligent highlights** ✓ shipped in v1.10.0 — `--highlights-prompt "..."` runs `highlights.py` which feeds the transcript to Claude (Haiku 4.5 by default, configurable) and gets back the N most relevant moments based on the user's prompt. Use cases: *"highlight only bug-related parts"*, *"find every mention of pricing"*, *"show me what the host says about caching"*. Replaces the evenly-distributed picker in summary mode. Cost: ~$0.001-0.005 per video transcript on Haiku.
- **DOCX report** ✓ shipped in v1.9.0 — `report.docx` generated alongside `report.md` and `report.html`. Native Office Open XML with embedded images. ~3-4 MB for an 80-frame video. Opens in Word, LibreOffice, Google Docs (after upload), and any DOCX-capable viewer. Pass `--no-docx` to skip.
- **PDF workflow (manual)** ✓ documented — open `report.html` in any browser, press `Ctrl+P`, choose "Save as PDF". Zero deps, browser-quality output. Native PDF generation in the skill itself is on the backlog.
- **ADF expand panels for long timelines** ✓ shipped in v1.8.0 — collapsible "click to expand" is now the default style for Jira posts.
- **`--style summary` Jira post + `report.html` as ticket attachment** ✓ shipped in v1.8.0 — short comment with N key moments, full HTML downloadable.

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
