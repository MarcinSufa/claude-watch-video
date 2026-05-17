"""MCP server wrapper for the watch-video skill.

A thin adapter that exposes the existing watch-video CLI scripts as MCP
tools. Hosts that speak MCP (Claude Desktop, Codex CLI in MCP mode, Cursor,
Continue.dev, Cline, Windsurf, Zed, VS Code Copilot Tool Mode, ...) can use
it without changes to the underlying Python pipeline.

Design:
  - Each MCP tool is a thin async wrapper around a child-process spawn of
    one of the scripts in ../scripts/. We use asyncio.create_subprocess_exec
    which passes argv as a list (no shell interpretation, injection-safe).
  - Workdirs are passed by path between tools; the server is stateless.
  - post_to_jira requires explicit `confirm=True` to write anything. Without
    confirm, it runs in dry-run and returns the planned-uploads preview --
    preserves the "no unsolicited Jira writes" invariant in MCP contexts
    where there is no TTY for interactive confirmation.

Resolving the scripts directory:
  - WATCH_VIDEO_SCRIPTS_DIR env var takes precedence.
  - Otherwise, look for ../scripts/ relative to this file.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


# ---- Locate the watch-video scripts directory ----------------------------

def _resolve_scripts_dir() -> Path:
    env_override = os.environ.get("WATCH_VIDEO_SCRIPTS_DIR")
    if env_override:
        p = Path(env_override).expanduser().resolve()
        if not p.is_dir():
            raise RuntimeError(
                f"WATCH_VIDEO_SCRIPTS_DIR points to non-existent dir: {p}")
        return p
    candidate = (Path(__file__).parent.parent / "scripts").resolve()
    if not candidate.is_dir():
        raise RuntimeError(
            f"Could not find scripts dir at {candidate}. Set "
            f"WATCH_VIDEO_SCRIPTS_DIR to override.")
    return candidate


SCRIPTS_DIR = _resolve_scripts_dir()


# ---- Subprocess helper (injection-safe: argv list, no shell) -------------

async def _spawn_script(
    script: str,
    *args: str,
    ctx: "Context | None" = None,
) -> tuple[int, str, str]:
    """Spawn scripts/<script> with the given args. Argv-list invocation, no
    shell interpretation (injection-safe). Returns (rc, stdout, stderr).

    Reads stdout and stderr concurrently via asyncio.gather. communicate()
    buffers both pipes until the child exits, which deadlocks on Windows when
    either pipe fills (default buffer size is small, and the pipeline emits
    one JSON event per step on stderr). Concurrent draining keeps both pipes
    free at the SUBPROCESS layer.

    ``ctx`` is accepted as a parameter for forward compatibility but is no
    longer used for per-event progress notifications -- doing that introduced
    a *second* pipe-buffer deadlock at the SERVER-TO-HOST layer (Claude
    Desktop doesn't drain the MCP server's stdout while a tool call is in
    flight; await ctx.info(...) blocks waiting for buffer space, the pump
    task hangs, gather() never resolves, the tool call never returns). The
    proper fix is the v2.1.0 polling pattern; see ROADMAP.md.
    """
    argv = [sys.executable, str(SCRIPTS_DIR / script), *args]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    # NOTE: live MCP notifications (ctx.report_progress / ctx.info) were tried
    # but deadlocked the call. The host doesn't drain the MCP server's stdout
    # while awaiting a tool result -- the OS pipe buffer fills after ~10-30
    # notifications, then `await ctx.info(...)` blocks forever waiting for
    # buffer space, which blocks the stderr pump, which blocks the child's
    # stderr writes, which hangs the entire pipeline.
    #
    # The pump itself is still required: without concurrent stdout+stderr
    # draining the child deadlocks the same way at the OS-pipe layer (this
    # is the original v2.0.0 communicate() bug). We drain silently here and
    # return the full log in stderr so the caller can inspect it after.
    async def _pump(stream, sink: list[str]) -> None:
        """Drain a stream line-by-line into sink. Silent; no host notifications."""
        if stream is None:
            return
        async for raw in stream:
            sink.append(raw.decode("utf-8", errors="replace"))

    await asyncio.gather(
        _pump(proc.stdout, stdout_chunks),
        _pump(proc.stderr, stderr_chunks),
        proc.wait(),
    )

    return (
        proc.returncode if proc.returncode is not None else -1,
        "".join(stdout_chunks),
        "".join(stderr_chunks),
    )


def _format_child_error(rc: int, stderr: str, script: str) -> str:
    tail = "\n".join(stderr.strip().splitlines()[-20:])
    return f"{script} exited with code {rc}. Last stderr lines:\n{tail}"


def _extract_final_json(stdout: str) -> str:
    """Find the final JSON object in subprocess stdout.

    The CLI scripts print their final result as JSON on stdout. watch_video.py
    uses indent=2 (multi-line pretty-print); highlights.py and post_to_jira.py
    use compact single-line JSON. The naive `splitlines()[-1]` only works for
    the single-line case -- multi-line JSON's last line is just '}'.

    Strategy: try parsing the whole stripped stdout. If that succeeds, return
    it (handles pretty-printed). If not, walk backwards looking for the last
    line that parses on its own (handles single-line + miscellaneous prefix
    noise). Default to '{}' when there's no JSON at all.
    """
    stripped = stdout.strip()
    if not stripped:
        return "{}"
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
            return line
        except json.JSONDecodeError:
            continue
    return "{}"


# ---- MCP server + tools --------------------------------------------------

mcp = FastMCP("watch-video")


@mcp.tool()
async def watch_video(
    input_ref: str,
    workdir: str | None = None,
    dedup: bool = True,
    ocr: bool = False,
    whisper: str = "auto",
    start: str | None = None,
    end: str | None = None,
    no_html: bool = False,
    no_docx: bool = False,
    ctx: Context | None = None,
) -> str:
    """Run the watch-video pipeline on an input.

    Args:
        input_ref: A local path, a public URL (YouTube, Loom, etc.), a Jira
            issue key like 'PROJ-1234', or the literal 'auto' to grab the
            newest video from ~/Downloads.
        workdir: Output directory. If omitted, the pipeline picks a default
            under c:\\tmp\\watch-<slug>\\ (Windows) or equivalent.
        dedup: Run smart perceptual-hash dedup with transcript-aware
            protection. Default true.
        ocr: Run Tesseract OCR on kept frames (useful for screen recordings).
            Default false.
        whisper: Transcription source. 'auto' (default) prefers VTT captions
            when yt-dlp pulled one (free, fast), else local faster-whisper.
            Other values: 'captions', 'local', 'groq', 'openai'.
        start: Optional window start, e.g. '2:30'.
        end: Optional window end, e.g. '3:00'.
        no_html: Skip report.html.
        no_docx: Skip report.docx.

    Returns:
        JSON string with the meta.json contents (workdir path, video meta,
        transcript summary, report paths, elapsed seconds). Pass the workdir
        path to read_transcript / read_report / read_highlights / post_to_jira
        in follow-up tool calls.
    """
    args = [input_ref]
    if workdir:
        args += ["--workdir", workdir]
    if dedup:
        args.append("--dedup")
    if ocr:
        args.append("--ocr")
    if whisper and whisper != "auto":
        args += ["--whisper", whisper]
    if start:
        args += ["--start", start]
    if end:
        args += ["--end", end]
    if no_html:
        args.append("--no-html")
    if no_docx:
        args.append("--no-docx")

    rc, stdout, stderr = await _spawn_script("watch_video.py", *args, ctx=ctx)
    if rc != 0:
        raise RuntimeError(_format_child_error(rc, stderr, "watch_video.py"))
    return _extract_final_json(stdout)


@mcp.tool()
async def read_transcript(workdir: str) -> str:
    """Read transcript.md from a watch-video workdir.

    Args:
        workdir: The workdir path returned by watch_video.

    Returns:
        The full transcript text (prose paragraphs with MM:SS markers).
    """
    p = Path(workdir).expanduser() / "transcript.md"
    if not p.is_file():
        raise RuntimeError(f"transcript.md not found at {p}")
    return p.read_text(encoding="utf-8")


@mcp.tool()
async def read_report(workdir: str, fmt: str = "md") -> str:
    """Read the report from a watch-video workdir.

    Args:
        workdir: The workdir path returned by watch_video.
        fmt: 'md' (default), 'html', or 'docx-path'. Markdown returns the
            text. 'html' returns the full self-contained HTML. 'docx-path'
            returns the filesystem path to report.docx (the binary file is
            not embedded in the MCP response).

    Returns:
        Text content (md / html) or absolute file path (docx-path).
    """
    wd = Path(workdir).expanduser()
    if fmt == "md":
        p = wd / "report.md"
    elif fmt == "html":
        p = wd / "report.html"
    elif fmt == "docx-path":
        p = wd / "report.docx"
        if not p.is_file():
            raise RuntimeError(f"report.docx not found at {p}")
        return str(p)
    else:
        raise RuntimeError(f"unknown fmt: {fmt} (expected md|html|docx-path)")
    if not p.is_file():
        raise RuntimeError(f"{p.name} not found at {p}")
    return p.read_text(encoding="utf-8")


@mcp.tool()
async def read_highlights(workdir: str) -> str:
    """Read highlights.json from a watch-video workdir.

    Args:
        workdir: The workdir path returned by watch_video.

    Returns:
        JSON string with prompt, provider, model, max_n, and the validated
        highlights list. Each highlight has 'timestamp' and 'reason'.
    """
    p = Path(workdir).expanduser() / "highlights.json"
    if not p.is_file():
        raise RuntimeError(
            f"highlights.json not found at {p}. Run pick_highlights first.")
    return p.read_text(encoding="utf-8")


@mcp.tool()
async def pick_highlights(
    workdir: str,
    prompt: str,
    max_n: int = 5,
    provider: str = "anthropic",
    model: str | None = None,
    ctx: Context | None = None,
) -> str:
    """LLM-driven moment selection over the transcript.

    Args:
        workdir: The workdir path returned by watch_video.
        prompt: What to look for, e.g. 'identify the bug and the moment it
            occurs' or 'summarize the rate decision and inflation outlook'.
        max_n: Maximum number of moments to return. Default 5.
        provider: 'anthropic' (default), 'openai', or 'groq'. Reads API key
            from the corresponding env var or ~/.watch-video/credentials.json.
        model: Optional model id; falls back to per-provider default.

    Returns:
        JSON string with the highlights result (prompt, provider, model,
        elapsed_seconds, tokens, highlights list, output paths).
    """
    args = [
        workdir, "--prompt", prompt,
        "--max-n", str(max_n),
        "--provider", provider,
    ]
    if model:
        args += ["--model", model]
    rc, stdout, stderr = await _spawn_script("highlights.py", *args, ctx=ctx)
    if rc != 0:
        raise RuntimeError(_format_child_error(rc, stderr, "highlights.py"))
    return _extract_final_json(stdout)


@mcp.tool()
async def post_to_jira(
    workdir: str,
    confirm: bool = False,
    jira_key: str | None = None,
    style: str = "collapsed",
    summary_key_frames: int | None = None,
    force: bool = False,
    ctx: Context | None = None,
) -> str:
    """Post the report.md back to its source Jira issue.

    SAFETY CONTRACT: Without `confirm=True`, this runs in dry-run mode and
    returns the planned-uploads preview WITHOUT writing to Jira. To actually
    post, the caller must explicitly pass confirm=True for this specific
    invocation. MCP hosts MUST surface this to the user and require a yes
    before passing confirm=True. This matches the no-unsolicited-Jira-writes
    rule baked into the CLI's interactive prompt.

    Args:
        workdir: The workdir path returned by watch_video.
        confirm: REQUIRED to actually post. Default false (dry-run preview
            only). The MCP host should treat confirm=True as a privileged
            action and only set it after explicit user authorization.
        jira_key: Override the target issue (default: the issue the workdir
            was fetched from).
        style: 'collapsed' (default), 'inline', or 'summary'.
        summary_key_frames: For style='summary', how many key moments to
            include. Default 3.
        force: Bypass the idempotency check (use only if you intentionally
            want to post a duplicate /watch-video comment).

    Returns:
        JSON with the post result (collapsed/inline/summary structure,
        issue_key, comment_id if posted, planned uploads if dry-run).
    """
    args = [workdir, "--style", style]
    if jira_key:
        args += ["--jira-key", jira_key]
    if summary_key_frames is not None:
        args += ["--summary-key-frames", str(summary_key_frames)]
    if force:
        args.append("--force")

    if confirm:
        # User authorized this specific post. --yes skips the interactive
        # prompt; the real post happens.
        args.append("--yes")
    else:
        # No confirmation -- run as dry-run so the caller sees the planned
        # uploads + body preview without writing anything.
        args.append("--dry-run")
        # --yes is also needed in dry-run to avoid the TTY check.
        args.append("--yes")

    rc, stdout, stderr = await _spawn_script("post_to_jira.py", *args, ctx=ctx)
    if rc != 0:
        raise RuntimeError(_format_child_error(rc, stderr, "post_to_jira.py"))
    final_json = _extract_final_json(stdout)
    try:
        parsed = json.loads(final_json)
    except json.JSONDecodeError:
        parsed = {"raw_output": final_json}
    parsed["_mcp_confirmed"] = confirm
    parsed["_mcp_safety_note"] = (
        "Real post executed -- confirm=True was set." if confirm
        else "Dry-run only. Caller must set confirm=True (after explicit "
             "user authorization) to actually write to Jira."
    )
    return json.dumps(parsed, indent=2)


# ---- Optional: expose the workdir's meta.json as an MCP resource so hosts
# that prefer resource-style access (e.g., file listings) can browse a
# completed run. This is read-only; tools above do the work.

@mcp.resource("workdir://{path}/meta.json")
async def workdir_meta(path: str) -> str:
    """Return meta.json for a watch-video workdir as a readable resource."""
    p = Path(path).expanduser() / "meta.json"
    if not p.is_file():
        raise RuntimeError(f"meta.json not found at {p}")
    return p.read_text(encoding="utf-8")


def main() -> None:
    """Entry point for `python -m claude_watch_video_mcp` or direct invocation."""
    mcp.run()


if __name__ == "__main__":
    main()
