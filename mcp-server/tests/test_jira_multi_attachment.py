"""A Jira ticket with more than one video must be watchable from MCP.

Measured failure (PROJ-1234, 2026-08-25): the ticket carries 5 video
attachments. `watch_video_start("PROJ-1234")` exited 5 (AMBIGUOUS) and the
status file reported only the stderr tail -- "5 video attachments on
PROJ-1234" -- while the candidates JSON the pipeline printed sat unread in
_mcp_stdout.log. The agent had no way to name a video either: the CLI takes
--attachment-id, the two MCP entry points never exposed it. Net effect: any
ticket with two or more videos was unwatchable through MCP.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


@pytest.mark.asyncio
async def test_start_forwards_the_chosen_attachment(tmp_path, monkeypatch):
    captured: dict = {}

    async def fake_pipeline(workdir: str, args: list[str]) -> None:
        captured["args"] = args

    monkeypatch.setattr(server, "_run_pipeline_and_update_status", fake_pipeline)
    await server.watch_video_start(
        "PROJ-1234", workdir=str(tmp_path), attachment_id="1001",
    )
    for _ in range(10):
        if "args" in captured:
            break
        await asyncio.sleep(0)

    args = captured["args"]
    assert "--attachment-id" in args, args
    assert args[args.index("--attachment-id") + 1] == "1001"


def test_two_videos_on_one_ticket_get_separate_workdirs():
    """Same key, different video: sharing a workdir means sharing
    _mcp_status.json, the cache and the frames of the other video."""
    assert (server._default_workdir("PROJ-1234", "1001")
            != server._default_workdir("PROJ-1234", "1003"))


def test_workdir_without_an_attachment_is_unchanged():
    assert server._default_workdir("PROJ-1234").endswith("watch-proj-1234")


def test_failure_status_carries_the_candidates(tmp_path):
    stdout = (
        'yt-dlp noise on stdout\n'
        '{"ambiguous": true, "issue_key": "PROJ-1234", '
        '"issue_summary": "[UI] New Cost Review Screen", '
        '"candidates": [{"id": "1001", "filename": "2026-08-24.mp4"}, '
        '{"id": "1003", "filename": "2026-08-20.mp4"}], '
        '"hint": "re-run with --attachment-id <id>"}\n'
    )
    payload = server._failure_payload(5, stdout, "some stderr tail")

    assert payload["state"] == "failed"
    ids = [c["id"] for c in payload["ambiguous"]["candidates"]]
    assert ids == ["1001", "1003"]
    assert "attachment_id" in payload["error"], (
        "the agent must be told the parameter that unblocks it, not just that "
        "the run failed")


def test_an_ordinary_failure_reports_no_candidates():
    payload = server._failure_payload(3, "", "ffmpeg not found")
    assert "ambiguous" not in payload
    assert "ffmpeg not found" in payload["error"]


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import watch_video  # noqa: E402


def test_the_cli_default_workdir_separates_two_videos_of_one_ticket():
    """Path A (the skill invokes the CLI directly) had the collision too.

    _default_workdir folds the attachment id into the slug and its docstring
    claims it mirrors the CLI exactly; the CLI helper never took the id, so
    two --attachment-id runs of PROJ-1234 shared c:/tmp/watch-proj-1234 -- one
    meta.json, one frames dir, one cache.
    """
    a = watch_video.default_workdir("jira", "PROJ-1234", "1001")
    b = watch_video.default_workdir("jira", "PROJ-1234", "1002")
    plain = watch_video.default_workdir("jira", "PROJ-1234", None)

    assert a != b
    assert a != plain and b != plain


@pytest.mark.parametrize("attachment_id", [None, "1001"])
def test_mcp_and_cli_agree_on_the_default_workdir(attachment_id):
    assert server._default_workdir("PROJ-1234", attachment_id) == str(
        watch_video.default_workdir("jira", "PROJ-1234", attachment_id))


@pytest.mark.asyncio
async def test_the_blocking_tool_also_reports_the_candidates(monkeypatch):
    """watch_video (deprecated, but SKILL.md still points agents at it) raised
    the stderr tail and dropped stdout, where the candidate list lives."""
    candidates = {
        "ambiguous": True, "issue_key": "PROJ-1234",
        "candidates": [{"id": "1001", "filename": "repro.mp4"},
                       {"id": "1002", "filename": "other.mp4"}],
    }

    async def fake_spawn(script, *args, ctx=None):
        return 5, json.dumps(candidates, indent=2), "5 video attachments on PROJ-1234"

    monkeypatch.setattr(server, "_spawn_script", fake_spawn)

    with pytest.raises(RuntimeError) as e:
        await server.watch_video("PROJ-1234")

    assert "attachment_id" in str(e.value)
    assert "1001" in str(e.value)
