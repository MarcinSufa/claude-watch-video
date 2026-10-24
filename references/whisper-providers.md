# Whisper providers, models and speaker relabeling

Read when picking a non-default `--whisper` provider or `--model`, setting up a hosted key, or when the transcript has `**S0**`/`**S1**` speaker tags to relabel.

## Which provider to pass

**Recommended order for the agent to choose `--whisper`** *(the orchestrator does NOT auto-select hosted providers from env vars — `--whisper auto` only picks `captions` for URLs with VTT subtitles, otherwise `local`. The agent inspects env vars and passes an explicit `--whisper <name>` flag.)*

1. Multi-speaker content (podcast / interview / meeting):
   - `DEEPGRAM_API_KEY` set → `--whisper deepgram` (hosted, ~$0.0043/min)
   - Else `HF_TOKEN` set with pyannote terms accepted → `--whisper whisperx` (free, local)
   - Else fall through to the single-speaker order below + warn the user diarization is unavailable
2. Single-speaker, want fastest hosted transcription:
   - `GROQ_API_KEY` set → `--whisper groq` (~5 s on a typical clip)
   - Else `OPENAI_API_KEY` set → `--whisper openai`
3. No hosted keys available → `--whisper auto` (default; picks captions if URL has VTT, otherwise local faster-whisper offline)

## Whisper providers

`watch-video` supports six transcription sources via `--whisper` (default `auto` = `captions` when a URL came with a VTT, else `local`):

| Provider | Latency | Cost | Privacy | API key |
|---|---|---|---|---|
| `captions` | Instant (VTT subtitles yt-dlp already downloaded) | Free | Nothing sent | None (URL inputs with captions only) |
| `local` (default for files) | Cold start ~25 s (one-time model download) + ~0.1 s per second of audio. Cached after first run. | Free | Offline (audio never leaves your machine) | None |
| `groq` | ~1-2 s for any short clip, often faster than real time | Free tier covers ~14400 s/day; paid is very cheap | Audio sent to Groq servers (deleted per their policy) | `GROQ_API_KEY` |
| `openai` | ~3-5 s per minute of audio | $0.006 / minute | Audio sent to OpenAI (deleted per their policy) | `OPENAI_API_KEY` |
| `deepgram` | Hosted (Nova-3) + speaker diarization | ~$0.0043 / minute | Audio sent to Deepgram | `DEEPGRAM_API_KEY` |
| `whisperx` | Local Whisper + pyannote.audio diarization; first run downloads ~500 MB-1 GB of weights | Free | Offline | HF token + accepted terms on three gated pyannote repos |

**When to choose what:**

- **`captions`** — picked automatically for URLs that come with subtitles; nothing to decide.
- **Default `local`** — works out of the box, no setup, offline. Best for repeated runs against the same video (cache hit). Slower on cold start.
- **`groq`** — best balance of speed and price. Use when you want fast iteration on many videos. Free tier is generous.
- **`openai`** — choose only if you already have an OpenAI key and prefer their stack. Slower and pricier than Groq for the same task.
- **`deepgram` / `whisperx`** — multi-speaker content; both tag paragraphs `**S0**`/`**S1**` and write `speakers.json` (see [Relabel speakers](#relabel-speakers-after-deepgram--whisperx) below).

### Setting up a hosted provider (one-time, ~2 minutes)

1. Get a key:
   - Groq: <https://console.groq.com/keys> (free signup, free tier covers most personal usage)
   - OpenAI: <https://platform.openai.com/api-keys> (paid; needs billing set up)

2. Choose how to provide it. The skill checks these in order:

   **a) Environment variable (recommended for CI / shared dev environments):**

   ```bash
   # in ~/.bashrc or ~/.zshrc
   export GROQ_API_KEY='gsk_...'
   # or
   export OPENAI_API_KEY='sk-...'
   ```

   Windows (PowerShell): `[Environment]::SetEnvironmentVariable('GROQ_API_KEY', 'gsk_...', 'User')`

   **b) Credentials file at `~/.watch-video/credentials.json` (recommended for desktop, ACL-lockable):**

   ```json
   {
     "groq_api_key": "gsk_...",
     "openai_api_key": "sk-..."
   }
   ```

   Lock it down just like the Atlassian credentials:

   ```bash
   mkdir -p ~/.watch-video && chmod 700 ~/.watch-video
   # ... write the JSON via your editor, then:
   chmod 600 ~/.watch-video/credentials.json
   ```

   Windows (PowerShell): `icacls "$env:USERPROFILE\.watch-video\credentials.json" /inheritance:r /grant:r "${env:USERNAME}:R"`

   **c) One-shot `--whisper-api-key <KEY>` flag** — for ad-hoc use, not persisted.

