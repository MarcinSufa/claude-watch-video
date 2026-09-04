"""_spawn_script must redirect child stdio to files, never to PIPE.

The one-shot tools (fetch_jira_attachment, post_to_jira, pick_highlights)
still went through the PIPE + asyncio-pump path after the blocking-tool fix,
and it wedges the same way under an MCP host: measured on Windows against
a 65 KB Jira attachment, the call never returned in 1800 s (twice) while
the child sat at 0.02 s of CPU, and the same script from a shell finished in
2.0 s. With log files the same call returns in 2.7 s.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


class _FakeProc:
    def __init__(self, rc: int = 0, on_wait=None) -> None:
        self.returncode: int | None = None
        self._rc = rc
        self._on_wait = on_wait
        self.killed = False

    async def wait(self) -> int:
        if self._on_wait is not None:
            await self._on_wait()
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_child_stdio_goes_to_files_not_pipes(monkeypatch):
    seen: dict = {}

    async def _fake_exec(*argv, **kwargs):
        seen["kwargs"] = kwargs
        # Whatever the child would have written must land in the caller's file.
        kwargs["stdout"].write(b'{"ok": true}\n')
        kwargs["stderr"].write(b'{"event": "complete"}\n')
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    rc, stdout, stderr = await server._spawn_script("fetch_attachment.py", "PROJ-1234")

    kwargs = seen["kwargs"]
    assert kwargs["stdout"] is not asyncio.subprocess.PIPE
    assert kwargs["stderr"] is not asyncio.subprocess.PIPE
    assert hasattr(kwargs["stdout"], "fileno"), "stdout must be a real file object"
    assert hasattr(kwargs["stderr"], "fileno"), "stderr must be a real file object"
    # The host's JSON-RPC pipe is this server's stdin; a child must not inherit it.
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert rc == 0
    assert stdout == '{"ok": true}\n'
    assert stderr == '{"event": "complete"}\n'


@pytest.mark.asyncio
async def test_cancelled_call_kills_the_child(monkeypatch):
    procs: list[_FakeProc] = []

    async def _fake_exec(*argv, **kwargs):
        async def _hang():
            await asyncio.sleep(3600)
        proc = _FakeProc(on_wait=_hang)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    task = asyncio.create_task(server._spawn_script("fetch_attachment.py", "PROJ-1234"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert procs and procs[0].killed, "an aborted tool call must not leave the child running"


@pytest.mark.asyncio
async def test_temp_log_dir_is_cleaned_up(monkeypatch):
    seen: dict = {}

    async def _fake_exec(*argv, **kwargs):
        seen["dir"] = Path(kwargs["stdout"].name).parent
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await server._spawn_script("fetch_attachment.py", "PROJ-1234")

    assert not seen["dir"].exists()
