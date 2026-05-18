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
import re
import sys
import uuid
from pathlib import Path
from typing import Any


# Mirrors scripts/watch_video.py's DEFAULT_WORKDIR_ROOT. Used by
# _default_workdir when the caller doesn't specify a workdir. Picking a
# platform-appropriate root matters because Path(r"C:\tmp") is a *relative*
# path on POSIX -- a literal C:\tmp under the server's CWD -- not /tmp.
DEFAULT_WORKDIR_ROOT = Path("c:/tmp") if sys.platform == "win32" else Path("/tmp")

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

    Handles:
    - Compact single-line JSON (highlights.py, post_to_jira.py)
    - Multi-line pretty-printed JSON (watch_video.py with indent=2)
    - Prefix noise: yt-dlp download progress writes to stdout when fetch
      is inlined in MCP mode (v2.1.0 in-proc refactor), and similar noise
      can leak from other in-proc subprocess calls.

    Strategy:
    1. Try parsing the whole stripped stdout (fast path: no noise).
    2. Otherwise locate the last line whose first non-space character is '{'
       (multi-line JSON always starts a new object at the beginning of a
       line under print(json.dumps(..., indent=2))). Parse from there to EOF.
    3. Fall back to walking lines in reverse looking for a complete one-line
       JSON object.
    """
    stripped = stdout.strip()
    if not stripped:
        return "{}"
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass

    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("{"):
            candidate = "\n".join(lines[i:]).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
            return line
        except json.JSONDecodeError:
            continue
    return "{}"


# ---- Job state for the polling pattern (watch_video_start/_status) ------
#
# The synchronous watch_video tool hangs in Claude Desktop on Windows because
# a multi-second tool call fights the host's stdio JSON-RPC drain timing.
# v2.1.0 splits the long-running watch_video into a non-blocking start +
# polling status pair. The host never sees a single multi-second call --
# every tool call returns within ~100ms.
#
# State is written to <workdir>/_mcp_status.json so it survives MCP server
# restarts (Claude Desktop restarts spawn a fresh server process; the file
# is the durable record). The job_id is the workdir path (simple, no
# separate state needed).


import time as _time  # alias to avoid shadowing if 'time' is used elsewhere


_STATUS_FILENAME = "_mcp_status.json"


# Strong refs to fire-and-forget background pipeline tasks. asyncio's event
# loop only keeps WEAK references to tasks, so a bare `asyncio.create_task(...)`
# whose return value is dropped can be garbage-collected mid-flight -- the
# coroutine simply stops, no error, no traceback. That's what caused v2.1.0-rc2
# to leave _mcp_status.json stuck in "running" forever even though the
# subprocess kept running and produced all artifacts: the awaiter (the task
# that calls proc.wait() and then _write_status({"state": "done"})) was GC'd.
# Holding a strong ref in this set, discarded on done, is the canonical fix.
# See https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_background_tasks: set[asyncio.Task] = set()


def _status_path(workdir: str) -> Path:
    return Path(workdir).expanduser() / _STATUS_FILENAME


def _write_status(workdir: str, payload: dict) -> None:
    """Write the status file atomically.

    The status file is rewritten on every state transition and may be read
    concurrently by `watch_video_status` polls. A naive `write_text` leaves
    a window where a reader can observe a half-written file -- `_read_status`
    returns None on the JSONDecodeError, and the running job is mistakenly
    reported as state="unknown". Stage to a uuid-suffixed sibling and
    `os.replace()` for an atomic rename.
    """
    p = _status_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    staging = p.with_name(f"{p.name}.partial-{uuid.uuid4().hex}")
    staging.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(staging, p)


def _read_status(workdir: str) -> dict | None:
    p = _status_path(workdir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


_JIRA_KEY_RE = re.compile(r"^[A-Z]{2,10}-\d+$")


def _slugify(text: str, max_len: int = 60) -> str:
    """Mirrors scripts/watch_video.py:slugify exactly."""
    out = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower()
    return out[:max_len] or "video"


def _default_workdir(input_ref: str) -> str:
    """Pick a default workdir if the caller didn't specify one.

    Mirrors scripts/watch_video.py:default_workdir so MCP defaults match the
    CLI defaults exactly. Important properties:
    - "auto" mode uses a unix-timestamp slug so repeated auto runs don't
      share workdirs (the selected file from ~/Downloads may differ).
    - URL slug uses the FULL last path segment including query/fragment.
      That keeps youtube.com/watch?v=ABC distinct from ...v=DEF (otherwise
      both collapse to "watch" and race on the same _mcp_status.json), and
      also keeps generic /download?id=1 distinct from /download?id=2.
    - Jira-key inputs get a clean lowercased slug.
    - File-path inputs slug from the basename stem.
    """
    if input_ref == "auto":
        slug = f"watch-{int(_time.time())}"
    elif _JIRA_KEY_RE.match(input_ref):
        slug = f"watch-{input_ref.lower()}"
    elif "://" in input_ref:
        last_segment = input_ref.rsplit("/", 1)[-1] or "url"
        slug = f"watch-{_slugify(last_segment)}"
    else:
        slug = f"watch-{_slugify(Path(input_ref).stem)}"
    return str(DEFAULT_WORKDIR_ROOT / slug)


async def _run_pipeline_and_update_status(
    workdir: str,
    args: list[str],
) -> None:
    """Background task: run watch_video.py, write final status to _mcp_status.json.

    Run as `asyncio.create_task(...)` from watch_video_start, which returns
    immediately. This task lives for the duration of the pipeline (typically
    5-60 seconds depending on input). The status file is the only state the
    polling status tool reads.

    CRITICAL DESIGN POINT (v2.1.0-rc2 fix): subprocess stdout/stderr are
    redirected to LOG FILES, not PIPE. Prior v2.1.0-rc1 used PIPE + concurrent
    asyncio pumps via _spawn_script. That worked in isolation but hung in
    Claude Desktop: rapid status polls starved the MCP server's event loop,
    pump tasks got tiny CPU slices, the subprocess's pipe buffer filled,
    everything chained-blocked on writes.

    Log files have no buffer ceiling -- the OS just keeps appending. The
    subprocess never blocks on its output regardless of what the MCP server's
    event loop is doing. This is the structural fix the v2.0.x patches
    needed but never delivered.
    """
    workdir_path = Path(workdir).expanduser()
    workdir_path.mkdir(parents=True, exist_ok=True)
    stdout_log = workdir_path / "_mcp_stdout.log"
    stderr_log = workdir_path / "_mcp_stderr.log"

    argv = [sys.executable, str(SCRIPTS_DIR / "watch_video.py"), *args]
    proc = None
    out_f = None
    err_f = None
    try:
        # Open log files for output redirection. Mode "wb" truncates per
        # run -- previously "ab" appended, which left the previous run's
        # events tail-readable by watch_video_status's _read_last_event()
        # during the new run's cold-start delay. The status reporter would
        # show the *previous* run's final "complete" event while the new
        # job hadn't written anything yet, looking like the job was done.
        # Truncating per run keeps last_event correct at the cost of
        # debugging history -- previous runs' logs are gone after a retry.
        out_f = open(stdout_log, "wb", buffering=0)
        err_f = open(stderr_log, "wb", buffering=0)

        # WATCH_VIDEO_NO_PIPE tells watch_video.py to redirect its sub-script
        # stdio (probe.py, frames.py, dedup.py, transcribe.py, ...) to log
        # files too, instead of PIPE + threaded pump. In CLI/skill context
        # PIPE works fine; in MCP context the kernel pipe between the
        # orchestrator and each sub-script suffers 10-75s drain latency
        # because the MCP server's asyncio loop competes with the pipe
        # drain for OS scheduler priority. Log files have no buffer
        # ceiling and no drain contention -- the only structural fix.
        child_env = {**os.environ, "WATCH_VIDEO_NO_PIPE": "1"}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=out_f,
            stderr=err_f,
            env=child_env,
            # No PIPE anywhere in the tree -> no buffer to fill -> no
            # blocked writes at any layer.
        )

        # Wait for the subprocess to exit. proc.wait() is async-friendly:
        # it just polls the OS for exit status, doesn't read pipes. The
        # event loop is free to handle status polls from the host in parallel.
        rc = await proc.wait()

        if rc == 0:
            # Read the final JSON output from the log file. watch_video.py
            # prints meta.json (indent=2) at the very end of main(); take
            # the whole stdout log and extract the trailing JSON object.
            try:
                stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                stdout_text = f"(could not read {stdout_log}: {e})"
            meta_text = _extract_final_json(stdout_text)
            try:
                meta = json.loads(meta_text)
            except json.JSONDecodeError:
                meta = {"raw_stdout": meta_text}
            _write_status(workdir, {
                "state": "done",
                "completed_at": _time.time(),
                "meta": meta,
                "workdir": workdir,
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
            })
        else:
            # Subprocess exited non-zero. Read stderr tail for diagnostics.
            try:
                stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                stderr_text = f"(could not read {stderr_log}: {e})"
            tail = "\n".join(stderr_text.strip().splitlines()[-20:])
            _write_status(workdir, {
                "state": "failed",
                "completed_at": _time.time(),
                "error": f"watch_video.py exited with code {rc}. Last stderr "
                         f"lines:\n{tail}",
                "workdir": workdir,
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
            })
    except Exception as e:  # noqa: BLE001 -- status file is the only sink
        _write_status(workdir, {
            "state": "failed",
            "completed_at": _time.time(),
            "error": f"unexpected: {type(e).__name__}: {e}",
            "workdir": workdir,
        })
    finally:
        # Always close the log file handles so the subprocess's output is
        # flushed and the files are released.
        for f in (out_f, err_f):
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass


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
    """[DEPRECATED on Claude Desktop / Windows -- prefer watch_video_start +
    watch_video_status, see below.] Run the watch-video pipeline on an input.

    This tool blocks until the pipeline completes. On Claude Desktop + Windows
    it hangs reliably due to a stdio JSON-RPC pipe-buffer interaction during
    long-running tool calls; see https://github.com/MarcinSufa/claude-watch-video/issues/1.
    Other MCP hosts (Cursor, Cline, etc.) and direct Python callers may still
    work fine with this tool; it's kept for backwards compatibility.

    For Claude Desktop and any other host where this tool hangs, use the
    `watch_video_start(input_ref, ...)` + `watch_video_status(job_id)` polling
    pair instead. Same artifacts, same workdir, but every tool call returns
    in <100ms so no stdio pressure.

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
async def watch_video_start(
    input_ref: str,
    workdir: str | None = None,
    dedup: bool = True,
    ocr: bool = False,
    whisper: str = "auto",
    start: str | None = None,
    end: str | None = None,
    no_html: bool = False,
    no_docx: bool = False,
) -> str:
    """Start the watch-video pipeline as a background job. Returns immediately
    with a job_id; poll watch_video_status to track completion.

    This is the recommended pattern on Claude Desktop and any other host where
    the blocking `watch_video` tool hangs due to stdio JSON-RPC buffer pressure
    during long-running tool calls. Each call to start/status returns within
    ~100ms, so no pipe-buffer issues. The agent (you) is expected to poll the
    status tool every few seconds until the state is 'done'.

    Args:
        input_ref: A local path, a public URL (YouTube, Loom, etc.), a Jira
            issue key like 'PROJ-1234', or the literal 'auto' to grab the
            newest video from ~/Downloads.
        workdir: Output directory. If omitted, defaults to
            'C:\\tmp\\watch-<slug>' on Windows or '/tmp/watch-<slug>' on
            POSIX, where <slug> is derived from input_ref (YouTube video id
            when present, else last path segment).
        dedup, ocr, whisper, start, end, no_html, no_docx: same as watch_video.

    Returns:
        JSON string {"job_id": "<workdir-path>", "state": "running",
                     "started_at": <timestamp>, "workdir": "<path>"}
        The job_id IS the workdir path -- pass it back to watch_video_status
        and to the other MCP tools (read_transcript, read_report, etc.)
        once the job completes.
    """
    # Resolve workdir (job_id = workdir path).
    resolved_workdir = workdir or _default_workdir(input_ref)
    job_id = str(Path(resolved_workdir).expanduser().resolve())

    # Don't spawn a second pipeline for a workdir that already has one
    # running in THIS server process -- both tasks would write the same
    # artifacts, logs, cache, and _mcp_status.json, racing each other.
    #
    # Stale-running detection: if the status was written by a different
    # server process (different pid), that process is gone -- Claude
    # Desktop restarted, the MCP server crashed mid-job, etc. -- and the
    # in-memory task that would have written the terminal state died with
    # it. Treat the persisted "running" as stale and start fresh; otherwise
    # the user is stuck polling a job that never completes.
    existing = _read_status(job_id)
    if existing and existing.get("state") == "running":
        recorded_pid = existing.get("server_pid")
        if recorded_pid == os.getpid():
            return json.dumps({
                "job_id": job_id,
                "state": "running",
                "started_at": existing.get("started_at"),
                "workdir": job_id,
                "note": ("job already running for this workdir; reusing it. "
                         "Poll watch_video_status until the state transitions."),
            })
        # else: stale from a previous server instance -- fall through and
        # start a fresh pipeline (will overwrite the stale status below).

    # Build args for the CLI subprocess.
    args = [input_ref, "--workdir", job_id]
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

    # Write the initial "running" status BEFORE launching the background task,
    # so an immediate status poll always sees something. server_pid lets a
    # later watch_video_start detect stale-running state from a previous
    # server instance (see the duplicate-guard above).
    started_at = _time.time()
    _write_status(job_id, {
        "state": "running",
        "started_at": started_at,
        "input_ref": input_ref,
        "workdir": job_id,
        "server_pid": os.getpid(),
    })

    # Spawn the pipeline as a background task. Doesn't block the tool
    # response; the background task writes the final status to _mcp_status.json
    # when the pipeline completes (success or failure).
    #
    # IMPORTANT: hold a strong ref to the Task. The event loop only tracks
    # weak refs -- a dropped Task can be GC'd mid-await, which is the
    # v2.1.0-rc2 bug that left jobs stuck in "running" forever. See the
    # _background_tasks definition for the full story.
    task = asyncio.create_task(_run_pipeline_and_update_status(job_id, args))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return json.dumps({
        "job_id": job_id,
        "state": "running",
        "started_at": started_at,
        "workdir": job_id,
    })


