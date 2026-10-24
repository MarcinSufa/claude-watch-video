"""MCP tools must reach the CLI flags an agent needs, with the CLI's defaults,
and a cancelled call must not leave grandchildren behind.

Before: watch_video_start exposed no lang/model/resolution/no_audio/
highlights_prompt, so through MCP a Polish recording could not be
transcribed as Polish, a CPU box could not pick tiny.en for a long video, and
a frames-only run was impossible. dedup defaulted to True in MCP but is
opt-in (--dedup) on the CLI. A cancel called proc.kill(), which signals only
the orchestrator: ffmpeg/whisper/tesseract kept running and writing into the
workdir while a retry started (ROADMAP residual).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def test_new_flags_map_to_cli_flags():
    args = server._build_pipeline_args(
        "clip.mp4", "/tmp/wd", None, None, False, False, "local", None, None,
        False, False, lang="pl", model="tiny", resolution=1280,
        no_audio=True, highlights_prompt="find the error")
    assert args[:3] == ["clip.mp4", "--workdir", "/tmp/wd"]
    assert "--dedup" not in args
    assert args[args.index("--lang") + 1] == "pl"
    assert args[args.index("--model") + 1] == "tiny"
    assert args[args.index("--resolution") + 1] == "1280"
    assert "--no-audio" in args
    assert args[args.index("--highlights-prompt") + 1] == "find the error"


def test_unset_flags_add_nothing():
    args = server._build_pipeline_args(
        "clip.mp4", "/tmp/wd", None, None, False, False, "auto", None, None,
        False, False)
    assert args == ["clip.mp4", "--workdir", "/tmp/wd"]


@pytest.mark.parametrize("tool", [server.watch_video, server.watch_video_start])
def test_tools_expose_parity_params_and_cli_dedup_default(tool):
    params = inspect.signature(tool).parameters
    for name in ("lang", "model", "resolution", "no_audio", "highlights_prompt"):
        assert name in params, f"{tool.__name__} lacks {name}"
    assert params["dedup"].default is False


@pytest.mark.asyncio
async def test_watch_video_start_passes_new_params(tmp_path, monkeypatch):
    captured: dict = {}

    async def fake_pipeline(workdir: str, args: list[str]) -> None:
        captured["args"] = args

    monkeypatch.setattr(server, "_run_pipeline_and_update_status", fake_pipeline)
    await server.watch_video_start(str(tmp_path / "clip.mp4"),
                                   workdir=str(tmp_path / "wd"),
                                   lang="pl", model="tiny.en", no_audio=True)
    for _ in range(10):
        if "args" in captured:
            break
        await asyncio.sleep(0)
    args = captured["args"]
    assert "--dedup" not in args
    assert args[args.index("--lang") + 1] == "pl"
    assert args[args.index("--model") + 1] == "tiny.en"
    assert "--no-audio" in args


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed-but-unreaped child is a zombie: gone for our purposes.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_cancel_kills_grandchildren(tmp_path, monkeypatch):
    """Real processes: the script spawns a sleeping grandchild (standing in
    for ffmpeg) and records its pid; cancelling the call must kill both."""
    pid_file = tmp_path / "grandchild.pid"
    (tmp_path / "spawner.py").write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(server, "SCRIPTS_DIR", tmp_path)

    task = asyncio.create_task(server._spawn_script("spawner.py"))
    deadline = time.time() + 15
    while not pid_file.exists() or not pid_file.read_text():
        assert time.time() < deadline, "spawner never started its grandchild"
        await asyncio.sleep(0.05)
    grandchild = int(pid_file.read_text())
    assert _alive(grandchild)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    deadline = time.time() + 5
    while _alive(grandchild) and time.time() < deadline:
        await asyncio.sleep(0.05)
    try:
        assert not _alive(grandchild), "grandchild outlived the cancelled call"
    finally:
        if _alive(grandchild):
            os.kill(grandchild, 9)


def test_kill_tree_falls_back_to_kill_without_pid():
    class _Proc:
        killed = False

        def kill(self):
            self.killed = True

    p = _Proc()
    server._kill_tree(p)
    assert p.killed
