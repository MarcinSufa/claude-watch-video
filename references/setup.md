# Prerequisites

Read when a run reports a missing dependency (exit 3, or a `warnings` entry naming ffmpeg, faster-whisper, yt-dlp, Pillow or Tesseract).

## Prereqs (one-time)

- **ffmpeg** on PATH: `sudo apt install ffmpeg` (Debian/Ubuntu; `dnf`/`pacman` equivalent) or `brew install ffmpeg`. Windows: `winget install Gyan.FFmpeg`.
- **Python 3.10+** as `python3`. (Windows: `py`; `python` there is often the Microsoft Store alias.)
- **All Python deps with tested pins in one go:** `python3 -m pip install -e "${CLAUDE_SKILL_DIR}/mcp-server[full]"` (use a virtualenv on distros that block `pip --user`). Or pick per feature:
- **faster-whisper**: `python3 -m pip install --user faster-whisper 'av<19'` (PyAV 19 breaks faster-whisper 1.2.x with `TypeError: ... 'metadata_errors'`)
- **yt-dlp**: `python3 -m pip install --user yt-dlp` (for URL mode)
- **Pillow + imagehash**: `python3 -m pip install --user Pillow imagehash` (for `--dedup`)
- **Tesseract + pytesseract** (for `--ocr`):
  - Linux: `sudo apt install tesseract-ocr` (or `dnf`/`pacman` equivalent)
  - macOS: `brew install tesseract`
  - Windows: `winget install UB-Mannheim.TesseractOCR`
  - Plus: `python3 -m pip install --user pytesseract Pillow`
  - Non-English languages: install the matching tessdata pack (Linux: `sudo apt install tesseract-ocr-<code>`; Windows: rerun installer with language options)

First whisper run downloads the model (~250 MB for `small`/`small.en`) to `~/.cache/huggingface/hub/`. Cached after that.
