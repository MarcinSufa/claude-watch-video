"""watch_video_status must not report a dead job as running.

Measured failure that motivated this: a job started 2026-08-21 15:30 whose
MCP server process later exited. Two days later `_mcp_status.json` still read
`{"state": "running"}`, `watch_video_status` still answered "running" with a
growing elapsed_seconds, and the owning pid did not exist. The pipeline task
lives INSIDE the server process, so when that process goes, nothing is left
to write the terminal state.

watch_video_start already treats a status written by another pid as stale
(it starts a fresh pipeline). This pins the same rule into status.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def _running(workdir: Path, **extra) -> None:
    server._write_status(str(workdir), {
        "state": "running",
        "started_at": 1.0,
        "input_ref": "PROJ-1234",
        "workdir": str(workdir),
        **extra,
    })


@pytest.mark.asyncio
async def test_running_from_a_dead_server_is_reported_as_stale(tmp_path):
    _running(tmp_path, server_pid=os.getpid() + 1_000_000)  # cannot be us
    out = json.loads(await server.watch_video_status(str(tmp_path)))
    assert out["state"] == "stale", out
    assert "watch_video_start" in json.dumps(out), (
        "the agent needs to be told how to recover, not just that it is stuck")


@pytest.mark.asyncio
async def test_running_in_this_server_stays_running(tmp_path):
    _running(tmp_path, server_pid=os.getpid())
    out = json.loads(await server.watch_video_status(str(tmp_path)))
    assert out["state"] == "running"
    assert "elapsed_seconds" in out


@pytest.mark.asyncio
async def test_status_without_a_pid_is_stale(tmp_path):
    """A running record written by THIS binary always carries server_pid, so
    one without the field came from an older build -- i.e. not this process.

    Treating it as still-running is also what main's watch_video_start did
    NOT do: there, `recorded_pid == os.getpid()` was false for None and the
    job was restarted. The two callers have to agree."""
    _running(tmp_path)
    out = json.loads(await server.watch_video_status(str(tmp_path)))
    assert out["state"] == "stale", out


@pytest.mark.asyncio
async def test_start_restarts_a_pidless_running_job(tmp_path, monkeypatch):
    """Regression guard for the recovery path itself: before this fix landed,
    `if not _job_is_orphaned(existing)` handed back the reuse payload for a
    pid-less file, so a job orphaned by an older build could never be
    restarted -- the opposite of what this PR is for."""
    ran: list = []

    async def _fake_pipeline(job_id, args):
        ran.append((job_id, args))
    monkeypatch.setattr(server, "_run_pipeline_and_update_status", _fake_pipeline)

    _running(tmp_path)  # no server_pid
    out = json.loads(await server.watch_video_start(
        input_ref="PROJ-1234", workdir=str(tmp_path)))
    assert out["state"] == "running"
    assert "note" not in out, f"reused a job nobody is running: {out}"
    await asyncio.sleep(0)  # let the background task start
    assert ran, "no fresh pipeline was started"


@pytest.mark.asyncio
async def test_start_reuses_a_job_this_process_owns(tmp_path, monkeypatch):
    """The other side of the same rule: a job this very process started is
    not restarted underneath itself."""
    async def _fake_pipeline(job_id, args):  # pragma: no cover - must not run
        raise AssertionError("started a second pipeline over a live one")
    monkeypatch.setattr(server, "_run_pipeline_and_update_status", _fake_pipeline)

    _running(tmp_path, server_pid=os.getpid())
    out = json.loads(await server.watch_video_start(
        input_ref="PROJ-1234", workdir=str(tmp_path)))
    assert out["state"] == "running"
    assert "note" in out and "already running" in out["note"]


@pytest.mark.asyncio
async def test_terminal_states_are_untouched_by_the_staleness_rule(tmp_path):
    for state in ("done", "failed"):
        server._write_status(str(tmp_path), {
            "state": state,
            "completed_at": 2.0,
            "workdir": str(tmp_path),
            "server_pid": os.getpid() + 1_000_000,
            "error": "boom" if state == "failed" else None,
        })
        out = json.loads(await server.watch_video_status(str(tmp_path)))
        assert out["state"] == state


def test_start_and_status_share_one_staleness_rule():
    """Two copies of this rule would drift. watch_video_start's stale branch
    and watch_video_status must ask the same function."""
    assert callable(getattr(server, "_job_is_orphaned", None)), (
        "expected a shared _job_is_orphaned() helper")
    mine = {"state": "running", "server_pid": os.getpid()}
    theirs = {"state": "running", "server_pid": os.getpid() + 1_000_000}
    assert server._job_is_orphaned(theirs) is True
    assert server._job_is_orphaned(mine) is False
    assert server._job_is_orphaned({"state": "running"}) is True  # no pid = not ours
    assert server._job_is_orphaned({"state": "done", "server_pid": 1}) is False
