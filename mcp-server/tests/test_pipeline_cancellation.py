"""A cancelled pipeline task must leave a terminal status, not a stuck
"running" one -- and the blocking tool's wait loop on an already-running job
must not spin forever chasing a job nobody will ever finish.

asyncio.CancelledError is BaseException, not Exception, so the broad
`except Exception` in _run_pipeline_and_update_status never saw it: a
cancelled task (host shutdown, task-group teardown) used to leave
_mcp_status.json reading "running" forever, same failure mode
_job_is_orphaned exists to catch for a dead server process, but for a task
killed inside a live one.
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


class _HangingProc:
    """A subprocess handle that never exits on its own -- used to simulate
    a task cancelled while awaiting proc.wait()."""

    def __init__(self):
        self.returncode = None
        self.killed = False

    async def wait(self):
        await asyncio.sleep(100)  # never resolves before cancellation

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_cancelling_the_runner_task_writes_a_failed_status(tmp_path, monkeypatch):
    workdir = str(tmp_path)

    async def _fake_exec(*args, **kwargs):
        return _HangingProc()
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _fake_exec)

    task = asyncio.create_task(
        server._run_pipeline_and_update_status(workdir, ["some/video.mp4"]))
    await asyncio.sleep(0)  # let the task reach proc.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    status = server._read_status(workdir)
    assert status is not None
    assert status["state"] == "failed", status
    assert "cancelled" in status["error"]


@pytest.mark.asyncio
async def test_cancelling_the_runner_task_kills_the_child_process(tmp_path, monkeypatch):
    """Cancelling the asyncio task only stops US awaiting proc.wait() -- the
    child process itself keeps running and keeps writing artifacts into
    workdir unless something kills it, and a retry would race it."""
    workdir = str(tmp_path)
    fake_proc = _HangingProc()

    async def _fake_exec(*args, **kwargs):
        return fake_proc
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _fake_exec)

    task = asyncio.create_task(
        server._run_pipeline_and_update_status(workdir, ["some/video.mp4"]))
    await asyncio.sleep(0)  # let the task reach proc.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake_proc.killed, "the child process was left running after cancellation"

    status = server._read_status(workdir)
    assert status is not None
    assert status["state"] == "failed", status
    assert "killed" in status["error"]


@pytest.mark.asyncio
async def test_blocking_tool_stops_waiting_on_an_orphaned_job(tmp_path, monkeypatch):
    """If the job this tool is waiting on gets rewritten as orphaned (its
    owning server process died), the wait loop must notice and stop instead
    of polling a job that will never advance."""
    async def _boom(job_id, args):
        raise AssertionError("watch_video spawned a second pipeline over a live one")
    monkeypatch.setattr(server, "_run_pipeline_and_update_status", _boom)

    job_id = str((tmp_path / "wd").resolve())
    server._write_status(job_id, {
        "state": "running",
        "started_at": 1.0,
        "input_ref": "some/video.mp4",
        "workdir": job_id,
        "server_pid": os.getpid(),
    })

    async def _orphan_soon():
        await asyncio.sleep(0.1)
        server._write_status(job_id, {
            "state": "running",
            "started_at": 1.0,
            "input_ref": "some/video.mp4",
            "workdir": job_id,
            "server_pid": os.getpid() + 1_000_000,  # cannot be us
        })
    task = asyncio.create_task(_orphan_soon())

    with pytest.raises(RuntimeError, match="orphaned"):
        await asyncio.wait_for(
            server.watch_video("some/video.mp4", workdir=job_id), timeout=3)
    await task
