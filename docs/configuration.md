# Configuration reference

Full reference for `scripts/watch_video.py` (the CLI orchestrator). Every flag, organized by what it affects. For per-script details (each individual step's flags) see [`SKILL.md`](../SKILL.md).

## Workdir + scope

| Flag | Purpose |
|---|---|
| `--workdir <PATH>` | Override the default `c:\tmp\watch-<slug>\` workdir |
| `--start MM:SS --end MM:SS` | Scope to a window; transcript timestamps stay original-video-time |
| `--no-cache` | Bypass the per-step output cache |
| `--force-step NAME[,...]` | Force a specific step (downstream auto-invalidates) |

## Frame extraction

| Flag | Purpose |
|---|---|
| `--resolution W` | Frame width in px (default 960; use 1280 for dense UI text) |
| `--frames N` | Override default frame budget (auto by duration) |
| `--scene-mode` | Extract frames at scene cuts (auto-fallback to uniform when no scenes) |
| `--dedup` | Smart pHash dedup with transcript-aware protection |
| `--dedup-threshold N` | pHash Hamming threshold (default 5) |

## Transcription

| Flag | Purpose |
|---|---|
| `--whisper auto\|captions\|local\|groq\|openai\|deepgram` | Source. `auto`: VTT captions if yt-dlp pulled one (free), else `local` faster-whisper. `deepgram` adds speaker diarization (writes `speakers.json`, tags transcript paragraphs with `**S0**` / `**S1**`). |
| `--model NAME` | Whisper model id (provider-specific) |
| `--lang en\|pl\|...\|auto` | Audio language |
| `--no-audio` | Skip transcription |
| `--whisper-api-key KEY` | API key for hosted providers (env vars also work) |
| `--whisper-credentials PATH` | JSON file path for hosted-provider keys |

## OCR

| Flag | Purpose |
|---|---|
| `--ocr` | Run Tesseract OCR over kept frames |

## Highlights (LLM-driven moment selection)

| Flag | Purpose |
|---|---|
| `--highlights-prompt "..."` | Enable LLM highlight selection (requires an API key for the chosen provider) |
| `--highlights-provider anthropic\|openai\|groq\|deepseek\|gemini\|openai-compat` | Which LLM. Default `anthropic` (Claude Haiku 4.5). All non-anthropic providers reuse the openai SDK with a base_url override. |
| `--highlights-model NAME` | Model id (defaults vary by provider) |
| `--highlights-base-url URL` | Required only with `--highlights-provider openai-compat` (Together AI, Fireworks, OpenRouter, Ollama, vLLM, ...). Ignored otherwise. |
| `--highlights-api-key KEY` | API key for the chosen highlights provider (env vars also work) |
| `--highlights-credentials PATH` | JSON file path for highlights API key (separate from Atlassian creds) |

## Report output

| Flag | Purpose |
|---|---|
| `--no-html` | Skip `report.html` (Markdown + DOCX still produced) |
| `--no-docx` | Skip `report.docx` (degrades gracefully if `python-docx` is missing anyway) |
| `--no-report` | Skip `report.md` generation entirely |

## Jira integration (opt-in only)

| Flag | Purpose |
|---|---|
| `--attachment-id <ID>` | Disambiguate multiple video attachments on the source issue |
| `--credentials <PATH>` | Override Atlassian credentials JSON path (default `~/.atlassian-token/credentials.json`) |
| `--post-to-jira` | **Opt-in only**: post `report.md` as a Jira comment. Default style is `collapsed` (short comment with click-to-expand timeline) |
| `--post-to-jira-style {collapsed,inline,summary}` | Jira comment layout. Default `collapsed`. |
| `--post-to-jira-summary-key-frames N` | Number of key moments in `--style summary` (default 3, evenly distributed) |
| `--post-to-jira-dry-run` | Preview the Jira comment without sending |
