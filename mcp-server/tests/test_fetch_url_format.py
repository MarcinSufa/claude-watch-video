"""Format selection for scripts/fetch.py URL mode.

YouTube now serves many videos only as separate video and audio streams, with
no pre-muxed file. These tests run yt-dlp's real format selection (no network)
against fixture format lists shaped like YouTube's, using the exact options
fetch.py passes.

Framework matches the rest of mcp-server/tests: pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import fetch  # noqa: E402

yt_dlp = pytest.importorskip("yt_dlp")


def _video(format_id: str, height: int, vcodec: str, ext: str = "mp4") -> dict:
    return {"format_id": format_id, "url": f"https://example.invalid/{format_id}",
            "ext": ext, "protocol": "https", "height": height,
            "width": height * 16 // 9, "vcodec": vcodec, "acodec": "none"}


def _audio(format_id: str, acodec: str, ext: str,
           language: str | None = None, language_preference: int = -1) -> dict:
    return {"format_id": format_id, "url": f"https://example.invalid/{format_id}",
            "ext": ext, "protocol": "https", "vcodec": "none", "acodec": acodec,
            "language": language, "language_preference": language_preference}


SPLIT_ONLY = [
    _video("134", 360, "avc1.4d401e"),
    _video("396", 360, "av01.0.01M.08"),
    _video("298", 720, "avc1.4d4020"),
    _video("398", 720, "av01.0.08M.08"),
    _video("302", 720, "vp9", ext="webm"),
    _video("299", 1080, "avc1.64002a"),
    _video("401", 2160, "av01.0.13M.08"),
    _audio("140", "mp4a.40.2", "m4a"),
    _audio("251", "opus", "webm"),
]

MUXED_ONLY = [
    {"format_id": "18", "url": "https://example.invalid/18", "ext": "mp4",
     "protocol": "https", "height": 360, "width": 640,
     "vcodec": "avc1.42001E", "acodec": "mp4a.40.2"},
]


def _select(formats: list[dict]) -> list[str]:
    opts = {**fetch.URL_FORMAT_OPTS, "quiet": True, "no_warnings": True,
            "simulate": True}
    info = {"id": "fixture", "title": "fixture", "extractor": "generic",
            "webpage_url": "https://example.invalid/watch",
            "formats": [dict(f) for f in formats]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        result = ydl.process_ie_result(info, download=False)
    return [f["format_id"] for f in result["requested_formats"]] \
        if result.get("requested_formats") else [result["format_id"]]


def test_split_only_video_downloads_h264_720p_with_m4a_audio():
    assert _select(SPLIT_ONLY) == ["298", "140"]


def test_muxed_only_video_still_downloads():
    assert _select(MUXED_ONLY) == ["18"]


def test_original_language_opus_beats_dubbed_aac():
    formats = [
        _video("298", 720, "avc1.4d4020"),
        _audio("251", "opus", "webm", language="pl", language_preference=10),
        _audio("140-de", "mp4a.40.2", "m4a", language="de", language_preference=-1),
    ]
    assert _select(formats) == ["298", "251"]
