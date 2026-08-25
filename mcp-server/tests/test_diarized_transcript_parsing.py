"""Every consumer of transcript.md must read the diarized paragraph shape.

transcribe.py writes `**S0** (_MM:SS_) text` whenever the provider diarizes
(Deepgram, whisperx), and relabel_speakers.py rewrites S0 to a name. The
paragraph-prefix regex was copied into three files and every copy matched only
the untagged `(_MM:SS_)` shape, so a diarized run silently produced: no frame
protection in dedup, no timeline rows in report.md, and no selectable
timestamps for highlights.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import dedup as dedup_mod  # noqa: E402
import highlights as highlights_mod  # noqa: E402
import report as report_mod  # noqa: E402

PLAIN = "(_00:01_) the tester opens the order form"
DIARIZED = "**S0** (_00:15_) the tester types a date"
RELABELLED = "**Joe Tester** (_01:02_) the save button does nothing"


@pytest.mark.parametrize("paragraph,seconds", [
    (PLAIN, 1.0), (DIARIZED, 15.0), (RELABELLED, 62.0),
])
def test_dedup_protects_the_moment_whatever_the_speaker_shape(paragraph, seconds, tmp_path):
    (tmp_path / "transcript.md").write_text(paragraph + "\n", encoding="utf-8")

    assert dedup_mod.protected_times_from_transcript(tmp_path) == [seconds]


@pytest.mark.parametrize("paragraph,mmss", [
    (PLAIN, "00:01"), (DIARIZED, "00:15"), (RELABELLED, "01:02"),
])
def test_highlights_can_pick_a_moment_whatever_the_speaker_shape(paragraph, mmss):
    assert mmss in highlights_mod._extract_available_timestamps(paragraph)


def test_the_report_timeline_keeps_diarized_paragraphs_and_their_speaker(tmp_path):
    md = tmp_path / "transcript.md"
    md.write_text("\n\n".join([PLAIN, DIARIZED, RELABELLED]) + "\n", encoding="utf-8")

    parsed = report_mod.parse_prose_transcript(md)

    assert [seconds for seconds, _ in parsed] == [1, 15, 62]
    assert parsed[0][1] == "the tester opens the order form"
    assert parsed[1][1].startswith("**S0**")
    assert parsed[2][1].startswith("**Joe Tester**")


def test_a_paragraph_body_spanning_lines_survives(tmp_path):
    md = tmp_path / "transcript.md"
    md.write_text("**S0** (_00:15_) first line\nsecond line\n", encoding="utf-8")

    parsed = report_mod.parse_prose_transcript(md)

    assert parsed[0][1].endswith("second line")


def test_an_inline_txt_style_tag_is_not_read_as_a_paragraph_start():
    """transcript.txt uses `[MM:SS] S0: text`; unanchoring the regex to reach
    the bold shape would start matching those mid-line."""
    assert highlights_mod._extract_available_timestamps(
        "some prose mentioning (_00:30_) inline") == set()
