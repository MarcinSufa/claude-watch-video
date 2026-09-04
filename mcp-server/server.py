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
import tempfile
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

    Child stdio goes to LOG FILES, never PIPE, for the reason spelled out on
    _run_pipeline_and_update_status: under an MCP host the server's event
    loop is starved while a tool call is in flight, the asyncio pump tasks
    get almost no CPU, the kernel pipe fills, and the child blocks on its
    first write. Measured on Windows fetching a 65 KB Jira attachment:
    the piped version never returned in 1800 s (twice), and the child sat at
    0.02 s of CPU the whole time, while the same script from a shell finished
    in 2.0 s. A file has no buffer ceiling, so the child never blocks
    regardless of what the loop is doing.

    stdin is /dev/null: the child inherits the server's stdin otherwise,
    which is the host's JSON-RPC pipe, and a child that reads it steals the
    host's bytes.

    ``ctx`` is accepted for forward compatibility but unused: live
    notifications (ctx.info / ctx.report_progress) deadlock the call at the
    SERVER-TO-HOST layer. Progress belongs on the polling pattern instead
    (watch_video_start + watch_video_status).
    """
    argv = [sys.executable, str(SCRIPTS_DIR / script), *args]
    proc = None
    with tempfile.TemporaryDirectory(prefix="watch-video-mcp-") as tmp:
        stdout_path = Path(tmp) / "stdout.log"
        stderr_path = Path(tmp) / "stderr.log"
        try:
            with open(stdout_path, "wb", buffering=0) as out_f, open(
                    stderr_path, "wb", buffering=0) as err_f:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                )
                rc = await proc.wait()
        except asyncio.CancelledError:
            # Without this the child outlives the cancelled call and keeps
            # writing into a temp dir nobody reads (observed: a fetch child
            # still running hours after its tool call was aborted). Killed
            # without awaiting: an await inside a cancelled task is not
            # guaranteed to resume, which would hang the cancellation itself.
            if proc is not None and proc.returncode is None:
                proc.kill()
            raise
        return (
            rc if rc is not None else -1,
            _read_log(stdout_path),
            _read_log(stderr_path),
        )


def _read_log(path: Path) -> str:
    """Read a child's log file back, tolerating a child that wrote nothing."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


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
       or '[' (multi-line JSON always starts a new object at the beginning of
       a line under print(json.dumps(..., indent=2)); fetch_attachment.py
       returns a top-level array). Parse from there to EOF.
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
        if lines[i].lstrip()[:1] in ("{", "["):
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


def _job_is_orphaned(status: dict) -> bool:
    """True when a persisted "running" job has no one left to finish it.

    The background pipeline task lives inside the server process that wrote
    the status file. If that pid is not ours, that task is not running here,
    and the process that owned it is gone (host restart, crash, session end)
    -- nothing will ever write the terminal state. Observed in the wild: a
    status file still claiming "running" two days after its server exited,
    with watch_video_status happily reporting a growing elapsed_seconds.

    Deliberately not a liveness probe on the pid: pids get reused, and
    os.kill(pid, 0) TERMINATES the target on Windows. "Not this process" is
    the rule watch_video_start already applied before starting fresh, and
    this is that rule inverted, unchanged -- including for a missing
    server_pid. Every running record this binary writes stamps the field, so
    a record without it came from an older build and cannot be ours. Letting
    None mean "still running" would have blocked start from recovering
    exactly the orphaned jobs this exists to recover.
    """
    if status.get("state") != "running":
        return False
    return status.get("server_pid") != os.getpid()


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


