"""Once frames exist, a failing later step must not leave stdout empty.

Measured on 2026-10-24 (fresh install, faster-whisper 1.2.1 + PyAV 19):
transcription died with `TypeError ... 'metadata_errors'`, rc=1 and 0 bytes
on stdout, although frames/, audio.wav and meta.json were already on disk.
`--highlights-prompt` with no API key gave rc=4 (documented as "Atlassian
401") and `--ocr` without Tesseract rc=3, both with an empty stdout. The
agent got nothing to answer from.

The contract now: optional steps (transcribe, dedup, OCR, report,
highlights) become `warnings` in meta.json and the run exits 0; a step that
must fail the run (posting to Jira) still exits non-zero but prints the
partial meta.json with an `error` block.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import watch_video  # noqa: E402


def _fake_pipeline(monkeypatch, tmp_path, *, audio: bool = True):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not a real video")
    workdir = tmp_path / "wd"
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True)
    for i in range(3):
        (frames_dir / f"t_{i:04d}.jpg").write_bytes(b"jpg")

    monkeypatch.setattr(watch_video, "fetch",
                        lambda *a, **k: {"path": str(video)})
    monkeypatch.setattr(watch_video, "probe", lambda *a, **k: {
        "duration": 10.0, "has_audio": audio, "is_silent": False,
        "mean_volume_db": -20.0,
    })
    monkeypatch.setattr(watch_video, "extract_frames", lambda *a, **k: {
        "frame_count": 3, "frames_dir": str(frames_dir),
        "timestamps_by_frame": {}})
    monkeypatch.setattr(watch_video, "report",
                        lambda *a, **k: {"report_path": str(workdir / "report.md")})
    return video, workdir


def _run(monkeypatch, capsys, video, workdir, *extra):
    monkeypatch.setattr(sys, "argv", ["watch_video.py", str(video),
                                      "--workdir", str(workdir), "--no-cache",
                                      *extra])
    rc = watch_video._entry()
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _raise_type_error(*a, **k):
    # What faster-whisper 1.2.1 raises in-process (MCP mode) under PyAV 19.
    raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")


def test_transcription_crash_continues_frames_only(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(watch_video, "transcribe", _raise_type_error)

    rc, meta = _run(monkeypatch, capsys, video, workdir)

    assert rc == 0
    assert meta["transcript"] is None
    assert "transcription failed" in meta["skipped_audio_reason"]
    assert "metadata_errors" in meta["skipped_audio_reason"]
    assert [w["step"] for w in meta["warnings"]] == ["transcribe"]
    assert meta["frames"]["frame_count"] == 3


def test_transcription_child_failure_continues(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path)

    def failing_child(*a, **k):
        raise watch_video.StepFailed("transcribe", 3, "",
                                     "faster_whisper not installed")

    monkeypatch.setattr(watch_video, "transcribe", failing_child)
    rc, meta = _run(monkeypatch, capsys, video, workdir)

    assert rc == 0
    assert "faster_whisper not installed" in meta["skipped_audio_reason"]
    assert meta["warnings"][0]["exit_code"] == 3


def test_highlights_without_key_is_a_warning(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(watch_video, "transcribe",
                        lambda *a, **k: {"transcript_md": "transcript.md"})

    def no_key(*a, **k):
        raise watch_video.StepFailed("highlights", 4, "",
                                     "No anthropic API key found.")

    monkeypatch.setattr(watch_video, "run_highlights", no_key)
    rc, meta = _run(monkeypatch, capsys, video, workdir,
                    "--highlights-prompt", "find the bug")

    assert rc == 0
    assert meta["transcript"] is not None
    (w,) = meta["warnings"]
    assert w["step"] == "highlights" and w["exit_code"] == 4
    assert "API key" in w["msg"]


def test_highlights_prompt_with_no_audio_warns(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(watch_video, "run_highlights",
                        lambda *a, **k: called.append(1))
    rc, meta = _run(monkeypatch, capsys, video, workdir,
                    "--no-audio", "--highlights-prompt", "find the bug")

    assert rc == 0
    assert not called
    (w,) = meta["warnings"]
    assert w["step"] == "highlights"
    assert "--highlights-prompt ignored" in w["msg"]
    assert "--no-audio" in w["msg"]


def test_ocr_without_tesseract_is_a_warning(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path, audio=False)

    def no_tesseract(*a, **k):
        raise watch_video.StepFailed("ocr", 3, "", "tesseract not found")

    monkeypatch.setattr(watch_video, "ocr", no_tesseract)
    rc, meta = _run(monkeypatch, capsys, video, workdir, "--ocr")

    assert rc == 0
    (w,) = meta["warnings"]
    assert w["step"] == "ocr" and "tesseract not found" in w["msg"]
    assert "ocr" not in meta["cache"]["steps"]


def test_hard_failure_after_frames_prints_partial_meta(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path, audio=False)

    def jira_401(*a, **k):
        raise watch_video.StepFailed("post_to_jira", 4, "", "Atlassian 401")

    monkeypatch.setattr(watch_video, "post_to_jira", jira_401)
    rc, meta = _run(monkeypatch, capsys, video, workdir,
                    "--post-to-jira", "--post-to-jira-yes")

    assert rc == 4
    assert meta["error"] == {"exit_code": 4, "step": "post_to_jira",
                             "msg": "Atlassian 401"}
    assert meta["frames"]["frame_count"] == 3
    on_disk = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
    assert on_disk["error"]["step"] == "post_to_jira"


def test_failure_before_frames_keeps_step_stdout(monkeypatch, tmp_path, capsys):
    """AMBIGUOUS from fetch: stdout is the candidates JSON, not a meta."""
    video = tmp_path / "clip.mp4"
    candidates = json.dumps({"ambiguous": True, "candidates": [{"id": "1"}]})

    def ambiguous(*a, **k):
        raise watch_video.StepFailed("fetch", 5, candidates, "2 attachments")

    monkeypatch.setattr(watch_video, "fetch", ambiguous)
    monkeypatch.setattr(sys, "argv", ["watch_video.py", str(video),
                                      "--workdir", str(tmp_path / "wd")])
    rc = watch_video._entry()
    assert rc == 5
    assert json.loads(capsys.readouterr().out)["ambiguous"] is True


def test_error_msg_prefers_error_event():
    lines = ['{"event":"start","step":"ocr"}',
             '{"event":"error","exit_code":3,"msg":"tesseract not found"}',
             ""]
    assert watch_video._error_msg_from_stderr(lines) == "tesseract not found"
    assert watch_video._error_msg_from_stderr(
        ["Traceback (most recent call last):", "TypeError: boom"]) == "TypeError: boom"


def test_rerun_clears_previous_warnings(monkeypatch, tmp_path, capsys):
    video, workdir = _fake_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(watch_video, "transcribe", _raise_type_error)
    _run(monkeypatch, capsys, video, workdir)
    monkeypatch.setattr(watch_video, "transcribe",
                        lambda *a, **k: {"transcript_md": "transcript.md"})
    monkeypatch.setattr(sys, "argv", ["watch_video.py", str(video),
                                      "--workdir", str(workdir)])
    assert watch_video._entry() == 0
    meta = json.loads(capsys.readouterr().out)
    assert meta["warnings"] == []
    assert meta["skipped_audio_reason"] is None


def test_mcp_failure_payload_carries_partial_meta():
    sys.path.insert(0, str(REPO / "mcp-server"))
    import server  # noqa: E402

    partial = {"schema_version": 2, "workdir": "/tmp/watch-x",
               "frames": {"frame_count": 3},
               "error": {"exit_code": 4, "step": "post_to_jira",
                         "msg": "Atlassian 401"}}
    payload = server._failure_payload(4, json.dumps(partial, indent=2), "tail")
    assert payload["state"] == "failed"
    assert payload["meta"]["frames"]["frame_count"] == 3
    assert "post_to_jira" in payload["error"] and "Atlassian 401" in payload["error"]
