# Configuration reference

Full reference for `scripts/watch_video.py` (the CLI orchestrator). Every flag, organized by what it affects. For per-script details (each individual step's flags) see [`SKILL.md`](../SKILL.md).

## Workdir + scope

| Flag | Purpose |
|---|---|
| `--workdir <PATH>` | Override the default workdir. Default uses a fixed root per OS — `c:\tmp\watch-<slug>\` on Windows, `/tmp/watch-<slug>/` on macOS/Linux. (Note: fixed paths, not the system temp dir like `%TEMP%` / `TMPDIR`.) Pass `--workdir` to put outputs anywhere else. |
| `--start MM:SS --end MM:SS` | Scope to a window; transcript timestamps stay original-video-time |
| `--no-cache` | Bypass the per-step output cache |
| `--force-step NAME[,...]` | Force a specific step (downstream auto-invalidates) |
| `--verbose, -v` | Print human-readable progress lines to stderr in addition to the JSON event stream |

## Input selection

| Flag | Purpose |
|---|---|
| `--since-seconds N` | (`auto` input mode only) max age in seconds of the file picked from `~/Downloads/`. Default 300. |

## Frame extraction

| Flag | Purpose |
|---|---|
| `--resolution W` | Frame width in px (default 960; use 1280 for dense UI text) |
| `--frames N` | Override default frame budget (auto by duration) |
| `--scene-mode` | Extract frames at scene cuts (auto-fallback to uniform when no scenes) |
| `--scene-threshold X` | ffmpeg scene-change sensitivity (0.1 = very sensitive, 0.5 = only major cuts). Default 0.3. |
| `--dedup` | Smart pHash dedup with transcript-aware protection |
| `--dedup-threshold N` | pHash Hamming threshold (default 5) |
| `--dedup-min-interval SECONDS` | Minimum seconds between consecutive kept frames (default 5.0) |
| `--dedup-protect-window SECONDS` | Seconds around transcript paragraph timestamps where frames are protected from dropping (default 1.5) |

## Transcription

| Flag | Purpose |
|---|---|
| `--whisper auto\|captions\|local\|groq\|openai\|deepgram\|whisperx` | Source. `auto`: VTT captions if yt-dlp pulled one (free), else `local` faster-whisper. Both `deepgram` (hosted, ~$0.0043/min) and `whisperx` (local + offline; needs `pip install whisperx` + HF token + accepting pyannote terms) add speaker diarization (write `speakers.json`, tag transcript paragraphs with `**S0**` / `**S1**`). The schema is identical for both → `relabel_speakers.py` works against either. |
| `--model NAME` | Whisper model id. For `whisperx`, the same Whisper IDs as `local` (e.g. `small.en`, `medium`, `large-v3`). |
| `--lang en\|pl\|...\|auto` | Audio language |
| `--no-audio` | Skip transcription |
| `--whisper-api-key KEY` | API key or HF token for hosted/whisperx providers (env vars also work: `GROQ_API_KEY`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `HF_TOKEN`) |
| `--whisper-credentials PATH` | JSON file path for provider keys/tokens (fields `groq_api_key`, `openai_api_key`, `deepgram_api_key`, `hf_token`) |

## OCR

| Flag | Purpose |
|---|---|
| `--ocr` | Run Tesseract OCR over kept frames |
| `--ocr-lang LANGS` | Tesseract language(s), e.g. `eng`, `pol`, `eng+pol`. Default `eng`. |
| `--ocr-min-text-length N` | Minimum extracted text length per frame to keep that frame's OCR row (filters out noise from frames with no real text) |

## Highlights (LLM-driven moment selection)

| Flag | Purpose |
|---|---|
| `--highlights-prompt "..."` | Enable LLM highlight selection (requires an API key for the chosen provider) |
| `--highlights-max-n N` | Max number of highlights the LLM is allowed to pick (default 5) |
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
| `--post-to-jira-yes` | Skip the interactive confirmation prompt. Use only in non-interactive contexts (CI, automation) where `--post-to-jira` was already explicitly set. |
| `--post-to-jira-no-embed-images` | Post the comment without uploading frame thumbnails as Jira attachments (smaller comment, no images) |

## Sibling scripts

`watch_video.py` is the orchestrator. Some workflows reach for sibling scripts after the orchestrator finishes:

### `relabel_speakers.py` (v2.3.1+)

Rewrites anonymous `**S0**` / `**S1**` speaker tags from a `--whisper deepgram` run with real names. Atomic, in place; auto-regenerates `report.md` / `.html` / `.docx` if they exist in the workdir. Typical wall-clock: ~0.1 second.

```bash
python scripts/relabel_speakers.py <workdir> --names "S0=Joe,S1=Naval"
# Or, for names with commas:
python scripts/relabel_speakers.py <workdir> --names-json '{"S0":"Smith, Jr."}'
```

| Flag | Purpose |
|---|---|
| `<workdir>` (positional) | Workdir produced by `watch_video.py --whisper deepgram`. Must contain `speakers.json`. |
| `--names "S0=Joe,S1=Naval"` | Comma-separated speaker map. Simple form; use `--names-json` if any name contains a comma. |
| `--names-json '{"S0":"Joe"}'` | Full JSON object form. Wins if both `--names` and `--names-json` are given. |