def _failure_payload(rc: int, stdout_text: str, stderr_tail: str) -> dict:
    """Build the status a failed pipeline leaves behind.

    Exit code 5 (AMBIGUOUS) is the one failure the agent can clear on its
    own: the pipeline prints the candidate attachments as JSON on STDOUT and
    stops. Reporting the stderr tail alone hid that list behind a bare "5
    video attachments on PROJ-1234", so the agent had to go re-enumerate the
    ticket through another tool. Lift the payload into the status and name
    the parameter that unblocks it.
    """
    payload: dict = {
        "state": "failed",
        "completed_at": _time.time(),
        "error": f"watch_video.py exited with code {rc}. Last stderr "
                 f"lines:\n{stderr_tail}",
    }
    try:
        parsed = json.loads(_extract_final_json(stdout_text))
    except json.JSONDecodeError:
        return payload
    if isinstance(parsed, dict) and parsed.get("ambiguous"):
        payload["ambiguous"] = parsed
        payload["error"] = (
            f"{len(parsed.get('candidates', []))} video attachments on "
            f"{parsed.get('issue_key', 'the issue')}; nothing was downloaded. "
            "Ask which one, then re-run watch_video_start with "
            "attachment_id=<id> from the ambiguous.candidates list below."
        )
    return payload


def _default_workdir(input_ref: str, attachment_id: str | None = None) -> str:
    """Pick a default workdir if the caller didn't specify one.

    Mirrors scripts/watch_video.py:default_workdir so MCP defaults match the
    CLI defaults exactly. Important properties:
    - "auto" mode uses a unix-timestamp slug so repeated auto runs don't
      share workdirs (the selected file from ~/Downloads may differ).
    - URL slug uses the FULL last path segment including query/fragment.
      That keeps youtube.com/watch?v=ABC distinct from ...v=DEF (otherwise
      both collapse to "watch" and race on the same _mcp_status.json), and
      also keeps generic /download?id=1 distinct from /download?id=2.
    - Jira-key inputs get a clean lowercased slug, plus the attachment id
      when one was named: two videos on one ticket would otherwise share a
      workdir, hence a _mcp_status.json, a frames dir and a cache.
    - File-path inputs slug from the basename stem.
    """
    if input_ref == "auto":
        slug = f"watch-{int(_time.time())}"
    elif _JIRA_KEY_RE.match(input_ref):
        slug = f"watch-{input_ref.lower()}"
        if attachment_id:
            slug = f"{slug}-{_slugify(attachment_id)}"
    elif "://" in input_ref:
        last_segment = input_ref.rsplit("/", 1)[-1] or "url"
        slug = f"watch-{_slugify(last_segment)}"
    else:
        slug = f"watch-{_slugify(Path(input_ref).stem)}"
    return str(DEFAULT_WORKDIR_ROOT / slug)


def _build_pipeline_args(
    input_ref: str,
    workdir: str,
    attachment_id: str | None,
    frames: int | None,
    dedup: bool,
    ocr: bool,
    whisper: str,
    start: str | None,
    end: str | None,
    no_html: bool,
    no_docx: bool,
) -> list[str]:
    """Build watch_video.py's argv tail. Shared by watch_video and
    watch_video_start so the two tools can't drift on flag handling."""
    args = [input_ref, "--workdir", workdir]
    if attachment_id:
        args += ["--attachment-id", attachment_id]
    if frames is not None:
        args += ["--frames", str(frames)]
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
    return args


def _reset_run_logs(workdir_path: Path) -> None:
    """Delete every file under <workdir>/_step_logs/ left by a previous run.

    A step that cache-hits this run never opens its log file, so a stale
    <step>.stderr.log from an earlier run stays on disk with an old "ts".
    Without clearing it, a status poll right after start -- before the new
    orchestrator has emitted anything of its own -- can pick that stale file
    as the newest event and report a step that isn't actually running.
    """
    step_logs_dir = workdir_path / "_step_logs"
    if not step_logs_dir.is_dir():
        return
    for f in step_logs_dir.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass


