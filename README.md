# claude-watch-video

> **Give Claude eyes and ears for any video** — local files, public URLs (YouTube, Loom, Vimeo, TikTok…), or Jira attachments. Pipeline runs locally on your machine; produces a paste-ready Markdown evidence bundle.

[![smoketest](https://github.com/MarcinSufa/claude-watch-video/actions/workflows/smoketest.yml/badge.svg)](https://github.com/MarcinSufa/claude-watch-video/actions/workflows/smoketest.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20macOS%20%7C%20linux-lightgrey.svg)](#prerequisites-any-path)
[![Plugin](https://img.shields.io/badge/Claude%20Code-plugin-purple.svg)](https://docs.claude.com/en/docs/claude-code/plugins)

Turn *"watch CON-1234 and tell me what broke"* into a single command. The skill downloads the video, extracts keyframes with ffmpeg, transcribes audio with local or hosted Whisper, deduplicates near-identical frames while preserving narrated moments, optionally OCRs on-screen text, and writes a paste-ready `report.md`. Costs **$0 to a few cents** per video. See [what it costs](#what-it-costs) for the breakdown.

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
| OCR (`--ocr`) | `pip install --user pytesseract` + Tesseract binary ([SKILL.md → OCR setup](SKILL.md#on-screen-text-search-with---ocr)) |
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

Two real runs with all artifacts and timings captured verbatim. Collapsed by default to keep this page short — click to expand the one you care about.

<details>
<summary>📊 <strong>Powell's FOMC statement</strong> — 5:30 video → 5 quantitative highlights at $0, 65s end-to-end <em>(click to expand)</em></summary>
<br>

The skill is run against the [Federal Reserve's official FOMC Introductory Statement, March 18, 2026](https://www.youtube.com/watch?v=SVrdJINZGIM) — Powell delivering the rate-decision opening remarks. Every artifact below is captured verbatim from the actual run; numbers and quotes are real.

**The command:**

```bash
python scripts/watch_video.py "https://www.youtube.com/watch?v=SVrdJINZGIM" \
  --workdir c:\tmp\fomc-demo --dedup --verbose
```

Then ran `highlights.py` against the prompt **"summarize the rate decision, inflation outlook, and rate-path forecast"** to pick the 5 most relevant moments.

**What landed on disk:**

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

**Smart dedup on a talking-head video:**

```
"dedup": { "before": 60, "after": 60, "dropped": 0,
           "kept_by_temporal_protection": 47,
           "kept_by_transcript_protection": 15 }
```

Zero frames dropped — every uniform-interval frame fell within either the 5-second min-interval window or the ±1.5s transcript-paragraph protection window. This is the *correct* behavior for a continuous-narration source. (For a screen recording with long static stretches, dedup typically removes 40–60%; see the second walkthrough below.)

**`highlights.md` rendered from the actual run** — frame thumbnail + verbatim transcript quote + analysis context per moment:

| Time | Why it matters |
|---|---|
| **00:22** | Headline rate decision: *"FOMC decided to leave our policy rate unchanged."* Committee judges current stance appropriate for the dual mandate. |
| **01:07** | Growth outlook: median SEP participant projects real GDP +2.4% in 2026 and +2.3% in 2027 — both stronger than December. Housing remains the weak link. |
| **02:21** | Inflation snapshot: total PCE +2.8% YoY (Feb), core PCE +3.0%. Goods-sector inflation boosted by tariffs; near-term expectations elevated by oil supply disruptions. |
| **03:23** | Target range held at **3.50%–3.75%**. Powell notes 3.4 ppts of cuts from Sep–Dec bring policy within plausible estimates of neutral. |
| **04:28** | Rate-path forecast (the dot plot): median sees fed funds at 3.4% end-2026 and 3.1% end-2027 — unchanged from December. Meeting-by-meeting, not a preset course. |

A 5:30 monetary-policy address distilled to the 5 quantitative bullets a fixed-income analyst, IR lead, or financial-services LLM agent actually needs. Drop into a research note, a Slack thread, a desk-readout email, or feed straight into a downstream model.

Frame previews: [00:22 — rate decision](docs/images/fomc/00-22-rate-decision.jpg) · [02:21 — inflation snapshot](docs/images/fomc/02-21-inflation-snapshot.jpg) · [03:23 — fed funds target](docs/images/fomc/03-23-fed-funds-target.jpg) · [04:28 — dot plot](docs/images/fomc/04-28-dot-plot.jpg).

**Total wall-clock: 65 s** (`elapsed_seconds: 65.05` in `meta.json`)

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

Transcribe is the bottleneck on a 5-minute speech with local Whisper. Swap `--whisper groq` and the same input drops under ~10s for the transcribe step on Whisper-large-v3 hosted.

</details>

<details>
<summary>🆕 <strong>Claude Code v2.1.142 release-notes</strong> — 54s video → 5 actionable workflow changes in 29s <em>(click to expand)</em></summary>
<br>

A 54-second product-release video that you'd otherwise need to actually watch to know whether anything in it changes how you work. Run against ["Claude Code v2.1.142 — Full Control Over Background Agents"](https://www.youtube.com/watch?v=O664gH_szoY) with the prompt **"show me how this will improve how i work with claude"**. Same pipeline as FOMC, different value: research/learning instead of macro analysis.

**The command:**

```bash
python scripts/watch_video.py "https://www.youtube.com/watch?v=O664gH_szoY" \
  --workdir c:\tmp\claude-features-demo --dedup --verbose
```

**Smart dedup on a fast-cut release video:**

```
"dedup": { "before": 25, "after": 14, "dropped": 11,
           "kept_by_temporal_protection": 4,
           "kept_by_transcript_protection": 5 }
```

44% reduction (25 → 14 frames) without losing any narrated moment. The 5 transcript paragraphs each had a ±1.5s protected window; another 4 frames survived because of the 5-second min-interval rule; the remaining 11 redundant frames were dropped. This is the dedup story for fast-cut B-roll-heavy content.

**Full `transcript.md` (54 seconds, 6 paragraphs):**

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

**`highlights.md` for the prompt *"show me how this will improve how i work with claude"*:**

| Time | Workflow win |
|---|---|
| **00:08** | Eight new dispatch-time flags (`--model`, `--effort`, `--permission-mode`, `--mcp-config`, `--settings`, `--add-dir`, `--plugin-dir`, `--dangerously-skip-permissions`) — tune each agent for the specific task instead of one shared profile. |
| **00:19** | "Every background session is now fully configurable before it ever starts" — config is part of the dispatch command, so scripts and aliases can encode entire workflows. |
| **00:28** | Fast mode now defaults to Opus 4.7. Pin the previous version via the env override if you need deterministic behavior for benchmarks. |
| **00:36** | Mac fix: background sessions no longer vanish after laptop sleep. Long-running agents survive a closed lid. |
| **00:46** | The daemon detects clock jumps instead of treating them as idle time. Docking/undocking, VPN switches, and timezone changes no longer silently kill in-flight agents. |

A 54-second release video distilled to the 5 things a power user would actually change about how they work. Each pick is anchored to a frame and the verbatim transcript paragraph, so the user can verify the analysis against the source in two clicks.

Frame previews: [00:08 — eight new dispatch flags](docs/images/claude-features/00-08-eight-flags.jpg) · [00:28 — fast-mode → Opus 4.7](docs/images/claude-features/00-28-fast-mode.jpg) · [00:46 — clock-jump fix](docs/images/claude-features/00-46-clock-jumps.jpg).

**Total wall-clock: 29 s** (`elapsed_seconds: 29.16` in `meta.json`)

| Phase | Time |
|---|---|
| Download (1.0 MB / yt-dlp) | ~2 s |
| Probe + audio extract + frames (25 uniform) | ~2 s |
| Transcribe (faster-whisper `small.en`, **local**) | ~24 s |
| Smart dedup (25 → 14 frames) | <1 s |
| Report (md + html + docx) | <1 s |
| Highlights (rendered by `highlights.py`) | <1 s |

Half a minute to know whether a release video changes anything about how you work — instead of either (a) watching 54 seconds + 5 minutes of re-watching to find the flag names, or (b) skipping it and being 6 weeks behind.

**Same pipeline, also great for Jira bug-repro screen-recordings:**

```bash
python scripts/watch_video.py PROJ-2145 --dedup --ocr \
  --highlights-prompt "what is the actual bug and at what moment does it occur" \
  --post-to-jira
```

For a screen-recording attached to a Jira ticket, `--ocr` extracts on-screen text (button labels, field contents, error toasts) into `ocr.txt` which you can `grep` — so "when did the user enter 90?" becomes a sub-second lookup instead of a re-watch.

</details>

---

## Use cases

- **Bug triage from Jira screen recordings** — `python scripts/watch_video.py PROJ-1234 --dedup --ocr` gives you the transcript + key frames + on-screen text in under a minute. Add `--post-to-jira` to attach the analysis back to the ticket
- **Sprint retro on bug videos** — `python scripts/watch_batch.py --jira-jql "project = PROJ AND labels = video-bug AND created >= -7d"` processes a week's videos at once
- **Researching public content** — point at any YouTube/Loom URL; captions-first means free + sub-5-second on most YouTube videos
- **Podcast / multi-speaker transcription** — `--whisper deepgram` gives anonymous speaker labels (`S0`, `S1`) and a `speakers.json` summary you can later relabel with real names
- **Compliance / privacy-first** — default `--whisper local` runs entirely on your machine; nothing is uploaded
- **CI integration** — auto-analyze Playwright/Cypress failure videos on CI; example workflow in [SKILL.md → CI](SKILL.md#ci-integration)

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

# Multi-speaker podcast with diarization
python scripts/watch_video.py PROJ-1234 --whisper deepgram

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

Latest release: **v2.3.0** ([changelog](https://github.com/MarcinSufa/claude-watch-video/releases)).

What's queued:
- **v2.3.1** — `relabel_speakers` tool: agent reads `speakers.json` from a Deepgram run, infers real names from context (intros, on-screen overlays), then rewrites the transcript with `**Joe**` / `**Naval**` instead of `**S0**` / `**S1**`
- **v2.3.2** — WhisperX local diarization (free + offline alternative to Deepgram)
- **v2.4.0+** — OCR cross-correlation for screen-recording name overlays (auto-label speakers from Zoom/Teams/Meet name tags)

Full roadmap: [ROADMAP.md](ROADMAP.md).

---

## License

MIT — see [LICENSE](LICENSE). Free for any use, commercial OK, just keep the copyright notice.

## Acknowledgements

Built on [ffmpeg](https://ffmpeg.org/), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Tesseract](https://github.com/tesseract-ocr/tesseract), [Pillow](https://pillow.readthedocs.io/), [imagehash](https://github.com/JohannesBuchner/imagehash), [python-docx](https://github.com/python-openxml/python-docx), and the [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python). LLM highlight picking optionally uses [OpenAI](https://github.com/openai/openai-python), [Groq](https://groq.com/), [DeepSeek](https://api.deepseek.com), or [Google Gemini](https://ai.google.dev/) via their OpenAI-compatibility endpoints. Speaker diarization via [Deepgram Nova-3](https://deepgram.com).