@mcp.tool()
async def watch_video_status(job_id: str) -> str:
    """Poll the status of a watch_video job started via watch_video_start.

    Call this every few seconds until the state is 'done' or 'failed'. When
    state is 'done', the workdir field tells you where to find the artifacts
    (transcript.md, frames/, report.md/.html/.docx) -- pass it to
    read_transcript / read_report / read_highlights / post_to_jira /
    pick_highlights to consume the results.

    Args:
        job_id: The job_id returned by watch_video_start (= absolute workdir
            path). The status is read from <workdir>/_mcp_status.json.

    Returns:
        JSON string with the current state. Shape depends on state:
        - Running: {"state": "running", "started_at": <ts>, "workdir": "<p>",
                    "elapsed_seconds": <s>}
        - Done:    {"state": "done", "completed_at": <ts>, "workdir": "<p>",
                    "meta": {...full meta.json contents...}}
        - Failed:  {"state": "failed", "completed_at": <ts>, "workdir": "<p>",
                    "error": "..."}
        - Unknown: {"state": "unknown", "error": "no _mcp_status.json found"}
    """
    status = _read_status(job_id)
    if status is None:
        return json.dumps({
            "state": "unknown",
            "job_id": job_id,
            "error": f"No _mcp_status.json found at {_status_path(job_id)}. "
                     f"Either the job_id is wrong, or the job was never started "
                     f"via watch_video_start.",
        })
    # Add live elapsed time for running jobs (convenience for the agent).
    if status.get("state") == "running" and "started_at" in status:
        status["elapsed_seconds"] = round(_time.time() - status["started_at"], 1)
        # Step-level granularity: tail _mcp_stderr.log for the latest event
        # so the agent can see which step is currently in progress instead
        # of just "running". Cheap (we only parse the LAST line of the log).
        last_event = _read_last_event(job_id)
        if last_event is not None:
            status["last_event"] = last_event
    return json.dumps(status, indent=2)


