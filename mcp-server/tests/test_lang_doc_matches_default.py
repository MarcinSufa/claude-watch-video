"""An omitted lang must transcribe non-English speech in its own language.

Before: local and whisperx (what whisper='auto' resolves to without an API
key) pinned English with small.en when lang was omitted, while the MCP doc
said "Omit to let the provider detect it". A Polish meeting came back as an
invented English transcript reported as "en (p=1)".
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-server"))
sys.path.insert(0, str(ROOT / "scripts"))
import server  # noqa: E402
import transcribe  # noqa: E402


def _lang_doc(tool) -> str:
    doc = tool.__doc__ or ""
    start = doc.index("lang: ")
    end = doc.index("model: ", start)
    return " ".join(doc[start:end].split())


@pytest.mark.parametrize("provider", ["local", "whisperx"])
@pytest.mark.parametrize("lang_arg", [None, "auto"])
def test_omitted_lang_auto_detects_with_multilingual_model(provider, lang_arg):
    model, lang = transcribe.pick_model_for_provider(provider, None, lang_arg)
    assert lang is None
    assert not model.endswith(".en")


@pytest.mark.parametrize("provider", ["local", "whisperx"])
def test_explicit_english_keeps_english_only_model(provider):
    model, lang = transcribe.pick_model_for_provider(provider, None, "en")
    assert lang == "en"
    assert model.endswith(".en")


def _fake_whisper(monkeypatch, calls):
    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            info = types.SimpleNamespace(language="pl", language_probability=0.99)
            return iter([]), info

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=FakeModel))


def test_local_auto_detect_votes_over_several_windows(monkeypatch, tmp_path):
    calls: list[dict] = []
    _fake_whisper(monkeypatch, calls)
    transcribe.transcribe_local(tmp_path, "small", None)
    assert calls[0]["language"] is None
    assert calls[0]["language_detection_segments"] > 1


def test_local_pinned_language_skips_detection_windows(monkeypatch, tmp_path):
    calls: list[dict] = []
    _fake_whisper(monkeypatch, calls)
    transcribe.transcribe_local(tmp_path, "small", "pl")
    assert calls[0]["language"] == "pl"
    assert "language_detection_segments" not in calls[0]


def test_lang_doc_states_auto_detect_default():
    doc = _lang_doc(server.watch_video)
    assert "small.en" not in doc
    assert "detect" in doc
    assert "'en'" in doc


def test_whisperx_auto_detect_votes_over_several_windows(monkeypatch, tmp_path):
    calls: list[dict] = []

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("stop after the call under test")

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio",
                        types.SimpleNamespace(Pipeline=object))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))
    (tmp_path / "audio.wav").write_bytes(b"")
    with pytest.raises(SystemExit):
        transcribe.transcribe_whisperx(tmp_path, "small", None, "hf")
    assert calls[0]["language"] is None
    assert calls[0]["language_detection_segments"] > 1


def test_transcript_cache_key_changes_with_resolved_default():
    import watch_video
    old_style = {"whisper": "local", "model": None, "lang": None}
    keyed = watch_video.transcribe_fp_inputs(
        "fp", None, None, "local", None, None, None)
    assert keyed["resolved_model"] == "small"
    assert keyed["resolved_lang"] is None
    assert {k: keyed[k] for k in old_style} == old_style
    english = watch_video.transcribe_fp_inputs(
        "fp", None, None, "local", None, "en", None)
    assert english["resolved_model"] == "small.en"
