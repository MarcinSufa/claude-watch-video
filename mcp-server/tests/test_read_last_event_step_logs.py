"""_read_last_event must prefer a live _step_logs event over a stale
_mcp_stderr.log one.

_run_step_via_log_files (scripts/watch_video.py) only forwards a step's
events to _mcp_stderr.log AFTER the step exits, so during a long step (OCR
on many frames) that file still shows the PREVIOUS step's "complete" event.
The live events sit in _step_logs/<step>.stderr.log the whole time the step
runs; without also reading it, watch_video_status looks stuck for the
duration of that step even though work is progressing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def test_prefers_newer_event_from_step_logs(tmp_path):
    (tmp_path / "_mcp_stderr.log").write_text(
        json.dumps({"ts": 1.0, "event": "complete", "step": "frames"}) + "\n",
        encoding="utf-8")

    step_logs = tmp_path / "_step_logs"
    step_logs.mkdir()
    (step_logs / "ocr.stderr.log").write_text(
        json.dumps({"ts": 2.0, "event": "progress", "step": "ocr"}) + "\n",
        encoding="utf-8")

    event = server._read_last_event(str(tmp_path))
    assert event == {"ts": 2.0, "event": "progress", "step": "ocr"}


def test_skips_non_json_lines_in_step_logs(tmp_path):
    (tmp_path / "_mcp_stderr.log").write_text(
        json.dumps({"ts": 1.0, "event": "complete", "step": "frames"}) + "\n",
        encoding="utf-8")

    step_logs = tmp_path / "_step_logs"
    step_logs.mkdir()
    (step_logs / "ocr.stderr.log").write_text(
        "DeprecationWarning: something something\n"
        + json.dumps({"ts": 2.0, "event": "progress", "step": "ocr"}) + "\n",
        encoding="utf-8")

    event = server._read_last_event(str(tmp_path))
    assert event == {"ts": 2.0, "event": "progress", "step": "ocr"}


def test_missing_step_logs_dir_falls_back_to_mcp_stderr(tmp_path):
    (tmp_path / "_mcp_stderr.log").write_text(
        json.dumps({"ts": 1.0, "event": "complete", "step": "frames"}) + "\n",
        encoding="utf-8")

    event = server._read_last_event(str(tmp_path))
    assert event == {"ts": 1.0, "event": "complete", "step": "frames"}
