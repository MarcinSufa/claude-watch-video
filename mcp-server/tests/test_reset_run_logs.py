"""_reset_run_logs must clear stale _step_logs/*.stderr.log files at the
start of a run.

A step that cache-hits this run never opens its log file, so a stale
<step>.stderr.log from an earlier run in the same workdir survives with its
old "ts". Without clearing it, a status poll right after start -- before the
new orchestrator has emitted anything of its own -- can pick that stale file
as the newest event via _read_last_event and report a step that isn't
actually running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def test_reset_run_logs_deletes_stale_step_logs(tmp_path):
    step_logs = tmp_path / "_step_logs"
    step_logs.mkdir()
    stale = step_logs / "ocr.stderr.log"
    stale.write_text(json.dumps({"ts": 1.0, "event": "complete", "step": "ocr"}),
                      encoding="utf-8")

    server._reset_run_logs(tmp_path)

    assert not stale.exists()
    assert step_logs.is_dir(), "the directory itself should survive, just emptied"


def test_reset_run_logs_tolerates_a_missing_step_logs_dir(tmp_path):
    server._reset_run_logs(tmp_path)  # must not raise


@pytest.mark.asyncio
async def test_run_pipeline_clears_stale_step_logs_before_a_fresh_run(
        tmp_path, monkeypatch):
    """End-to-end through _run_pipeline_and_update_status: a stale event left
    over from a previous run must not be the "newest" one right after a new
    run starts."""
    step_logs = tmp_path / "_step_logs"
    step_logs.mkdir()
    (step_logs / "ocr.stderr.log").write_text(
        json.dumps({"ts": 9999999999.0, "event": "complete", "step": "ocr"}),
        encoding="utf-8")

    class _FakeProc:
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(*args, **kwargs):
        stdout_f = kwargs.get("stdout")
        if stdout_f is not None:
            stdout_f.write(b'{"workdir": "' + str(tmp_path).encode() + b'"}')
        return _FakeProc()
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", _fake_exec)

    await server._run_pipeline_and_update_status(str(tmp_path), ["some/video.mp4"])

    assert not (step_logs / "ocr.stderr.log").exists()
