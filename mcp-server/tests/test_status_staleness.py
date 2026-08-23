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
async def test_status_without_a_pid_is_not_called_stale(tmp_path):
    """Status files written by an older version carry no server_pid. Absence
    of evidence is not evidence of death."""
    _running(tmp_path)
    out = json.loads(await server.watch_video_status(str(tmp_path)))
    assert out["state"] == "running"


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
    assert server._job_is_orphaned({"state": "running"}) is False
    assert server._job_is_orphaned({"state": "done", "server_pid": 1}) is False
