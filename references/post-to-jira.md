# Posting report.md to Jira (opt-in only)

Read only when the user, in the current message, asked to post the analysis to a Jira ticket, or asked for headless (API-key) highlights.

`scripts/post_to_jira.py` takes a workdir and posts the generated `report.md` as a comment on the source Jira issue, via the same Atlassian REST API the skill uses for downloads.

**This feature is opt-in everywhere and never runs automatically.** Specifically:

- `watch_video.py --post-to-jira` defaults OFF. Without the flag, no comment is ever posted.
- `post_to_jira.py` is a standalone tool. It runs only when you explicitly invoke it.
- Even with the flag, the script confirms interactively unless you pass `--post-to-jira-yes`.
- An idempotency check scans recent comments on the ticket; if a prior `/watch-video` analysis is already there, the script aborts (unless `--force`).

The agent guidance is reinforced in memory: never invoke this path silently. If the user hasn't explicitly asked for "post" / "comment on the ticket" / "send to PROJ-XXXX" in the current message, do not pass these flags.

## Usage

Single video, post the analysis after generation:

```bash
# Generate then post in one command (with confirmation prompt):
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_video.py" PROJ-1234 --dedup --ocr --post-to-jira

# Generate first, post later (standalone tool):
python3 "${CLAUDE_SKILL_DIR}/scripts/watch_video.py" PROJ-1234 --dedup --ocr
# ...review report.md, decide it's good...
python3 "${CLAUDE_SKILL_DIR}/scripts/post_to_jira.py" /tmp/watch-proj-1234

# Dry-run preview (no POST):
python3 "${CLAUDE_SKILL_DIR}/scripts/post_to_jira.py" /tmp/watch-proj-1234 --dry-run

# Bypass confirmation (only with explicit prior authorization):
python3 "${CLAUDE_SKILL_DIR}/scripts/post_to_jira.py" /tmp/watch-proj-1234 --yes

# Post to a different ticket than the source (override):
python3 "${CLAUDE_SKILL_DIR}/scripts/post_to_jira.py" /tmp/watch-foo --jira-key PROJ-9999
```

## Flags

| Flag | Effect |
|---|---|
| `--jira-key KEY` | Override the issue key from `meta.json`'s `video.issue_key` (useful for posting to a related ticket). |
| `--dry-run` | Print preview + ADF block summary; DO NOT POST. |
| `--yes` | Skip the interactive confirmation prompt. Use only when you've explicitly authorized this specific post. |
| `--force` | Bypass the idempotency check; post even if a prior `/watch-video` comment already exists. |
| `--no-embed-images` | Skip image embedding (`mediaSingle` ADF nodes); reference frames as italic text. Default behavior is to embed. Useful when the API token user lacks attachment permissions. |
| `--style collapsed\|inline\|summary` | Comment layout. **Default: `collapsed`** wraps the timeline in an ADF expand panel (compact ticket UI, click to expand). `inline` shows the full timeline expanded (legacy v1.5.0 behavior, good for short bug repros). `summary` posts only N key moments and attaches `report.html` as a downloadable artifact (good for long videos / archival-heavy teams). |
| `--summary-key-frames N` | With `--style summary`: number of key timeline moments to include in the comment (default 3, evenly distributed first/middle/last; replaced by `highlights.json` picks when that file exists). |
| `--credentials PATH` | Override default credentials JSON location. |

The `--highlights-*` flags belong to `watch_video.py` (see its flags table), not to `post_to_jira.py`.

## Two ways to generate highlights

The `highlights` step picks the most relevant moments from the transcript based on a user prompt. Two invocation paths produce the same artifacts (`highlights.json` / `highlights.md` / `highlights.html`):

1. **Inside Claude Code (default, no API key needed)** — ask Claude conversationally: *"watch PROJ-1234 then write highlights for the bug-related parts"*. Claude runs the pipeline and reads the transcript / frames in conversation to produce the highlights files directly. This path also gets multimodal vision (Claude can look at the frames), so visual prompts work too.
2. **Headless (CI / scripted, optional, needs a paid API key)** — `python3 watch_video.py URL --highlights-prompt "..."` or standalone `python3 highlights.py <workdir> --prompt "..."`. Calls the Anthropic API via the SDK (default model: Haiku 4.5, ~$0.001-0.005 per video). Same JSON validator + frame-matching logic; produces the same artifact files.

Either way, `post_to_jira.py --style summary` automatically picks up `highlights.json` and uses those moments instead of evenly-distributed picks.

## What gets posted

`report.md` is converted to Atlassian Document Format (ADF) with a minimal converter:

- `# heading` → ADF heading block (level 1-6)
- `> blockquote` → ADF blockquote
- `---` → ADF horizontal rule
- `![alt](path)` → **embedded image** (`mediaSingle` node). Each referenced frame is first uploaded to the ticket as an attachment via `POST /rest/api/3/issue/<KEY>/attachments`, then its media-services UUID is resolved and embedded in the ADF. Pass `--no-embed-images` to fall back to italic text references (the v1 behavior).
- Plain text → ADF paragraphs

The resulting comment renders cleanly on Atlassian Cloud's UI with proper headings, blockquote styling, and inline frame thumbnails. If the API token user lacks the *Add Attachments* permission on the project, the script logs a warning, falls back to text-only references, and still posts the comment.

## Idempotency signature

`report.md`'s footer includes the literal string `Generated by `/watch-video``. The idempotency check walks recent comments looking for that signature. So if you re-run `watch_video.py --post-to-jira` on the same ticket, the second attempt detects the prior comment and skips. To force a duplicate post (e.g., after editing report.md), pass `--force`.