def _existing_running_job(job_id: str) -> dict | None:
    """Return the status payload of a non-orphaned "running" job at job_id,
    or None if there isn't one.

    Shared by watch_video and watch_video_start so a caller of either tool
    can't spawn a second pipeline over one already running in THIS server
    process for the same workdir -- both tasks would write the same
    artifacts, logs, cache, and _mcp_status.json, racing each other. A
    "running" record left by a since-exited server process doesn't count
    (see _job_is_orphaned): that pipeline task died with its process, so
    nothing will ever advance it, and a fresh run is what's needed.
    """
    existing = _read_status(job_id)
    if existing and existing.get("state") == "running" and not _job_is_orphaned(existing):
        return existing
    return None


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
    argv = [sys.executable, str(SCRIPTS_DIR / "watch_video.py"), *args]
    proc = None
    out_f = None
    err_f = None
    try:
        # mkdir/_reset_run_logs are inside the try too: a cancellation that
        # lands before either runs would otherwise skip both the except
        # branch below and the finally's (harmless, since out_f/err_f are
        # still None here) cleanup, leaving nothing to write a terminal
        # status -- the same "stuck running forever" failure this whole
        # except branch exists to prevent.
        workdir_path = Path(workdir).expanduser()
        workdir_path.mkdir(parents=True, exist_ok=True)
        _reset_run_logs(workdir_path)
        stdout_log = workdir_path / "_mcp_stdout.log"
        stderr_log = workdir_path / "_mcp_stderr.log"

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
            # Subprocess exited non-zero. stdout still matters here: an
            # AMBIGUOUS exit carries its candidate attachments there.
            try:
                stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stdout_text = ""
            try:
                stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                stderr_text = f"(could not read {stderr_log}: {e})"
            tail = "\n".join(stderr_text.strip().splitlines()[-20:])
            _write_status(workdir, {
                **_failure_payload(rc, stdout_text, tail),
                "workdir": workdir,
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
            })
    except asyncio.CancelledError:
        # CancelledError is BaseException, not Exception -- the broad catch
        # below never sees it. Without this branch, a cancelled task (host
        # shutdown, task-group teardown) leaves the status file stuck at
        # "running" forever, same failure mode _job_is_orphaned exists to
        # detect for a dead server process, but for a task killed inside a
        # live one. Write the terminal state, then re-raise so cancellation
        # still propagates as normal.
        #
        # Cancelling this task does NOT touch the child process -- proc.wait()
        # just stops being awaited, the subprocess keeps running and keeps
        # writing artifacts into workdir, and a retry races it. Kill it first.
        killed = False
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                killed = True
            except (ProcessLookupError, OSError):
                pass
        error = ("pipeline task cancelled; child process killed" if killed
                  else "pipeline task cancelled")
        _write_status(workdir, {
            "state": "failed",
            "completed_at": _time.time(),
            "error": error,
            "workdir": workdir,
        })
        raise
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
    attachment_id: str | None = None,
    frames: int | None = None,
    dedup: bool = True,
    ocr: bool = False,
    whisper: str = "auto",
    start: str | None = None,
    end: str | None = None,
    no_html: bool = False,
    no_docx: bool = False,
    ctx: Context | None = None,
) -> str:
    """Run the watch-video pipeline on an input and block until it finishes.

    Runs through the same log-file-backed runner as watch_video_start, so
    subprocess output is no longer piped: no 10-75 s per-step drain latency
    and no orchestrator-side deadlock at that layer. It still holds this one
    tool call open for the whole pipeline though -- minutes with --ocr or
    local Whisper transcription -- and a host that penalizes long-running
    tool calls (Claude Desktop on Windows among them) can still stall on
    that, independent of the subprocess-piping fix. `watch_video_start(input_ref,
    ...)` + `watch_video_status(job_id)` remain the recommended pattern
    there. Same artifacts, same workdir either way.

    Args:
        input_ref: A local path, a public URL (YouTube, Loom, etc.), a Jira
            issue key like 'PROJ-1234', or the literal 'auto' to grab the
            newest video from ~/Downloads.
        workdir: Output directory. If omitted, the pipeline picks a default
            under c:\\tmp\\watch-<slug>\\ (Windows) or equivalent.
        attachment_id: Jira mode only. Which video to take when the issue
            carries more than one; without it such a run stops and reports
            the candidates. Ids come from that report, or from an earlier
            getJiraIssue call.
        frames: How many frames to sample across the window. Omit to let
            the pipeline size the budget from the duration. This is the knob
            for a silent recording, where dedup does not run.
        dedup: Run smart perceptual-hash dedup with transcript-aware
            protection. Default true. Automatically skipped on a run with no
            transcript (silent or no-audio video), where it would drop the
            small UI changes a screen recording exists to show.
        ocr: Run Tesseract OCR on kept frames. Default false. Costs 1.7-2.3 s
            per frame plus a cold start; for a silent UI recording, reading
            frames/ directly is faster.
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
    job_id = str(Path(workdir or _default_workdir(input_ref, attachment_id))
                 .expanduser().resolve())

    # A job already running for this workdir (started via watch_video_start,
    # or a concurrent watch_video call) owns the pipeline -- spawning a
    # second one would race it for the same artifacts, logs, cache, and
    # status file. Wait on that one instead of starting a fresh one.
    if _existing_running_job(job_id) is not None:
        while True:
            status = _read_status(job_id)
            # None: the status file vanished from under the job. Orphaned:
            # the process that owned it is gone (see _job_is_orphaned), so
            # nothing will ever write a terminal state -- spinning here
            # would wait forever. Either way, stop and let the caller see it.
            if status is None or status.get("state") != "running" or _job_is_orphaned(status):
                break
            await asyncio.sleep(1)
    else:
        args = _build_pipeline_args(
            input_ref, job_id, attachment_id, frames, dedup, ocr, whisper,
            start, end, no_html, no_docx)
        _write_status(job_id, {
            "state": "running",
            "started_at": _time.time(),
            "input_ref": input_ref,
            "workdir": job_id,
            "server_pid": os.getpid(),
        })
        await _run_pipeline_and_update_status(job_id, args)
        status = _read_status(job_id)
    if status is None:
        raise RuntimeError(f"pipeline finished but wrote no status at {job_id}")
    if _job_is_orphaned(status):
        raise RuntimeError(
            f"the job running for this workdir was orphaned (owning process "
            f"{status.get('server_pid')} is no longer running); call "
            f"watch_video_start again for {job_id} to run it fresh")
    if status["state"] == "failed":
        error = status.get("error", "unknown error")
        if status.get("ambiguous"):
            raise RuntimeError(error + "\n" + json.dumps(status["ambiguous"], indent=2))
        raise RuntimeError(error)
    return json.dumps(status["meta"])


@mcp.tool()
async def watch_video_start(
    input_ref: str,
    workdir: str | None = None,
    attachment_id: str | None = None,
    frames: int | None = None,
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

    This is the recommended pattern on Claude Desktop and any other host that
    penalizes a long-running tool call: the blocking `watch_video` tool holds
    one call open for the whole pipeline, which can still stall against
    stdio JSON-RPC buffer pressure on such hosts even though it no longer
    pipes its subprocess output. Each call to start/status returns within
    ~100ms, so no such pressure here. The agent (you) is expected to poll the
    status tool every few seconds until the state is 'done'.

    Args:
        input_ref: A local path, a public URL (YouTube, Loom, etc.), a Jira
            issue key like 'PROJ-1234', or the literal 'auto' to grab the
            newest video from ~/Downloads.
        workdir: Output directory. If omitted, defaults to
            'C:\\tmp\\watch-<slug>' on Windows or '/tmp/watch-<slug>' on
            POSIX, where <slug> is derived from input_ref (YouTube video id
            when present, else last path segment, plus attachment_id in Jira
            mode).
        attachment_id: Jira mode only. Which video to take when the issue
            carries more than one. Without it, such a run ends as
            state='failed' with an 'ambiguous' block listing every candidate
            (id, filename, size, created, author): ask which one, then start
            again passing its id. Ids also come from getJiraIssue.
        frames, dedup, ocr, whisper, start, end, no_html, no_docx: same as
            watch_video.

    Returns:
        JSON string {"job_id": "<workdir-path>", "state": "running",
                     "started_at": <timestamp>, "workdir": "<path>"}
        The job_id IS the workdir path -- pass it back to watch_video_status
        and to the other MCP tools (read_transcript, read_report, etc.)
        once the job completes.
    """
    # Resolve workdir (job_id = workdir path).
    resolved_workdir = workdir or _default_workdir(input_ref, attachment_id)
    job_id = str(Path(resolved_workdir).expanduser().resolve())

    # Don't spawn a second pipeline for a workdir that already has one
    # running in THIS server process (see _existing_running_job). A "running"
    # record from a since-exited server process doesn't count -- fall through
    # and start a fresh pipeline (will overwrite the stale status below).
    existing = _existing_running_job(job_id)
    if existing is not None:
        return json.dumps({
            "job_id": job_id,
            "state": "running",
            "started_at": existing.get("started_at"),
            "workdir": job_id,
            "note": ("job already running for this workdir; reusing it. "
                     "Poll watch_video_status until the state transitions."),
        })

    # Build args for the CLI subprocess.
    args = _build_pipeline_args(
        input_ref, job_id, attachment_id, frames, dedup, ocr, whisper,
        start, end, no_html, no_docx)

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

    Call this every few seconds until the state is 'done', 'failed' or
    'stale'. All three are terminal for polling purposes. When
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
                   On an ambiguous Jira issue it also carries "ambiguous":
                   {"issue_key": ..., "candidates": [{"id", "filename",
                   "size_bytes", "created", "author"}, ...]} -- pick one and
                   call watch_video_start again with attachment_id.
        - Stale:   {"state": "stale", "owner_pid": <pid>, "artifacts": [...]}
                   -- the server process that owned this job is gone, so the
                   file is stuck at "running". Stop polling; call
                   watch_video_start again to run it fresh.
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
    if _job_is_orphaned(status):
        # Don't let the agent poll a job nobody is running. Say what happened
        # and how to recover, rather than reporting "running" forever.
        stale = {
            "state": "stale",
            "job_id": job_id,
            "workdir": status.get("workdir", job_id),
            "started_at": status.get("started_at"),
            "owner_pid": status.get("server_pid"),
            "error": (
                "This job was started by MCP server process "
                f"{status.get('server_pid')}, which is no longer this process "
                f"({os.getpid()}). The pipeline task died with it, so the "
                "status file is stuck at 'running' and will never advance."),
            "hint": ("Call watch_video_start again for this workdir to run it "
                     "fresh; it overwrites the stale status. Partial artifacts "
                     "already in the workdir are listed below."),
            "artifacts": sorted(
                p.name for p in Path(job_id).expanduser().glob("*")
                if not p.name.startswith("_")
            ) if Path(job_id).expanduser().is_dir() else [],
        }
        last_event = _read_last_event(job_id)
        if last_event is not None:
            stale["last_event"] = last_event
        return json.dumps(stale, indent=2)

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


