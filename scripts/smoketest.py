"""Self-contained smoke test for the watch-video skill.

Generates a synthetic 5s video with ffmpeg (no external fixtures), runs the
full orchestrator pipeline, and asserts the expected artifacts exist and meta.json
fields are sensible. Cleans up on success.

Exits 0 on pass, non-zero on first failure with a clear message. Suitable for
CI or pre-commit.

Usage:
    python smoketest.py [--keep] [--with-audio]

    --keep        do NOT delete the workdir on success (for debugging)
    --with-audio  include a 440 Hz sine on the audio track; otherwise no audio stream
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import require_executable  # noqa: E402


SCRIPTS_DIR = Path(__file__).parent
WATCH_VIDEO = SCRIPTS_DIR / "watch_video.py"


def fail(msg: str) -> None:
    print(f"[smoketest] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[smoketest] {msg}", file=sys.stderr)


def generate_test_video(out_path: Path, with_audio: bool) -> None:
    ffmpeg = require_executable("ffmpeg")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-t", "5", "-pix_fmt", "yuv420p", str(out_path)]
    info(f"generating test video at {out_path} (with_audio={with_audio})")
    subprocess.run(cmd, check=True)


def run_watch(video: Path, workdir: Path) -> dict:
    info(f"running watch_video.py {video.name}")
    proc = subprocess.run(
        [sys.executable, str(WATCH_VIDEO), str(video),
         "--workdir", str(workdir), "--no-audio"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        fail(f"watch_video.py exited {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        fail(f"watch_video.py stdout is not JSON: {e}\n{proc.stdout[:500]}")


def validate(workdir: Path, meta: dict) -> None:
    """Assert the expected artifacts exist and meta.json fields are sensible."""
    # Required files
    must_exist = ["meta.json", "report.md", "frames"]
    for name in must_exist:
        p = workdir / name
        if not p.exists():
            fail(f"missing artifact: {p}")

    # Frame count > 0
    frame_files = list((workdir / "frames").glob("t_*.jpg"))
    if not frame_files:
        fail(f"no frames in {workdir / 'frames'}")
    info(f"  frames: {len(frame_files)} files OK")

    # Schema version
    if meta.get("schema_version") != 2:
        fail(f"unexpected schema_version: {meta.get('schema_version')} (expected 2)")

    # Probe sanity
    probe = meta.get("probe", {})
    if not probe.get("duration"):
        fail("probe.duration missing")
    if abs(probe["duration"] - 5.0) > 0.5:
        fail(f"unexpected duration: {probe['duration']} (expected ~5.0s)")
    info(f"  probe.duration: {probe['duration']:.2f}s OK")

    # Audio should be skipped (we passed --no-audio)
    if meta.get("transcript") is not None:
        fail("transcript should be None (--no-audio was passed)")
    reason = meta.get("skipped_audio_reason")
    if not reason or "no-audio" not in reason:
        fail(f"expected skipped_audio_reason mentioning '--no-audio', got: {reason}")
    info(f"  skipped_audio_reason: {reason!r} OK")

    # Report content sanity
    report_text = (workdir / "report.md").read_text(encoding="utf-8")
    expected_in_report = ["# ", "Evidence bundle", "Timeline"]
    for needle in expected_in_report:
        if needle not in report_text:
            fail(f"report.md missing expected text: {needle!r}")
    info("  report.md content: contains header + timeline OK")

    # Window fields present in meta
    if "window" not in meta:
        fail("meta.window missing")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="keep workdir + test video on success")
    ap.add_argument("--with-audio", action="store_true",
                    help="include 440Hz sine on audio track")
    args = ap.parse_args()

    tmp_dir = Path(tempfile.mkdtemp(prefix="watch-smoketest-"))
    test_video = tmp_dir / "test_input.mp4"
    workdir = tmp_dir / "workdir"

    try:
        generate_test_video(test_video, args.with_audio)
        meta = run_watch(test_video, workdir)
        validate(workdir, meta)
    except SystemExit:
        info(f"keeping artifacts for debug at: {tmp_dir}")
        raise
    except Exception as e:
        info(f"unexpected exception: {e}")
        info(f"artifacts at: {tmp_dir}")
        raise

    print("[smoketest] PASS")
    if not args.keep:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        info(f"artifacts kept at: {tmp_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
