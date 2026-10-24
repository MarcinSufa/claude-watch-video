# Per-step cache (resumable re-runs)

Read when a re-run did not pick up a changed flag, or you need `--no-cache` / `--force-step` semantics or the meta.json `cache` block.

`watch_video.py` records a fingerprint for each step in `meta.json`'s `cache` block. On re-run against the same workdir, steps whose fingerprint AND expected output files match are skipped. Result: re-running on a populated workdir is typically <1 second.

## What gets cached

| Step | Fingerprint inputs | Skipped if... |
|---|---|---|
| `fetch` | input string, kind, attachment-id, credentials path, since-seconds | video file exists at the cached path |
| `probe` | (always re-runs — cheap, <1s) | n/a |
| `frames` | video file fingerprint (size:mtime), `--frames`, `--resolution`, `--start`, `--end`, `--scene-mode`, `--scene-threshold` | `frames/` dir exists |
| `transcribe` | video fingerprint, `--start`, `--end`, `--whisper`, `--model`, `--lang` | `transcript.txt` + `transcript.md` exist |
| `dedup` | frames step fp, transcribe step fp, dedup thresholds | `frames/` dir exists |
| `ocr` | frames step fp, dedup step fp, `--ocr-lang`, `--ocr-min-text-length` | `ocr.txt` exists |
| `report` | frames step fp, transcribe step fp, dedup step fp, ocr step fp | `report.md` exists |

## Dependency-aware invalidation

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

## CLI flags

| Flag | Effect |
|---|---|
| `--no-cache` | Bypass cache entirely; re-run every step from scratch and overwrite stored fingerprints. |
| `--force-step NAME[,NAME,...]` | Invalidate specific steps. Downstream is invalidated automatically per the table above. Example: `--force-step transcribe` re-runs transcribe + dedup + report. |

## Benchmarks (typical short video)

| Scenario | Elapsed |
|---|---|
| Cold run (no cache) | ~30 s |
| Cached re-run (same flags) | ~0.25 s |
| One downstream flag changed (e.g. `--dedup-threshold`) | ~12-15 s (skips fetch/transcribe; re-runs dedup/ocr/report) |

## meta.json `cache` block

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
