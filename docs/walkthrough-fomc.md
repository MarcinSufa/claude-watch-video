# Walkthrough — Powell's FOMC statement (macro analysis)

A real end-to-end run, not a hypothetical. The skill is run against the [Federal Reserve's official FOMC Introductory Statement, March 18, 2026](https://www.youtube.com/watch?v=SVrdJINZGIM) — Powell delivering the rate-decision opening remarks. Every artifact below is captured verbatim from the actual run; numbers and quotes are real.

## The command

```bash
python scripts/watch_video.py "https://www.youtube.com/watch?v=SVrdJINZGIM" \
  --workdir c:\tmp\fomc-demo --dedup --verbose
```

Then ran `highlights.py` against the prompt **"summarize the rate decision, inflation outlook, and rate-path forecast"** to pick the 5 most relevant moments.

## What landed on disk

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

## Smart dedup on a talking-head video

```
"dedup": { "before": 60, "after": 60, "dropped": 0,
           "kept_by_temporal_protection": 47,
           "kept_by_transcript_protection": 15 }
```

Zero frames dropped — every uniform-interval frame fell within either the 5-second min-interval window or the ±1.5s transcript-paragraph protection window. This is the *correct* behavior for a continuous-narration source: nothing redundant to remove. (For a screen recording with long static stretches, dedup typically removes 40–60%; see [walkthrough #2](walkthrough-claude-code-release.md).)

## Sample of `transcript.md`

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

## `highlights.md` for the prompt *"summarize the rate decision, inflation outlook, and rate-path forecast"*

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

A 5:30 monetary-policy address distilled to the 5 quantitative bullets a fixed-income analyst, IR lead, or financial-services LLM agent actually needs. Frame + verbatim quote + analysis context. Drop into a research note, a Slack thread, a desk-readout email, or feed straight into a downstream model.

Frame previews (the rendered images): [00:22 — rate decision](images/fomc/00-22-rate-decision.jpg), [02:21 — inflation snapshot](images/fomc/02-21-inflation-snapshot.jpg), [03:23 — fed funds target](images/fomc/03-23-fed-funds-target.jpg), [04:28 — dot plot](images/fomc/04-28-dot-plot.jpg).

## Total wall-clock time

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
