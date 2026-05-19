# Walkthrough — Claude Code release-notes video (personal workflow)

A 54-second product-release video that you'd otherwise need to actually watch to know whether anything in it changes how you work. The skill is run against ["Claude Code v2.1.142 — Full Control Over Background Agents"](https://www.youtube.com/watch?v=O664gH_szoY) with the prompt **"show me how this will improve how i work with claude"**. Same pipeline as the [FOMC walkthrough](walkthrough-fomc.md), different value: research/learning instead of macro analysis.

## The command

```bash
python scripts/watch_video.py "https://www.youtube.com/watch?v=O664gH_szoY" \
  --workdir c:\tmp\claude-features-demo --dedup --verbose
```

Then `highlights.py` against the prompt above.

## What landed on disk

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

## Smart dedup on a fast-cut release video

```
"dedup": { "before": 25, "after": 14, "dropped": 11,
           "kept_by_temporal_protection": 4,
           "kept_by_transcript_protection": 5 }
```

44% reduction (25 → 14 frames) without losing any narrated moment. The 5 transcript paragraphs each had a ±1.5s protected window; another 4 frames survived because of the 5-second min-interval rule; the remaining 11 redundant frames were dropped. This is the dedup story for fast-cut B-roll-heavy content.

## Full `transcript.md` (54 seconds, 6 paragraphs)

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

## `highlights.md` for the prompt *"show me how this will improve how i work with claude"*

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

A 54-second release video distilled to the 5 things a power user would actually change about how they work. Each pick is anchored to a frame and the verbatim transcript paragraph, so the user can verify the analysis against the source in two clicks. Drop into a team-wide "what's new" message, paste into your weekly notes, or have Claude reason over `highlights.json` to update your shell aliases.

Frame previews: [00:08 — eight new dispatch flags](images/claude-features/00-08-eight-flags.jpg), [00:28 — fast-mode → Opus 4.7](images/claude-features/00-28-fast-mode.jpg), [00:46 — clock-jump fix](images/claude-features/00-46-clock-jumps.jpg).

## Total wall-clock time

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

## Same pipeline, also great for Jira bug-repro screen-recordings

The same `watch_video.py` accepts a Jira issue key directly:

```bash
python scripts/watch_video.py PROJ-2145 --dedup --ocr \
  --highlights-prompt "what is the actual bug and at what moment does it occur" \
  --post-to-jira
```

For a screen-recording attached to a Jira ticket, `--ocr` extracts on-screen text (button labels, field contents, error toasts) into `ocr.txt` which you can `grep` — so "when did the user enter 90?" becomes a sub-second lookup instead of a re-watch. Smart dedup on screen recordings typically removes 40–60% of frames (long static stretches collapse to one frame, narrated moments are preserved). Add `--post-to-jira` to write the analysis back to the same ticket with the explicit-confirmation safety stack.
