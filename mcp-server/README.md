# claude-watch-video-mcp

> **MCP server wrapper for the [watch-video skill](https://github.com/MarcinSufa/claude-watch-video).** Make the same pipeline available in Claude Desktop, Codex CLI (MCP mode), Cursor, Continue.dev, Cline, Windsurf, Zed, VS Code Copilot Chat, and any other MCP-speaking host.

The CLI scripts in [`../scripts/`](../scripts/) remain the canonical implementation. This package is a thin async wrapper around them — exposes ~5 MCP tools, no business logic duplication.

---

## Tools exposed

| Tool | What it does | When to use |
|---|---|---|
| `watch_video(input_ref, ...)` | Run the full pipeline: download → frames → transcribe → dedup → OCR → report. | Starting point. Returns the workdir path; pass it to the other tools. |
| `read_transcript(workdir)` | Returns `transcript.md` content. | When you want just the narration. |
| `read_report(workdir, fmt)` | Returns `report.md` / `report.html` / path to `report.docx`. | When you want the full evidence bundle. |
| `pick_highlights(workdir, prompt, ...)` | LLM-driven moment selection with a user prompt + multi-provider (Anthropic / OpenAI / Groq). | When you want "give me only the X parts." |
| `read_highlights(workdir)` | Returns `highlights.json`. | After `pick_highlights` ran. |
| `post_to_jira(workdir, confirm=...)` | Posts the report to the source Jira issue. **`confirm=False` runs in dry-run; `confirm=True` writes.** | Bug-triage workflows. |

Plus one MCP **resource**: `workdir://<path>/meta.json` — read-only access to the workdir's metadata, for hosts that prefer the resource-browser pattern.

---

## Safety contract for `post_to_jira`

This wrapper preserves the "no unsolicited Jira writes" invariant from the CLI:

```text
post_to_jira(workdir, confirm=False)   →   dry-run preview only, nothing written
post_to_jira(workdir, confirm=True)    →   real post (after the HOST has asked the user)
```

MCP hosts MUST surface the planned action to the user and obtain authorization **before** setting `confirm=True`. Treat `confirm=True` as a privileged action.

---

## Install

### From the repo (recommended while in beta)

```bash
git clone https://github.com/MarcinSufa/claude-watch-video
cd claude-watch-video/mcp-server
pip install -e .

# Optionally install everything the underlying CLI needs:
pip install -e ".[full]"
```

After install, the `claude-watch-video-mcp` command is on your PATH.

### Underlying CLI prerequisites

The MCP server delegates to [`../scripts/watch_video.py`](../scripts/watch_video.py), which needs:

| Required | For |
|---|---|
| `ffmpeg` + `ffprobe` | frame extraction, audio extraction, probing |
| `faster-whisper` | local Whisper transcription (the default fallback) |

Optional, unlocks features:

| Optional | Unlocks |
|---|---|
| `yt-dlp` | URL input mode |
| `Pillow` + `imagehash` | smart dedup |
| `pytesseract` + Tesseract binary | OCR on frames |
| `anthropic` / `openai` Python SDK | LLM highlights |
| `python-docx` | `report.docx` (gracefully skipped if missing) |
| Atlassian API token at `~/.atlassian-token/credentials.json` | Jira fetch + post |

See the main [README](../README.md#prerequisites) for install commands per platform.

---

## Configure your MCP host

### Claude Desktop

Edit `~/.claude.json` (or the platform-equivalent `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "watch-video": {
      "command": "claude-watch-video-mcp"
    }
  }
}
```

Or, without installing the package, point at the script directly:

```json
{
  "mcpServers": {
    "watch-video": {
      "command": "python",
      "args": ["/absolute/path/to/claude-watch-video/mcp-server/server.py"]
    }
  }
}
```

Restart Claude Desktop. The tools should appear in the tool picker.

### Codex CLI

```bash
codex mcp add watch-video --command claude-watch-video-mcp
```

Or edit `~/.codex/config.toml`:

```toml
[mcp_servers.watch-video]
command = "claude-watch-video-mcp"
```

### Cursor / Continue.dev / Cline / Windsurf / Zed / VS Code Copilot Chat

Each host has its own MCP-server registration UI; all share the same shape: a `command` and optional `args`. Point them at `claude-watch-video-mcp` (or the absolute path to `server.py`) and they pick up the tools automatically.

---

## Pointing at a non-default scripts directory

If you've installed the MCP server from PyPI but the CLI scripts live elsewhere on disk, set:

```bash
export WATCH_VIDEO_SCRIPTS_DIR=/path/to/claude-watch-video/scripts
```

The server resolves the scripts dir at startup via this env var, falling back to `<server-dir>/../scripts/` if unset.

---

## Example session

```text
USER: Watch https://www.youtube.com/watch?v=O664gH_szoY and tell me what's new.

HOST → MCP: watch_video(input_ref="https://www.youtube.com/watch?v=O664gH_szoY")
MCP → HOST: { "workdir": "C:/tmp/watch-O664gH_szoY", "elapsed_seconds": 3.82,
             "transcript": { "provider": "captions", "segments": 19 }, ... }

HOST → MCP: pick_highlights(workdir="C:/tmp/watch-O664gH_szoY",
                            prompt="what is new in this Claude release")
MCP → HOST: { "highlights": [
    { "timestamp": "00:08", "reason": "..." },
    ...
] }

HOST (to user): Here are the 5 most relevant moments from the video...
```

For a Jira flow:

```text
USER: Triage CON-8970 -- watch the attached video and identify the bug.

HOST → MCP: watch_video(input_ref="CON-8970", dedup=True, ocr=True)
MCP → HOST: { "workdir": "C:/tmp/watch-con-8970", ... }

HOST → MCP: pick_highlights(workdir="...", prompt="identify the bug and the moment it occurs")
MCP → HOST: { "highlights": [...] }

HOST (to user): Here is the bug analysis. Would you like me to post the
                report back to CON-8970?
USER: Yes, post it.

HOST → MCP: post_to_jira(workdir="C:/tmp/watch-con-8970", confirm=True)
MCP → HOST: { "issue_key": "CON-8970", "comment_id": "10247", ... }
```

---

## Versioning

The MCP server's version (`v2.0.0`) tracks the parent repo. The CLI scripts in `../scripts/` are the canonical artifact; this package version-locks to whichever scripts are present at install time. Pinning to a specific repo tag (`git checkout v2.0.0` before `pip install -e .`) ensures the script API matches what the server expects.

---

## License

MIT. See [`../LICENSE`](../LICENSE).
