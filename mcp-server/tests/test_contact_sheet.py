from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import watch_video  # noqa: E402


@pytest.mark.parametrize("count, expected", [
    (1, (1, 1)),
    (8, (3, 3)),
    (30, (6, 5)),
    (40, (7, 6)),
])
def test_contact_sheet_grid_sizing(count, expected):
    cols, rows = watch_video._contact_sheet_grid(count)
    assert (cols, rows) == expected
    assert cols * rows >= count


def test_contact_sheet_command_uses_given_frame_list(tmp_path):
    frames = [tmp_path / "first scene.jpg", tmp_path / "last.jpg"]
    command, concat_input = watch_video._build_contact_sheet_command(
        frames, tmp_path / "frames.ffconcat", tmp_path / "sheet.jpg",
        "ffmpeg-test")

    assert command[0] == "ffmpeg-test"
    assert command[command.index("-i") + 1] == str(tmp_path / "frames.ffconcat")
    assert command[command.index("-vf") + 1] == "scale=400:-1,tile=2x1:padding=4"
    assert "drawtext" not in " ".join(command)
    assert concat_input.splitlines() == [
        f"file '{frames[0].resolve().as_posix()}'",
        f"file '{frames[1].resolve().as_posix()}'",
    ]
    assert command[-1] == str(tmp_path / "sheet.jpg")


def test_contact_sheet_failure_is_a_warning(monkeypatch, tmp_path, capsys):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not a real video")
    workdir = tmp_path / "wd"
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True)
    for name in ("frame-b.jpg", "frame-a.jpg"):
        (frames_dir / name).write_bytes(b"jpg")

    monkeypatch.setattr(watch_video, "fetch",
                        lambda *a, **k: {"path": str(video)})
    monkeypatch.setattr(watch_video, "probe", lambda *a, **k: {
        "duration": 10.0, "has_audio": False, "is_silent": True,
        "mean_volume_db": None,
    })
    monkeypatch.setattr(watch_video, "extract_frames", lambda *a, **k: {
        "frame_count": 2, "frames_dir": str(frames_dir),
        "timestamps_by_frame": {"frame-b.jpg": 8.0, "frame-a.jpg": 1.0},
    })

    def fail(*args, **kwargs):
        raise watch_video.StepFailed("contact_sheet", 6, "", "ffmpeg failed")

    monkeypatch.setattr(watch_video, "create_contact_sheet", fail)
    monkeypatch.setattr(sys, "argv", [
        "watch_video.py", str(video), "--workdir", str(workdir), "--no-cache",
        "--no-audio", "--no-report", "--contact-sheet",
    ])

    rc = watch_video._entry()
    meta = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert meta["contact_sheet"] is None
    assert meta["frames"]["frame_count"] == 2
    assert meta["warnings"][0]["step"] == "contact_sheet"
    assert meta["warnings"][0]["exit_code"] == 6
    assert "ffmpeg failed" in meta["warnings"][0]["msg"]