def _tail_last_json_event(log_path: Path) -> dict | None:
    """Return the last JSON-object line in log_path, tail-read from 8KB back."""
    if not log_path.is_file():
        return None
    try:
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


def _read_last_event(workdir: str) -> dict | None:
    """Return the most recent JSON event across _mcp_stderr.log and every
    _step_logs/*.stderr.log, keyed by "ts".

    _mcp_stderr.log only receives a sub-step's events after that step exits
    (_run_step_via_log_files in scripts/watch_video.py forwards them on
    completion), so during a long step (OCR on a big frame set) it shows a
    stale event from the previous step. The live events are in that step's
    own log under _step_logs/ the whole time it runs. Comparing "ts" across
    both sources picks whichever is actually newest.
    """
    workdir_path = Path(workdir).expanduser()
    candidates = [workdir_path / "_mcp_stderr.log"]
    step_logs_dir = workdir_path / "_step_logs"
    if step_logs_dir.is_dir():
        candidates += sorted(step_logs_dir.glob("*.stderr.log"))

    best: dict | None = None
    for log_path in candidates:
        event = _tail_last_json_event(log_path)
        if event is None:
            continue
        if best is None or event.get("ts", 0) > best.get("ts", 0):
            best = event
    return best


@mcp.tool()
async def read_transcript(workdir: str) -> str:
    """Read transcript.md from a watch-video workdir.

    Args:
        workdir: The workdir path returned by watch_video.

    Returns:
        The full transcript text (prose paragraphs with MM:SS markers).
    """
    wd = Path(workdir).expanduser()
    p = wd / "transcript.md"
    if not p.is_file():
        meta_path = wd / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
            reason = meta.get("skipped_audio_reason")
            if reason and meta.get("transcript") is None:
                raise RuntimeError(
                    f"no transcript for this run -- transcription was skipped: "
                    f"{reason}. The frames are the whole evidence here; read "
                    f"report.md (read_report) instead.")
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


