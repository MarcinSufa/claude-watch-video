"""watch_video (blocking) must run through the same no-pipe runner as
watch_video_start, not through _spawn_script's PIPE + asyncio-pump path.

That pipe path is exactly the drain wedge NO_PIPE mode was built to avoid --
an agent calling the blocking tool saw its whole session freeze. This pins
watch_video to _run_pipeline_and_update_status by making _spawn_script raise
if it's ever called.
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


def _forbid_spawn_script(monkeypatch):
    async def _boom(*args, **kwargs):
        raise AssertionError("watch_video must not call _spawn_script (PIPE path)")
    monkeypatch.setattr(server, "_spawn_script", _boom)


@pytest.mark.asyncio
async def test_watch_video_returns_meta_via_log_file_runner(tmp_path, monkeypatch):
    _forbid_spawn_script(monkeypatch)

    async def _fake_pipeline(job_id, args):
        server._write_status(job_id, {
            "state": "done",
            "completed_at": 2.0,
            "meta": {"workdir": job_id, "elapsed_seconds": 1.2},
            "workdir": job_id,
        })
    monkeypatch.setattr(server, "_run_pipeline_and_update_status", _fake_pipeline)

    out = await server.watch_video("some/video.mp4", workdir=str(tmp_path / "wd"))
    assert json.loads(out) == {"workdir": str(tmp_path / "wd"), "elapsed_seconds": 1.2}


@pytest.mark.asyncio
async def test_watch_video_raises_with_ambiguous_candidates_on_failure(tmp_path, monkeypatch):
    _forbid_spawn_script(monkeypatch)

    ambiguous = {
        "issue_key": "PROJ-1234",
        "candidates": [{"id": "1001", "filename": "a.mp4"}],
    }

    async def _fake_pipeline(job_id, args):
        server._write_status(job_id, {
            "state": "failed",
            "completed_at": 2.0,
            "error": "2 video attachments on PROJ-1234; nothing was downloaded.",
            "ambiguous": ambiguous,
            "workdir": job_id,
        })
    monkeypatch.setattr(server, "_run_pipeline_and_update_status", _fake_pipeline)

    with pytest.raises(RuntimeError) as exc_info:
        await server.watch_video("PROJ-1234", workdir=str(tmp_path / "wd"))

    message = str(exc_info.value)
    assert "PROJ-1234" in message
    assert "1001" in message
    assert "a.mp4" in message


@pytest.mark.asyncio
async def test_watch_video_waits_on_an_already_running_job_instead_of_spawning(
        tmp_path, monkeypatch):
    """watch_video_start already guards against two pipelines racing the same
    workdir (server.py's _existing_running_job). The blocking tool must obey
    the same guard: if a job is already running here, join it instead of
    starting a second one over the same artifacts, logs, cache, and status
    file."""
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

    async def _finish_soon():
        await asyncio.sleep(0.1)
        server._write_status(job_id, {
            "state": "done",
            "completed_at": 2.0,
            "meta": {"workdir": job_id, "elapsed_seconds": 1.2},
            "workdir": job_id,
        })
    task = asyncio.create_task(_finish_soon())

    out = await server.watch_video("some/video.mp4", workdir=job_id)
    await task
    assert json.loads(out) == {"workdir": job_id, "elapsed_seconds": 1.2}