def _read_last_event(workdir: str) -> dict | None:
    """Return the most recent JSON event from <workdir>/_mcp_stderr.log.

    Events are written one per line by _common.emit() in the pipeline
    sub-scripts. Reads the file in chunks from the end so it stays cheap
    even when the log gets long. Returns None on any error or missing log.
    """
    log_path = Path(workdir).expanduser() / "_mcp_stderr.log"
    if not log_path.is_file():
        return None
    try:
        # Read the last 8KB; almost always contains the final line.
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return None
    return None


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
    base_url: str | None = None,
    ctx: Context | None = None,
) -> str:
    """LLM-driven moment selection over the transcript.

    Args:
        workdir: The workdir path returned by watch_video.
        prompt: What to look for, e.g. 'identify the bug and the moment it
            occurs' or 'summarize the rate decision and inflation outlook'.
        max_n: Maximum number of moments to return. Default 5.
        provider: One of 'anthropic' (default), 'openai', 'groq', 'deepseek',
            'gemini', or 'openai-compat'. The first four read API keys from
            ANTHROPIC_API_KEY / OPENAI_API_KEY / GROQ_API_KEY /
            DEEPSEEK_API_KEY env vars (or ~/.watch-video/credentials.json).
            'gemini' uses GEMINI_API_KEY against Google's OpenAI-compatibility
            endpoint. 'openai-compat' is a generic escape hatch for any
            OpenAI-compatible endpoint -- requires base_url + an
            OPENAI_COMPAT_API_KEY.
        model: Optional model id; falls back to per-provider default
            (claude-haiku-4-5, gpt-4o-mini, llama-3.1-70b, deepseek-chat,
            gemini-2.0-flash, gpt-3.5-turbo respectively).
        base_url: REQUIRED only when provider='openai-compat'. Examples:
            Together AI -> https://api.together.xyz/v1
            Fireworks   -> https://api.fireworks.ai/inference/v1
            OpenRouter  -> https://openrouter.ai/api/v1
            Ollama      -> http://localhost:11434/v1
            vLLM        -> http://localhost:8000/v1
            Ignored for the other named providers (their base_url is
            built in).

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
    if base_url:
        args += ["--base-url", base_url]
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
