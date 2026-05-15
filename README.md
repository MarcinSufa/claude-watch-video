# claude-watch-video

Give Claude eyes and ears for any video — local file, public URL, or Jira attachment.

This is both a [Claude Code skill](https://support.claude.com/en/articles/12512176-what-are-skills) (`SKILL.md`) and a [Claude Code plugin](https://docs.claude.com/en/docs/claude-code/plugins) (`.claude-plugin/plugin.json`). Same files, two installation paths.

## What it does

1. **Resolves the input** — local path, public URL (via `yt-dlp`), Jira issue key (via Atlassian REST API), or "auto-pick latest in `~/Downloads/`".
2. **Probes** the video for duration / dimensions / audio presence / silence.
3. **Extracts keyframes** with `ffmpeg` — uniform sampling by default, optional scene-change detection with fallback.
4. **Transcribes audio** with the chosen Whisper provider (`local` faster-whisper offline, `groq` hosted, or `openai` hosted). Timestamps offset to match the original video timeline when `--start`/`--end` is used.
5. **Smart dedup (optional)** — drops near-duplicate frames via perceptual hash while protecting frames near transcript-paragraph timestamps. Typical ~50% token reduction with no loss of narrated content.
6. **Writes a `report.md` evidence bundle** — transcript paragraphs interleaved with embedded frame thumbnails. Paste-ready for Jira / PR descriptions.
7. **Emits `meta.json`** — durable, versioned schema describing every artifact.

Claude (or whichever agent picked up the skill) then `Read`s the frames as multimodal images plus the transcript, and answers grounded in what was on screen and what was said.

## Quick start

### As a skill (Claude Code, manual install)

```bash
git clone https://github.com/MarcinSufa/claude-watch-video.git ~/.claude/skills/watch-video
```

Restart Claude Code; the skill auto-loads. Then:

```
/watch-video PROJ-1234
```

(Triggers SKILL.md's procedure — the orchestrator downloads the Jira attachment, extracts frames, transcribes, generates the report.)

### As a plugin (Claude Code marketplace, once published)

```
/plugin marketplace add MarcinSufa/claude-watch-video
/plugin install watch-video@claude-watch-video
```

### Direct CLI use

```bash
python ~/.claude/skills/watch-video/scripts/watch_video.py <input> [flags]
```

Where `<input>` can be:
- `c:\path\video.mp4` — a local file
- `https://www.youtube.com/watch?v=...` — any public URL `yt-dlp` supports
- `PROJ-1234` — a Jira issue key (needs API token configured)
- `https://yoursite.atlassian.net/browse/PROJ-1234` — same as above, URL form
- `auto` — pick the most recently modified video in `~/Downloads/`

## Setup

### Prerequisites (one-time)

- `ffmpeg` + `ffprobe` on PATH (Windows: `winget install Gyan.FFmpeg`)
- Python 3.10+
- `pip install --user faster-whisper`

### Optional, per feature

| Feature | Install |
|---|---|
| URL mode (YouTube/Loom/etc.) | `pip install --user yt-dlp` |
| Frame dedup (`--dedup`) | `pip install --user Pillow imagehash` |
| Jira full-auto download | Atlassian API token — see SKILL.md → "Jira token setup" |
| Hosted Whisper (Groq) | Get a key at https://console.groq.com/keys |
| Hosted Whisper (OpenAI) | Get a key at https://platform.openai.com/api-keys |

See [`SKILL.md`](SKILL.md) for full documentation including credential file layout, flag reference, exit codes, and `meta.json` schema.

## Examples

```bash
# Most common: full-auto Jira workflow
python scripts/watch_video.py PROJ-1234

# Scope to a 10-second window
python scripts/watch_video.py PROJ-1234 --start 0:30 --end 0:40

# Fastest hosted transcription
python scripts/watch_video.py PROJ-1234 --whisper groq

# Maximum token efficiency with transcript-aware dedup
python scripts/watch_video.py PROJ-1234 --dedup

# YouTube clip with bumped resolution for tiny on-screen text
python scripts/watch_video.py https://youtu.be/abc --resolution 1280
```

## Why this exists

Most public video-watching skills handle URL downloads well but can't authenticate to Jira and don't produce a paste-ready evidence bundle. This skill closes both gaps:

- **Jira-native** — Atlassian REST API + range-request CDN handling for reliable downloads of multi-MB screen recordings.
- **Evidence-bundle output** — `report.md` ready to paste back into the ticket as a comment.

Built for bug-triage workflows where the input is "watch PROJ-1234 and tell me what broke" and the desired output is a structured analysis Claude can produce in seconds.

## Engineering notes

- **Cross-platform** — pure Python; no PowerShell.
- **Structured stderr events** — every sub-script emits JSON-line progress events so the orchestrator can stream status reliably.
- **Atomic writes** — every output (download, frames dir, audio.wav, transcripts, meta.json, report.md) stages to `.partial-<uuid>` and renames on success. Failures don't leave half-written artifacts.
- **Deterministic exit codes** — `2` bad input, `3` missing dep, `4` auth fail, `5` ambiguous (e.g., multiple Jira attachments), `6` IO, `7` timeout.
- **Schema-versioned `meta.json`** — durable contract for downstream tooling.
- **Self-contained smoke test** — `scripts/smoketest.py` generates a synthetic 5-second video and validates the full pipeline. Suitable for CI.

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgements

- [`ffmpeg`](https://ffmpeg.org/) for media processing
- [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) for local transcription
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) for URL download support
- [`Pillow`](https://python-pillow.org/) + [`imagehash`](https://github.com/JohannesBuchner/imagehash) for perceptual hashing
- [Groq](https://groq.com/) and [OpenAI](https://openai.com/) Whisper APIs
- The competitive landscape that informed the design — particularly [`bradautomates/claude-video`](https://github.com/bradautomates/claude-video), which set a high bar for Claude Code video skills.