3. Verify with a tiny local clip (no Jira needed):

   ```bash
   ffmpeg -loglevel error -f lavfi -i testsrc=duration=5:size=320x240 \
       -f lavfi -i sine=frequency=440:duration=5 -shortest /tmp/wv-check.mp4
   python3 "${CLAUDE_SKILL_DIR}/scripts/watch_video.py" /tmp/wv-check.mp4 \
       --whisper groq --no-report --workdir /tmp/wv-check
   ```

   Exit code is 0 either way. Success: `transcript.provider` is `groq`. Auth failure: `transcript` is null, `skipped_audio_reason` says `transcription failed (groq): ...` and `warnings` has a `transcribe` entry with `exit_code: 4` and the setup options.

### Security notes (mirror of the Atlassian token guidance)

- The credentials file is plaintext; ACL-lock it.
- Don't commit, screenshot, or paste keys into shared chats. (Same risk as Atlassian.)
- Both providers' keys never expire automatically -- rotate yearly.
- If a key leaks: revoke at the provider's console URL above, generate a new one.

### How the skill picks a model

Default model depends on provider:

- `local --lang en` → `small.en` (English-only, ~5% better accuracy on English)
- `local --lang auto` or other → `small` (multilingual, 99 languages)
- `groq` → `whisper-large-v3` (large-v3 is Groq's free-tier default and very fast)
- `openai` → `whisper-1` (only option)

Override with `--model NAME` -- the skill passes it through to the chosen provider unchanged.

## Relabel speakers after deepgram / whisperx

**Relabel anonymous speakers (multi-speaker content only).** When the run used a diarizing provider — `--whisper deepgram` (hosted, ~$0.0043/min) or `--whisper whisperx` (local + offline, requires `pip install pyannote.audio` + HF token + accepting terms on three gated pyannote repos: `speaker-diarization-3.1`, `segmentation-3.0`, and `speaker-diarization-community-1`) — the transcript has speakers tagged `**S0**`, `**S1**`, … and a `speakers.json` file lists each speaker's first utterance + airtime stats. The `speakers.json` schema is identical for both providers, so the relabel flow below is provider-agnostic. Before answering the user, infer real names from context and rewrite the transcript so the answer reads naturally (`**Joe Rogan**` instead of `**S0**`).

Procedure:

```bash
# a. Read speakers.json -- each entry has id + first_utterance_text + segment_count.
cat <workdir>/speakers.json

# b. Infer names from the first utterances:
#    - "Welcome to the show, I'm Joe..."   -> S0 is Joe
#    - "Thanks for having me, Joe..."      -> S1 is the guest; the host's
#                                             intro often names them too
#    - Screen-recording with OCR: the name overlay near speaker-change
#      timestamps is the ground truth (look at ocr.txt if --ocr was run).
#
# c. If the inference is uncertain (e.g. ambiguous intros, three+ speakers
#    without clear self-intros), surface the speakers.json content to the
#    user and ask: "I see 3 speakers. From the openings these look like
#    Alice, Bob, and Carol -- correct, or should I assign differently?"
#
# d. Rewrite atomically in place. Same path convention as
#    watch_video.py in SKILL.md: ${CLAUDE_SKILL_DIR} is the skill's
#    directory for both the plugin and a manual install.
#    Comma form for simple names:
python3 "${CLAUDE_SKILL_DIR}/scripts/relabel_speakers.py" \
    <workdir> --names "S0=Joe Rogan,S1=Naval Ravikant"
# JSON form when names contain commas (e.g. "Smith, Jr."):
python3 "${CLAUDE_SKILL_DIR}/scripts/relabel_speakers.py" \
    <workdir> --names-json '{"S0":"Joe Rogan","S1":"Naval Ravikant"}'
```

The script rewrites `transcript.md`, `transcript.txt`, and `speakers.json` (adding a `name` field while preserving `id`). If `report.md` / `.html` / `.docx` already exist in the workdir, the script auto-regenerates them so they pick up the new names too. Typical wall-clock: ~0.1 second.

**When to skip relabel:**
- Single-speaker content (only S0 detected) -- the inline tag is redundant; user gains nothing.
- User explicitly asked for raw transcript output without naming.
- You can't confidently infer names AND the user hasn't supplied them. Better to leave `S0` / `S1` than to mislabel.
