"""Dedup must not run when the pipeline produced no transcript.

Measured failure (PROJ-1234 attachment 1001, 2026-08-25): a silent bug repro.
meta.json reported probe.is_silent true and frames.dedup
{before: 30, after: 8, dropped: 22, kept_by_transcript_protection: 0}. Dedup
has two guards against dropping a frame -- the temporal one (--min-interval,
5.0 s) and the transcript-aware one. With no transcript the second guard is
inert, so 40 s of screen recording collapsed to 8 frames and the date field
the tester typed into was unreadable. The frames had to be re-extracted by
hand with `frames.py --frames 28`.

This is not a threshold to tune. Two consecutive frames of a UI repro differ
by one character in one input, so a perceptual hash is *supposed* to call them
duplicates: on a silent recording dedup removes exactly the signal the run
exists to capture.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import watch_video  # noqa: E402


def _fake_pipeline(monkeypatch, tmp_path, *, audio: str, frame_count: int = 30):
    video = tmp_path / "repro.mp4"
    video.write_bytes(b"not a real video")
    frames_dir = tmp_path / "wd" / "frames"
    frames_dir.mkdir(parents=True)
    for i in range(frame_count):
        (frames_dir / f"t_{i:04d}.jpg").write_bytes(b"jpg")

    calls: list[str] = []

    monkeypatch.setattr(watch_video, "fetch",
                        lambda *a, **k: {"path": str(video)})
    monkeypatch.setattr(watch_video, "probe", lambda *a, **k: {
        "duration": 40.0, "has_audio": audio != "none",
        "is_silent": audio == "silent", "mean_volume_db": -91.0,
    })
    def fake_extract_frames(*a, **k):
        calls.append("frames")
        return {"frame_count": frame_count, "frames_dir": str(frames_dir),
                "timestamps_by_frame": {}}

    monkeypatch.setattr(watch_video, "extract_frames", fake_extract_frames)

    def fake_transcribe(*a, **k):
        calls.append("transcribe")
        return {"text_path": "transcript.txt"}

    def fake_dedup(*a, **k):
        calls.append("dedup")
        return {}

    monkeypatch.setattr(watch_video, "transcribe", fake_transcribe)
    monkeypatch.setattr(watch_video, "smart_dedup", fake_dedup)
    return video, tmp_path / "wd", calls


def _run(monkeypatch, capsys, video, workdir, extra=()):
    monkeypatch.setattr(sys, "argv",
                        ["watch_video.py", str(video), "--workdir", str(workdir),
                         "--dedup", "--no-report", *extra])
    rc = watch_video.main()
    capsys.readouterr()
    return rc


@pytest.mark.parametrize("audio", ["silent", "none"])
def test_dedup_is_skipped_when_there_is_no_transcript(audio, tmp_path, monkeypatch, capsys):
    video, workdir, calls = _fake_pipeline(monkeypatch, tmp_path, audio=audio)

    _run(monkeypatch, capsys, video, workdir)

    assert "dedup" not in calls
    meta = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["transcript"] is None
    assert "dedup" not in meta["cache"]["steps"]


def test_the_skip_says_why_it_skipped(tmp_path, monkeypatch, capsys):
    video, workdir, _ = _fake_pipeline(monkeypatch, tmp_path, audio="silent")

    monkeypatch.setattr(sys, "argv",
                        ["watch_video.py", str(video), "--workdir", str(workdir),
                         "--dedup", "--no-report"])
    watch_video.main()
    events = [json.loads(l) for l in capsys.readouterr().err.splitlines() if l.startswith("{")]

    warnings = [e for e in events
                if e.get("event") == "warning" and e.get("step") == "dedup"]
    assert warnings, "the skip must be announced, not silent"
    assert "silent track" in json.dumps(warnings[0])


def test_dedup_still_runs_when_a_transcript_exists(tmp_path, monkeypatch, capsys):
    video, workdir, calls = _fake_pipeline(monkeypatch, tmp_path, audio="voice")

    _run(monkeypatch, capsys, video, workdir)

    assert calls == ["frames", "transcribe", "dedup"]


def test_no_audio_flag_also_disarms_dedup(tmp_path, monkeypatch, capsys):
    video, workdir, calls = _fake_pipeline(monkeypatch, tmp_path, audio="voice")

    _run(monkeypatch, capsys, video, workdir, extra=("--no-audio",))

    assert calls == ["frames"]


def test_a_deduped_frames_dir_is_not_reused_once_dedup_stops_running(
        tmp_path, monkeypatch, capsys):
    """frames/ is mutated in place, so the cache key must track the real skip.

    Without this, a second run of the same workdir with --no-audio cache-hits
    on the subset the first run's dedup left behind and the skip returns
    nothing."""
    video, workdir, calls = _fake_pipeline(monkeypatch, tmp_path, audio="voice")
    _run(monkeypatch, capsys, video, workdir)
    calls.clear()

    _run(monkeypatch, capsys, video, workdir, extra=("--no-audio",))

    assert "frames" in calls


@pytest.mark.integration
def test_a_real_silent_recording_keeps_every_sampled_frame(tmp_path):
    """End to end on a video ffmpeg builds: 30 near-identical silent frames."""
    video = tmp_path / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=white:s=320x240:d=40",
         "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
         "-t", "40", "-shortest", str(video)],
        check=True,
    )
    workdir = tmp_path / "wd"
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "watch_video.py"), str(video),
         "--workdir", str(workdir), "--frames", "30", "--dedup", "--no-report"],
        check=True, capture_output=True,
    )
    meta = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))

    assert meta["probe"]["is_silent"] is True
    assert meta["transcript"] is None
    assert meta["frames"]["frame_count"] == 30
    assert "dedup" not in meta["frames"]


@pytest.mark.asyncio
async def test_read_transcript_names_the_silence_instead_of_a_missing_file(tmp_path):
    sys.path.insert(0, str(REPO / "mcp-server"))
    import server

    (tmp_path / "meta.json").write_text(json.dumps({
        "transcript": None,
        "skipped_audio_reason": "silent track (mean_volume=-91.0 dB)",
    }), encoding="utf-8")

    with pytest.raises(RuntimeError) as e:
        await server.read_transcript(str(tmp_path))

    assert "silent track" in str(e.value)
    assert "report.md" in str(e.value)


def test_a_transcript_with_no_paragraphs_disarms_dedup_too(tmp_path):
    """`transcript is None` is not the whole hole.

    is_silent is threshold-based and falls back to False when volumedetect
    cannot read the volume, so a recording can carry a transcript that yields
    zero paragraph timestamps. Protection is then exactly as inert as on a
    silent track, and the orchestrator's guard does not see it.
    """
    import dedup as dedup_mod
    from PIL import Image

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for i in range(1, 6):
        Image.new("RGB", (64, 64), (128, 128, 128)).save(frames_dir / f"t_{i:03d}.jpg")
    (tmp_path / "transcript.md").write_text("no paragraph markers here\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "frames": {
            "frame_count": 5, "frames_dir": str(frames_dir),
            "timestamps_by_frame": {f"t_{i:03d}.jpg": float(i) for i in range(1, 6)},
        },
    }), encoding="utf-8")

    dedup_mod.dedup(tmp_path, threshold=5, min_interval=5.0, protect_window=1.5)

    assert len(list(frames_dir.glob("t_*.jpg"))) == 5


@pytest.mark.asyncio
async def test_mcp_can_set_the_frame_budget_the_skip_advises(tmp_path, monkeypatch):
    """The skip tells the caller to bound the budget with --frames N. Through
    MCP that was unreachable: neither tool exposed a frames parameter."""
    sys.path.insert(0, str(REPO / "mcp-server"))
    import server

    captured: dict = {}

    async def fake_pipeline(workdir: str, args: list[str]) -> None:
        captured["args"] = args

    monkeypatch.setattr(server, "_run_pipeline_and_update_status", fake_pipeline)
    await server.watch_video_start("PROJ-1234", workdir=str(tmp_path), frames=28)
    for _ in range(10):
        if "args" in captured:
            break
        await asyncio.sleep(0)

    assert "--frames" in captured["args"]
    assert captured["args"][captured["args"].index("--frames") + 1] == "28"


@pytest.mark.parametrize("paragraph", [
    "(_00:01_) the tester opens the order form",
    "**S0** (_00:01_) the tester opens the order form",
    "**Joe** (_00:01_) the tester opens the order form",
])
def test_dedup_still_drops_duplicates_when_paragraphs_exist(paragraph, tmp_path):
    """Guard the guard: the bail-out must not disable dedup on a real transcript.

    Diarizing providers (Deepgram, whisperx) write `**S0** (_MM:SS_) text`, and
    relabel_speakers.py rewrites S0 to a name. Feeding only the untagged shape
    let the bail-out turn dedup off for every diarized run.
    """
    import dedup as dedup_mod
    from PIL import Image

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for i in range(1, 6):
        Image.new("RGB", (64, 64), (128, 128, 128)).save(frames_dir / f"t_{i:03d}.jpg")
    (tmp_path / "transcript.md").write_text(
        paragraph + "\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "frames": {
            "frame_count": 5, "frames_dir": str(frames_dir),
            "timestamps_by_frame": {f"t_{i:03d}.jpg": float(i) for i in range(1, 6)},
        },
    }), encoding="utf-8")

    dedup_mod.dedup(tmp_path, threshold=5, min_interval=5.0, protect_window=1.5)

    assert len(list(frames_dir.glob("t_*.jpg"))) < 5