def _confine_outdir(outdir: str | None, jira_key: str) -> str | None:
    """Keep a caller-supplied destination under DEFAULT_WORKDIR_ROOT.

    The destination arrives from the model, which reads Jira tickets --
    attacker-controlled text. Sanitized filenames do not help when the
    DIRECTORY is the attack: 'authorized_keys' and 'config' are ordinary
    names, and finalize() replaces whatever is already there. So the tool
    accepts a subdirectory of the tmp root and nothing else; the CLI, driven
    by a human, keeps the unrestricted --outdir flag.
    """
    if outdir is None:
        return None
    root = DEFAULT_WORKDIR_ROOT.expanduser().resolve()
    p = Path(outdir).expanduser().resolve()
    if p == root or not p.is_relative_to(root):
        raise ValueError(
            f"outdir must be a subdirectory of {root} (got {p}). Omit outdir "
            f"to use the default {root / f'jira-{jira_key}'}.")
    return str(p)


@mcp.tool()
async def fetch_jira_attachment(
    jira_key: str,
    mime_prefix: str = "image/",
    attachment_id: str | None = None,
    outdir: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Download non-video attachments (images, PDFs, ...) from a Jira issue.

    Nothing is written to Jira, so unlike post_to_jira there is no confirm
    flag. It DOES write files to the local disk, under <tmp> only.
    It exists because an agent sandbox typically cannot make the
    authenticated HTTPS call itself; this server can, and reads the Atlassian
    token on the agent's behalf. Files land on disk and the agent then opens
    them with an ordinary file read.

    Args:
        jira_key: Jira issue key, e.g. PROJ-1234.
        mime_prefix: mimeType prefix filter. 'image/' (default), 'video/',
            'application/pdf', or '' to take every attachment.
        attachment_id: Download exactly this attachment, ignoring mime_prefix.
        outdir: Destination directory. Must be under the tmp root; anything
            else is rejected. Default: <tmp>/jira-<KEY>/.

    Returns:
        JSON array, one object per attachment: {id, filename, mime_type,
        size_bytes, path}. Anything not downloaded comes back with path=null
        and skipped set to "too_large" (over ~50 MB), "size_mismatch", or
        "over_file_limit" (past the first 25 attachments).
    """
    outdir = _confine_outdir(outdir, jira_key)
    args = [jira_key, "--mime-prefix", mime_prefix]
    if attachment_id:
        args += ["--attachment-id", attachment_id]
    if outdir:
        args += ["--outdir", outdir]
    rc, stdout, stderr = await _spawn_script("fetch_attachment.py", *args, ctx=ctx)
    if rc != 0:
        raise RuntimeError(_format_child_error(rc, stderr, "fetch_attachment.py"))
    return _extract_final_json(stdout)


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
