"""The pipeline spawn must close stdin too, and clear the logs before it
publishes state=running.

Both come from the same class of bug as the one-shot fix: the orchestrator and
its grandchildren (ffmpeg reads stdin by default) otherwise inherit the host's
JSON-RPC pipe, and a status poll landing between "running" and the background
task's reset reads the PREVIOUS run's newest event.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


class _FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_pipeline_spawn_closes_stdin(tmp_path, monkeypatch):
    seen: dict = {}

    async def _fake_exec(*argv, **kwargs):
        seen["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await server._run_pipeline_and_update_status(str(tmp_path / "wd"), ["--input", "x.mp4"])

    assert seen["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert seen["kwargs"]["stdout"] is not asyncio.subprocess.PIPE
    assert seen["kwargs"]["stderr"] is not asyncio.subprocess.PIPE


def test_prepare_run_logs_clears_previous_run(tmp_path):
    workdir = tmp_path / "wd"
    step_logs = workdir / "_step_logs"
    step_logs.mkdir(parents=True)
    (step_logs / "ocr.stderr.log").write_text('{"ts": 1, "event": "complete"}\n', encoding="utf-8")
    (workdir / "_mcp_stderr.log").write_text('{"ts": 1, "event": "complete"}\n', encoding="utf-8")

    server._prepare_run_logs(str(workdir))

    assert list(step_logs.glob("*")) == []
    assert (workdir / "_mcp_stderr.log").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_start_clears_logs_before_publishing_running(tmp_path, monkeypatch):
    """A poll racing the start must not see the previous run's last event."""
    workdir = tmp_path / "wd"
    (workdir / "_step_logs").mkdir(parents=True)
    stale = workdir / "_step_logs" / "ocr.stderr.log"
    stale.write_text('{"ts": 1, "event": "complete", "step": "ocr"}\n', encoding="utf-8")

    logs_at_publish: dict = {}
    real_write_status = server._write_status

    def _spy_write_status(job_id, status):
        if status.get("state") == "running":
            logs_at_publish["step_logs"] = [p.name for p in (workdir / "_step_logs").glob("*")]
        return real_write_status(job_id, status)

    monkeypatch.setattr(server, "_write_status", _spy_write_status)

    async def _never_runs(job_id, args):
        return None

    monkeypatch.setattr(server, "_run_pipeline_and_update_status", _never_runs)

    out = json.loads(await server.watch_video_start("x.mp4", workdir=str(workdir)))

    assert out["state"] == "running"
    assert logs_at_publish["step_logs"] == [], (
        "state=running was published while a previous run's step log was still readable")
