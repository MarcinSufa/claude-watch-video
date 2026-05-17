"""MCP protocol test harness for the watch-video server.

This test was missing in v2.0.0 / v2.0.1 / v2.0.2 and is the reason we
shipped broken code three times: every prior "verification" used direct
Python calls or a mock Context object, which bypassed the actual stdio
JSON-RPC roundtrip that real MCP hosts (Claude Desktop, Cursor, etc.)
exercise. Tests that don't go through stdio can't catch stdio-pipe
buffer issues.

This harness spawns the server as a real subprocess and uses the official
mcp.client.stdio.stdio_client to send real JSON-RPC requests over real
pipes. If THIS passes, we have strong evidence the server-to-host
protocol layer works at the same boundary Claude Desktop would exercise.

The polling pattern (watch_video_start + watch_video_status) is the
primary thing under test — that's the v2.1.0 design choice we're locking
in. The blocking watch_video tool is NOT tested here because we already
know it fails on Windows + Claude Desktop and we're not trying to ship
it as a primary path.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest


SERVER_PY = (Path(__file__).resolve().parent.parent / "server.py")
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=O664gH_szoY"  # Claude Code v2.1.142 release notes; ~54s, captions-eligible


def _import_mcp_or_skip() -> None:
    """The 'mcp' package is the SDK. If it's not installed, skip rather than
    fail -- the protocol tests are opt-in / dev-only."""
    try:
        import mcp  # noqa: F401
        from mcp.client.stdio import stdio_client, StdioServerParameters  # noqa: F401
        from mcp.client.session import ClientSession  # noqa: F401
    except ImportError:
        pytest.skip(
            "mcp SDK not installed; install with `pip install mcp` to run "
            "MCP protocol tests.",
            allow_module_level=False,
        )


@pytest.mark.asyncio
async def test_protocol_initialize_and_list_tools(tmp_path):
    """Smoke test: spawn server, initialize the MCP session, list tools.

    Verifies the server starts, responds to MCP handshake, and exposes the
    expected toolset. Fast (~1s); doesn't run the pipeline.
    """
    _import_mcp_or_skip()
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PY)],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}

    expected_minimum = {
        "watch_video",           # deprecated but kept for back-compat (v2.0.x callers)
        "watch_video_start",     # v2.1.0 new
        "watch_video_status",    # v2.1.0 new
        "read_transcript",
        "read_report",
        "read_highlights",
        "pick_highlights",
        "post_to_jira",
    }
    missing = expected_minimum - tool_names
    assert not missing, f"Missing expected tools: {missing}; got {tool_names}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_polling_pattern_end_to_end(tmp_path):
    """End-to-end: start a real pipeline via watch_video_start, poll
    watch_video_status until done, verify the result.

    This is the test that would have caught the v2.0.1 hang: real subprocess,
    real stdio, real JSON-RPC roundtrip. If the protocol layer is broken,
    THIS hangs or fails.

    Marked as `integration` because it requires:
      - Network access to YouTube
      - yt-dlp + ffmpeg installed
      - faster-whisper available (fallback when captions are rate-limited)
    Run with: pytest -m integration. Default `pytest` runs skip it.

    The captions-eligible Claude Code release-notes video typically completes
    in ~5-10 seconds; we give it 60 seconds to be safe across slow machines.
    """
    _import_mcp_or_skip()
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    # tmp_path is per-test, auto-cleaned by pytest; isolates concurrent runs
    # and avoids leaving artifacts in C:\tmp\ or in the repo working dir.
    workdir = str(tmp_path / "polling-test")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PY)],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) Start the job.
            # whisper="auto" not "captions": YouTube's subtitle endpoint
            # rate-limits aggressively (429 / Too Many Requests). When that
            # happens, fetch.py retries without subtitles and the pipeline
            # falls through to local Whisper. Hardcoding "captions" makes
            # this test flaky under YouTube throttling.
            t0 = time.time()
            start_result = await asyncio.wait_for(
                session.call_tool(
                    "watch_video_start",
                    {
                        "input_ref": TEST_VIDEO_URL,
                        "workdir": workdir,
                        "dedup": True,
                        "whisper": "auto",
                    },
                ),
                timeout=10,  # start must return fast (< 100ms in practice)
            )
            start_elapsed = time.time() - t0
            assert start_elapsed < 5, (
                f"watch_video_start took {start_elapsed:.2f}s; should return "
                f"in well under 1s (no stdio pressure)"
            )

            # The result content is a list of TextContent objects.
            start_text = start_result.content[0].text
            start_data = json.loads(start_text)
            assert start_data["state"] == "running"
            job_id = start_data["job_id"]
            assert job_id, "start did not return a job_id"

            # 2) Poll status until done. Max 60 polls of 1s = 60s budget.
            final_status = None
            for attempt in range(60):
                await asyncio.sleep(1)
                status_result = await asyncio.wait_for(
                    session.call_tool(
                        "watch_video_status",
                        {"job_id": job_id},
                    ),
                    timeout=5,  # each poll must return fast
                )
                status_text = status_result.content[0].text
                status = json.loads(status_text)
                if status["state"] in ("done", "failed", "unknown"):
                    final_status = status
                    break
                # Should still be running with elapsed time growing.
                assert status["state"] == "running", (
                    f"Unexpected state on poll {attempt}: {status['state']}"
                )

            assert final_status is not None, (
                "Polling timed out after 60s; pipeline never reached terminal state"
            )
            assert final_status["state"] == "done", (
                f"Pipeline did not succeed: state={final_status['state']}, "
                f"error={final_status.get('error')}"
            )

            # 3) Verify the meta from a successful run.
            meta = final_status["meta"]
            assert meta["transcript"]["provider"] in ("captions", "local", "groq", "openai")
            assert meta["transcript"]["segments"] > 0
            assert meta["workdir"], "meta has no workdir"

            total_elapsed = time.time() - t0
            assert total_elapsed < 60, f"Total wall-clock exceeded 60s: {total_elapsed:.1f}s"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_background_task_is_strongly_referenced(tmp_path):
    """Regression: v2.1.0-rc2 dropped the asyncio.create_task() return value,
    which let the GC reap the awaiter mid-pipeline. Symptom: artifacts produced
    on disk, but _mcp_status.json stuck at state=running forever. Fix: hold a
    strong ref in module-level _background_tasks.

    This is a direct in-process test (not via the MCP protocol harness) because
    GC pressure is what we need to exercise and the harness keeps the event
    loop hot in ways Claude Desktop does not. We:
      1. Call watch_video_start
      2. Aggressively gc.collect() to flush any orphan task
      3. Verify the task is still registered in _background_tasks
      4. Wait for completion and verify it gets discarded on done

    Marked as `integration` for the same reason as the polling test: it
    exercises the real pipeline (network + yt-dlp + ffmpeg + Whisper).
    """
    import gc
    import importlib

    server_mod = importlib.import_module("server")
    workdir = str(tmp_path / "gc-regression-test")

    initial_count = len(server_mod._background_tasks)

    result = await server_mod.watch_video_start(
        input_ref=TEST_VIDEO_URL,
        workdir=workdir,
        dedup=True,
        whisper="auto",  # see comment in test_polling_pattern_end_to_end re: 429
    )
    job = json.loads(result)
    assert job["state"] == "running"

    # Force GC aggressively. A bare `asyncio.create_task(...)` whose return
    # value was dropped would die here. With the strong-ref fix, it survives.
    for _ in range(5):
        gc.collect()

    assert len(server_mod._background_tasks) == initial_count + 1, (
        "Background task is not strongly referenced -- GC would reap it. "
        "Are you missing _background_tasks.add(task)?"
    )

    # Wait for the task to finish and verify it's cleaned up by the done callback.
    deadline = time.time() + 60
    while time.time() < deadline:
        status = server_mod._read_status(workdir)
        if status and status["state"] in ("done", "failed"):
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("pipeline did not finish within 60s")

    assert status["state"] == "done", f"Pipeline did not succeed: {status}"

    # done_callback should have removed the task by now.
    # Allow one event-loop tick for the callback to fire.
    await asyncio.sleep(0.1)
    assert len(server_mod._background_tasks) == initial_count, (
        "Task was not removed from _background_tasks after completion. "
        "Are you missing task.add_done_callback(_background_tasks.discard)?"
    )


@pytest.mark.asyncio
async def test_status_with_unknown_job_id():
    """watch_video_status on a non-existent job_id should return state=unknown,
    not throw or hang."""
    _import_mcp_or_skip()
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PY)],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await asyncio.wait_for(
                session.call_tool(
                    "watch_video_status",
                    {"job_id": r"C:\tmp\definitely-does-not-exist-xyz"},
                ),
                timeout=5,
            )
            status = json.loads(result.content[0].text)
            assert status["state"] == "unknown"
            assert "error" in status
