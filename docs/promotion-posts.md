# Promotion posts -- watch-video v2.0.0

Copy-paste-ready content for the four launch channels. Pre-drafted so you can fire each one off without composing in the moment.

Repo: <https://github.com/MarcinSufa/claude-watch-video>
Install one-liner: `/plugin marketplace add MarcinSufa/claude-watch-video` then `/plugin install watch-video@claude-watch-video`

---

## Submission status (live tracking)

### claude-watch-video

| List | Status | Link |
|---|---|---|
| **travisvn/awesome-claude-skills** | PR open | <https://github.com/travisvn/awesome-claude-skills/pull/743> |
| **jqueryscript/awesome-claude-code** | PR open | <https://github.com/jqueryscript/awesome-claude-code/pull/284> |
| **hesreallyhim/awesome-claude-code** | 14-day account cooldown (extended from 7 days after issue #1837 was filed during the original hold). Re-submit **after 2026-05-30**. | <https://github.com/hesreallyhim/awesome-claude-code/issues/1836> |
| **ComposioHQ/awesome-claude-skills** | Skipped (their own skills monorepo, not a cross-listing index) | n/a |
| **rohitg00/awesome-claude-code-toolkit** | Skipped (their own plugin distribution) | n/a |

### git-timesheet

| List | Status | Link |
|---|---|---|
| **travisvn/awesome-claude-skills** | PR open (filed 2026-05-16) | <https://github.com/travisvn/awesome-claude-skills/pull/744> |
| **jqueryscript/awesome-claude-code** | PR open (filed 2026-04-22, awaiting review for 24+ days) | <https://github.com/jqueryscript/awesome-claude-code/pull/227> |
| **hesreallyhim/awesome-claude-code** | Closed -- account is in 14-day cooldown due to over-eager submission while watch-video was still in its 7-day hold. Re-submit **after 2026-05-30**. | <https://github.com/hesreallyhim/awesome-claude-code/issues/1837> |

---

## hesreallyhim/awesome-claude-code -- git-timesheet manual submission

**Open this URL in your browser:**

<https://github.com/hesreallyhim/awesome-claude-code/issues/new?template=recommend-resource.yml>

Field-by-field paste content:

| Field | Paste |
|---|---|
| **Display Name** | `git-timesheet` |
| **Category** | `Tooling` |
| **Sub-Category** | `General` (or whatever Tooling variant fits best) |
| **Primary Link** | `https://github.com/MarcinSufa/git-timesheet` |
| **Author Name** | `Marcin Sufa` |
| **Author Link** | `https://github.com/MarcinSufa` |
| **License** | `MIT` |

**Description** (1-3 sentences, no emojis):

```
Turns your git commit history into realistic weekly timesheets -- hours allocated by commit complexity (lines changed, files touched), not flat splits. Outputs print-ready PDF and importable CSV, supports public holidays for 9 countries plus PTO/sick days, has 6 built-in languages with dynamic translation, and pushes time entries directly to Toggl, Clockify, TMetric, and Harvest APIs. Matches custom company PDF templates from a sample file, so your output looks like your existing timesheet form.
```

**Validate Claims:**

```
Install via /plugin marketplace add MarcinSufa/git-timesheet then /plugin install git-timesheet@marcin-sufa-plugins. The README documents the install command, a quick-start example, and a How-It-Works section with a Mermaid diagram of the commit-weight allocation algorithm. The repo's examples/ directory includes a sample-timesheet.pdf showing the actual output format.
```

**Specific Task(s):**

```
1. Generate a weekly timesheet for the current week: "/timesheet for last week" -- the plugin reads commits across configured repos, weighs each by lines changed and files touched, allocates 8 hours per day proportionally, and writes a PDF + CSV.
2. Push generated entries to Toggl/Clockify/TMetric/Harvest: configure the integration once in your profile, then run "/timesheet --push toggl" to send the entries directly via the provider's API.
3. Match a company-specific timesheet format: drop a sample PDF or PNG of your employer's timesheet form in the configured templates directory, and the plugin imitates the layout for future outputs.
```

**Specific Prompt(s):**

```
- "Generate my timesheet for the week of May 5 to May 9, 2026, using the 'sysdyne' profile."
- "Create a CSV timesheet for the last two weeks across all my repos, using Polish holidays."
- "Generate this week's timesheet as PDF and push it to Clockify."
```

**Additional Comments (optional):**

```
First commit: 2026-04-22. Latest tag: 1.0.4-beta. The plugin is part of the marcin-sufa-plugins marketplace at https://github.com/MarcinSufa/git-timesheet. Output examples are in examples/. Supports configurable work hours, project mapping, saved per-client profiles, and 6 languages out of the box (English, Polish, German, French, Spanish, Italian) plus dynamic translation for other locales.
```

**Recommendation Checklist** -- tick all 5 boxes (repo is 24 days old so the one-week-rule item is clean).

---

## hesreallyhim/awesome-claude-code -- watch-video manual submission (HELD until 2026-05-21)

This list bans PR submissions and explicitly blocks `gh` CLI. Submission must be done via the GitHub web UI issue form by a human. Below is exact content to paste into each field.

**Open this URL in your browser:**

<https://github.com/hesreallyhim/awesome-claude-code/issues/new?template=recommend-resource.yml>

Then fill in:

| Field | Paste this |
|---|---|
| **Display Name** | `watch-video` |
| **Category** | `Agent Skills` |
| **Sub-Category** | `General` |
| **Primary Link** | `https://github.com/MarcinSufa/claude-watch-video` |
| **Author Name** | `Marcin Sufa` |
| **Author Link** | `https://github.com/MarcinSufa` |
| **License** | `MIT` |

**Description** (1-3 sentences, no emojis -- they explicitly ban emojis here):

```
Watches any video (local file, public URL via yt-dlp, or Jira attachment) and produces a paste-ready evidence bundle: timestamped frames, transcript (free YouTube captions or local faster-whisper), LLM-driven highlights with a user-defined prompt, and three report formats (Markdown, self-contained HTML with base64 frames, and Word DOCX). Pipeline runs locally with zero API cost by default; smart dedup with transcript-aware protection drops 40-60 percent of redundant frames on screen recordings without losing narrated moments. Also installable as an MCP server for Claude Desktop, Codex CLI, Cursor, Continue.dev, Cline, Windsurf, Zed, and VS Code Copilot Chat.
```

**Validate Claims** (how a reviewer can verify the claims):

```
Install via /plugin marketplace add MarcinSufa/claude-watch-video then /plugin install watch-video@claude-watch-video. The README's two end-to-end walkthroughs document real runs with verbatim artifacts -- a 5:30 Powell FOMC press conference (https://www.youtube.com/watch?v=SVrdJINZGIM, 65 seconds wall-clock, 60 frames, 29 transcript paragraphs) and a 54-second Claude Code release video (https://www.youtube.com/watch?v=O664gH_szoY, 3.82 seconds wall-clock with captions-first transcription). The repo includes a smoketest at scripts/smoketest.py that runs a full pipeline end-to-end against a synthetic test video and asserts all stages succeed.
```

**Specific Task(s):**

```
1. Triage a Jira ticket with a screen-recording attachment: ask Claude "watch CON-1234 and identify the bug" -- the skill auto-fetches via Atlassian REST API, runs the pipeline, and Claude reads frames + transcript + OCR to answer.
2. Summarize a long-form YouTube video against a user prompt: ask Claude "watch https://www.youtube.com/watch?v=SVrdJINZGIM and summarize the rate decision and inflation outlook" -- the skill prefers free YouTube captions over Whisper, then runs LLM-driven highlights (Anthropic/OpenAI/Groq) to pick the 5 most relevant moments with frame + reason + verbatim quote.
3. Bulk-process a sprint of bug videos for retro: ask Claude to run watch_batch.py with --jira-jql "project = PROJ AND labels = video-bug AND created >= -7d" -- 20 tickets become 20 reports in roughly 3 minutes.
```

**Specific Prompt(s):**

```
- "Watch https://www.youtube.com/watch?v=O664gH_szoY and tell me what's new for Claude Code users."
- "Watch CON-1234 and write a bug analysis I can paste into the Jira ticket."
- "Watch demo.mp4 from 2:30 to 3:00 with OCR and tell me what UI state caused the error."
```

**Additional Comments** (optional but useful):

```
Latest tag: v2.0.0 (2026-05-16). Released v2.0.0 ships an MCP server wrapper so the same pipeline works in Claude Desktop, Codex CLI, Cursor, Continue.dev, Cline, Windsurf, Zed, and VS Code Copilot Chat -- not just Claude Code. The skill defaults to zero API cost (local ffmpeg + free YouTube captions or local Whisper); hosted Whisper and LLM-driven highlights are opt-in. Jira posting is opt-in only with a confirmation gate that runs before any attachment uploads, preserving a no-unsolicited-writes invariant.
```

**Recommendation Checklist** -- tick all five boxes after reviewing.

---

## 1. Awesome-list PRs (no account warmup needed -- do these first)

### Target lists

| List | Stars | URL |
|---|---|---|
| **hesreallyhim/awesome-claude-code** | 36.8k | <https://github.com/hesreallyhim/awesome-claude-code> |
| jqueryscript/awesome-claude-code | medium | <https://github.com/jqueryscript/awesome-claude-code> |
| travisvn/awesome-claude-skills | medium | <https://github.com/travisvn/awesome-claude-skills> |
| ComposioHQ/awesome-claude-skills | small | <https://github.com/ComposioHQ/awesome-claude-skills> |
| Chat2AnyLLM/awesome-claude-plugins | small | <https://github.com/Chat2AnyLLM/awesome-claude-plugins> |

### One-line entry (paste into the right section -- typically "Skills" or "Plugins" alphabetically)

```markdown
- [watch-video](https://github.com/MarcinSufa/claude-watch-video) - Give Claude eyes and ears for any video. Local files, public URLs (YouTube/Loom/Vimeo/~1500 sites), or Jira attachments. $0 pipeline (local ffmpeg + free YouTube captions or local Whisper), smart dedup, optional Tesseract OCR for screen recordings, LLM-driven highlights with user prompt, opt-in Jira post-back. Also installable as an MCP server (Claude Desktop, Codex, Cursor, Cline, etc.).
```

### PR description (use this as the PR body)

```
Adds watch-video -- a video-analysis plugin for Claude Code.

Why this entry: most "watch a video" skills handle YouTube and call it
a day. watch-video is the one that closes the loop for dev workflows:
Jira attachment fetch via API, OCR for screen-recording UIs, transcript-
aware smart dedup, LLM-driven highlights with user-defined prompts,
multi-format reports (.md / .html / .docx), and opt-in Jira posting
with a confirmation gate.

Pipeline runs locally with $0 API cost by default. Hosted Whisper +
LLM highlights opt-in for users who want them.

v2.0.0 also ships an MCP server wrapper so the same pipeline works in
Claude Desktop, Codex CLI, Cursor, Continue.dev, Cline, Windsurf, Zed,
and VS Code Copilot Chat -- not just Claude Code.

Repo: https://github.com/MarcinSufa/claude-watch-video
License: MIT
```

### How to submit

```bash
gh repo fork hesreallyhim/awesome-claude-code --clone --remote
cd awesome-claude-code
# Find the right section (probably "Skills" or "Plugins"). Insert
# the one-line entry alphabetically.
git checkout -b add-watch-video
git add README.md
git commit -m "Add watch-video skill"
git push -u origin add-watch-video
gh pr create --fill
```

Repeat for each of the 5 lists. ~5 minutes total per list. Compound passive traffic.

---

## 2. Show HN submission

### Title (under 80 chars)

```
Show HN: Make Claude Code watch any video, $0 pipeline cost
```

Alt titles to A/B with friends before posting:

- `Show HN: watch-video - a Claude Code plugin that watches videos for $0`
- `Show HN: Smart-dedup video pipeline for Claude Code agents`
- `Show HN: Bug-triage videos with Claude Code -- 300x cheaper than raw upload`

### Post URL field

```
https://github.com/MarcinSufa/claude-watch-video
```

### Post body (first comment after submitting)

```
Built this to solve a specific problem: Jira tickets with attached
screen-recording bug videos that nobody wants to re-watch every time
they get assigned. The pipeline now sits in front of Claude Code (and
Claude Desktop / Codex / Cursor / Cline via MCP):

  /plugin marketplace add MarcinSufa/claude-watch-video
  /plugin install watch-video@claude-watch-video

  > Watch CON-1234 and tell me what the bug is

It downloads the video (yt-dlp for public URLs, Atlassian REST API for
Jira attachments), extracts keyframes with ffmpeg, transcribes with
captions-first / faster-whisper fallback, smart-dedups frames using
perceptual hash + transcript-aware protection (so narrated moments
never get dropped), optionally OCRs on-screen text, and produces a
paste-ready report.md / .html / .docx.

A few design choices that made it interesting:

1. CAPTIONS-FIRST TRANSCRIPTION. For YouTube content yt-dlp already
   downloads the manual or auto-generated VTT captions alongside the
   video. Parsing those is free and instantaneous; Whisper only fires
   if no captions exist. Real measurement: 3.82s end-to-end on a 54s
   YouTube video vs 29.16s with local Whisper. 7.6x speedup.

2. SMART DEDUP. Naive perceptual-hash dedup drops near-identical frames
   -- but UI screen recordings have huge unchanged areas (sidebars,
   chrome) where a small but critical change (a typed value, an icon
   flip) gets misclassified as duplicate. Smart dedup adds two
   protections: temporal (always keep one frame per N seconds) and
   transcript-aware (always keep frames within +/- 1.5s of a
   transcript paragraph start). The narrator said something important
   at that moment; the visual must be preserved. 40-60% reduction on
   typical screen recordings without losing narrated content.

3. NO UNSOLICITED JIRA WRITES. The skill can post the analysis back
   to the source Jira ticket -- but only when you explicitly pass
   --post-to-jira, and even then there's a confirmation gate that
   runs BEFORE any attachment uploads (so declining leaves the ticket
   completely untouched, no orphan state). The MCP server preserves
   this with a confirm=True flag the host must pass after user
   authorization.

4. ~300X CHEAPER THAN RAW CLAUDE VIDEO. Sending raw video to Claude
   API would tokenize every frame at 30fps -- ~500k input tokens for
   a 54s clip, ~$7.50 on Opus pricing. watch-video smart-dedups
   1620 raw frames to 16 keepers and has the agent read 4-8 of them.
   Same answer for ~$0.25 on Opus, or ~$0.015 on Haiku.

Tech stack: Python, ffmpeg, faster-whisper, pHash via imagehash,
Tesseract via pytesseract, python-docx, anthropic + openai SDKs,
yt-dlp, FastMCP. ~3000 lines, MIT licensed.

I'd love feedback on the dedup heuristic in particular -- I tuned the
defaults on bug-triage screen recordings, but it might need different
constants for movie clips or live talks. PRs welcome.

GitHub: https://github.com/MarcinSufa/claude-watch-video
ROADMAP: https://github.com/MarcinSufa/claude-watch-video/blob/main/ROADMAP.md
```

### Submission tips

- Submit Tue/Wed/Thu between 8-10am Pacific Time for highest velocity
- Title MUST start with `Show HN:` -- HN's discovery requires it
- Do NOT upvote your own submission from a second account; HN's spam detector catches this and shadow-bans
- Engage with every comment in the first 2 hours -- HN ranking weights early engagement heavily
- Be honest in replies -- HN audience is technical and detects marketing-speak

---

## 3. Reddit /r/ClaudeAI post

### Title

```
I built a Claude Code plugin that watches YouTube/Jira videos and writes paste-ready bug reports (open source, $0 pipeline)
```

### Body

```
After getting tired of clicking through 5-minute screen-recording bug
videos on Jira tickets, I built /watch-video -- a Claude Code plugin
that downloads videos (local, URL, or Jira attachment), extracts
frames, transcribes with free YouTube captions or local Whisper,
smart-dedups, and produces a paste-ready evidence-bundle report
(.md / .html / .docx) plus LLM-picked highlights against a user prompt.

Install (one line each):

    /plugin marketplace add MarcinSufa/claude-watch-video
    /plugin install watch-video@claude-watch-video

Then in any conversation:

    Watch CON-1234 and identify the bug.
    Watch https://youtu.be/XYZ and summarize the rate decision.
    Watch C:/Users/me/Downloads/demo.mp4 and explain what changed.

**Why it's different from other Claude video skills:**

- Jira attachment auto-download via Atlassian REST API (with multi-attachment disambiguation)
- Smart dedup with transcript-aware protection -- preserves the moment the user typed the wrong value even if the UI looks visually identical
- OCR layer tuned for screen-recording UIs (Tesseract with 2x upscale + auto-invert + PSM 6) so you can grep on-screen text
- LLM-driven highlights with user prompt ("identify the bug and the moment it occurs") -- works on Anthropic / OpenAI / Groq
- Opt-in Jira post-back with a real confirmation gate (declining leaves the ticket fully untouched -- no orphan attachments)
- Batch mode (--jira-keys or JQL query) for sprint retros
- Per-step content-hash cache makes re-runs ~120x faster

**Cost:** the pipeline is $0 (local ffmpeg + free YouTube captions or
local Whisper). The only token cost is when the agent reads the
artifacts to answer your question: ~$0.015 per video on Claude Haiku,
~$0.25 on Opus. Compare to raw Claude video upload, which would cost
~$7.50 for the same 54s video at 30fps tokenization.

**Also installable as an MCP server** for Claude Desktop, Codex CLI,
Cursor, Continue.dev, Cline, Windsurf, Zed, VS Code Copilot Chat.
See [mcp-server/README.md] in the repo.

GitHub: https://github.com/MarcinSufa/claude-watch-video

Happy to answer questions or take feature requests -- the ROADMAP
file in the repo lists what's next (GitHub Issues integration,
Linear, CI Playwright triage recipes).
```

### Posting tips

- New Reddit accounts need ~10-20 comment karma before /r/ClaudeAI auto-approves posts. Spend 15 min commenting helpfully on 2-3 existing posts first.
- Don't post on weekends -- weekday morning Pacific is best
- Reply to every comment in the first 4 hours
- If a mod removes it as self-promotion, message them with the "I'm the maintainer, this is open source, here's the receipt" angle -- usually gets reinstated

---

## 4. Twitter/X thread (4 tweets)

### Tweet 1 (hook + visual)

```
Built a Claude Code plugin that watches videos for you.

Point it at a YouTube URL, local file, or Jira ticket -- get back a
paste-ready report with timestamped frames, transcript, and LLM-picked
highlights.

Open source, MIT, $0 pipeline cost.

github.com/MarcinSufa/claude-watch-video
```

ATTACH IMAGE: the FOMC highlight card screenshot from the README's "What it produces" section -- shows the Powell frame at 00:22 with the bold "Why this matters" and the verbatim quote. Use a desktop screenshot of github.com rendering it. That's the share-able artifact.

### Tweet 2 (the install one-liner)

```
Install in 10 seconds:

/plugin marketplace add MarcinSufa/claude-watch-video
/plugin install watch-video@claude-watch-video

Then in any Claude Code chat:
"Watch this YouTube link and summarize" or
"Watch CON-1234 and identify the bug"

Auto-detects URL vs Jira key vs local file.
```

### Tweet 3 (the technical hook -- 300x)

```
The cost story: raw Claude video upload would charge ~$7.50 for a
54s clip (30fps frame tokenization on Opus). watch-video smart-dedups
1620 raw frames to 16 keepers and has the agent read 4-8 of them.

Same answer. ~$0.25 on Opus, ~$0.015 on Haiku. ~300x cheaper.

How: 🧵👇
```

### Tweet 4 (technical depth + close)

```
1. Captions-first transcription (free YouTube VTTs > paid Whisper)
2. Smart dedup w/ transcript-aware protection -- never drops a
   narrated moment
3. Strategic frame sampling at read time (4-8 of 16-80 kept)

Plus MCP server for Claude Desktop, Codex CLI, Cursor, Cline, etc.

Roadmap + docs: github.com/MarcinSufa/claude-watch-video
```

### Posting tips

- Tag @AnthropicAI and @alexalbert__ on the first tweet -- might get a retweet
- Post Tue-Thu morning Pacific
- Pin the thread to your profile after posting
- Quote-retweet your own first tweet 24h later if engagement is good -- gets a second visibility wave
- Reply to anyone who engages, ESPECIALLY in the first hour

---

## Cross-channel ordering (recommended sequence)

1. **awesome-claude-code PRs** -- 5 PRs, 30 min total. Submit while you wait for the other accounts to be ready. Long-tail compounding traffic.
2. **Show HN** -- requires HN account (probably already have one if you've ever commented). Submit Tue 8am Pacific.
3. **Twitter thread** -- after the Show HN settles (~24h). Use the HN URL as social proof in tweet 1 if it got traction.
4. **/r/ClaudeAI** -- after Twitter (give Reddit account time to age past spam filters). Reference the GitHub repo + the HN/Twitter activity as proof of life.

The order matters because each channel feeds the next -- HN gets you GitHub stars, stars give Twitter credibility, Twitter gives Reddit a "this exists" anchor.

---

## What NOT to post

- Don't include API keys or screenshots of your `~/.atlassian-token/credentials.json`
- Don't include real Jira ticket IDs from your company (only the generic PROJ-1234 placeholders the README uses)
- Don't claim performance numbers you haven't measured -- the 7.6x speedup, ~300x cost ratio, and Haiku $0.015 are all from real measurements documented in the repo. Anything beyond that, omit
- Don't disparage other plugins by name (bradautomates/claude-video is a perfectly good alternative for non-Jira workflows -- the README's "How it compares" section gets the tone right; mirror that)

---

## After posting -- measurement

Watch these signals (manually for now; future v2.1 may include automation):

| Channel | Signal | Where to check |
|---|---|---|
| GitHub | stars, forks, issues, PR submissions | <https://github.com/MarcinSufa/claude-watch-video> |
| Show HN | upvotes, comments, position on front page | <https://news.ycombinator.com/item?id=YOUR_ID> |
| Twitter | impressions, retweets, replies | analytics on the thread |
| Reddit | upvotes, comments, awards | the post page |
| Anthropic marketplace | "Approved" status flip | <https://claude.ai/settings/plugins> |

The marketplace approval is the lagging indicator -- it'll happen on Anthropic's timeline regardless of how the launch posts go. Optimize for the leading indicators (GitHub stars + HN/Reddit engagement).
