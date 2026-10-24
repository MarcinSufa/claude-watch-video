# On-screen text search (--ocr) and smart dedup (--dedup)

Read when deciding on `--ocr` or `--dedup`, tuning their thresholds, or when OCR output looks wrong.

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
