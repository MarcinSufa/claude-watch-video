# Bulk mode: watch_batch.py

Read when the user wants several videos or Jira tickets processed in one go (a list of keys, a JQL query, or many paths/URLs).

For processing many videos in one go (sprint retro, weekly support triage, multi-attachment review), use the batch orchestrator:

```bash
# Three input modes -- mutually exclusive
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_batch.py" --jira-keys PROJ-1234,PROJ-1235,PROJ-1236
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_batch.py" --jira-jql "project=PROJ AND labels=video-bug AND created >= -7d"
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_batch.py" --inputs "/data/a.mp4,https://youtu.be/abc,PROJ-9999"
```

Any flag not consumed by `watch_batch.py` is forwarded to each `watch_video.py` invocation:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_batch.py" --jira-keys PROJ-1234,PROJ-1235 --dedup --ocr --whisper groq
```

## Layout

Each item gets its own workdir under `<batch-dir>/<slug>/`:

```
${TMPDIR:-/tmp}/watch-batch-<timestamp>/
├── batch.json              # per-item results + aggregate summary
├── proj-1234/              # workdir for PROJ-1234 (same layout as single-video)
│   ├── frames/
│   ├── transcript.md
│   ├── ocr.txt
│   ├── report.md
│   └── meta.json
├── proj-1235/
│   └── ...
└── proj-1236/
    └── ...
```

## batch.json schema

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

## Failure handling

- **Continue-on-error is the default.** Per-item failures don't abort the batch. Each item's status (`ok` / `ambiguous` / `failed` / `ok_no_meta`) is recorded.
- **`--strict`** flips that: any item failure or ambiguity makes the whole batch return non-zero.
- **Default exit code:** `0` if at least one item succeeded; non-zero only when ALL items failed.

## Ambiguous items

If a Jira ticket has multiple video attachments, that item lands in the `ambiguous` bucket with the candidates list. To resolve: re-run that one ticket directly with `watch_video.py --attachment-id <ID>`. The other items in the batch are unaffected.

## What it does NOT do

- **Does not post anything to Jira.** No comments, no transitions, no edits. Read-only API usage.
- **Sequential by default.** Parallel processing is a future flag; today batch runs items one at a time. For 20 short videos this is typically <5 minutes; for hour-long videos you may want to chunk manually.
