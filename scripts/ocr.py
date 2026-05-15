"""OCR each kept frame and write <workdir>/ocr.txt for grep-friendly text search.

Runs Tesseract over every frame currently in <workdir>/frames/ (so this is best
placed AFTER dedup in the pipeline -- you only OCR the frames that survived).
Reads meta.json for timestamps_by_frame, writes ocr.txt with [filename @ MM:SS]
headers per frame, and adds an `ocr` block to meta.json.

Why: for UI bug videos, the bug IS on-screen text (e.g. "Unload field shows 10
instead of 90"). Letting Claude grep ocr.txt finds those values without
re-reading every JPEG, which is far cheaper in tokens.

Tesseract is detected in this order:
  1. `tesseract` on PATH
  2. C:\\Program Files\\Tesseract-OCR\\tesseract.exe (UB-Mannheim default on Windows)
  3. /opt/homebrew/bin/tesseract (macOS Homebrew)
  4. die() with MISSING_DEP if none found

Usage:
    python ocr.py <workdir> [--lang eng] [--min-text-length 10]

Stdout: single JSON object: { frames_with_text, frames_total, ocr_path, language }
Stderr: structured events.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


TESSERACT_FALLBACK_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
]


def find_tesseract() -> str | None:
    """Returns explicit path if tesseract is not on PATH; None if it is."""
    if shutil.which("tesseract"):
        return None
    for candidate in TESSERACT_FALLBACK_PATHS:
        if os.path.exists(candidate):
            return candidate
    die(ExitCode.MISSING_DEP,
        "Tesseract not found. Install it first.\n"
        "  Windows: winget install UB-Mannheim.TesseractOCR\n"
        "  macOS:   brew install tesseract\n"
        "  Linux:   apt install tesseract-ocr  (or dnf/pacman equivalent)\n"
        "Also: pip install --user pytesseract Pillow",
        dependency="tesseract")


def load_meta(workdir: Path) -> dict:
    p = workdir / "meta.json"
    if not p.exists():
        die(ExitCode.BAD_INPUT, f"meta.json not found at {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def write_meta(workdir: Path, meta: dict) -> None:
    p = workdir / "meta.json"
    staging = atomic_path(p)
    staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    finalize(staging, p)


def ocr_one(image_path: Path, lang: str, psm: int = 6) -> str:
    """Returns cleaned OCR text (rstripped per line, leading/trailing blanks removed).

    Preprocessing for screen-recording quality:
    - Auto-inverts dark-mode frames (mean grayscale < 128) so Tesseract sees
      its preferred dark-text-on-light-background.
    - Page Segmentation Mode 6 (uniform block of text) outperforms the default
      mode 3 on UI screens in empirical testing. Mode 11 (sparse text) sometimes
      yields more text but at the cost of much more noise.
    """
    import pytesseract
    from PIL import Image, ImageOps

    img = Image.open(image_path)
    # Upscale 2x with high-quality resampling -- Tesseract works much better
    # on larger text (screen recordings have ~10px UI labels at 960p, which is
    # below Tesseract's sweet spot of ~30px-tall glyphs).
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    # Detect dark mode via mean luminance and invert if needed
    gray = img.convert("L")
    pixels = list(gray.getdata())
    mean_luma = sum(pixels) / max(1, len(pixels))
    if mean_luma < 128:
        img = ImageOps.invert(img.convert("RGB"))

    raw = pytesseract.image_to_string(img, lang=lang, config=f"--psm {psm}")
    cleaned = "\n".join(line.rstrip() for line in raw.split("\n"))
    return cleaned.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--lang", default="eng",
                    help="Tesseract language code(s). 'eng' (default), 'pol', 'deu', 'spa', "
                         "or combinations like 'eng+pol'. Each non-English language requires "
                         "its tessdata pack installed (Windows: re-run the installer with "
                         "language packs; Linux: apt install tesseract-ocr-<code>).")
    ap.add_argument("--min-text-length", type=int, default=10,
                    help="frames with fewer than N non-whitespace chars are skipped (default 10)")
    ap.add_argument("--psm", type=int, default=6,
                    help="Tesseract page segmentation mode (default 6 = uniform block, "
                         "best for UI screens; 3 = full auto, 11 = sparse text)")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        die(ExitCode.BAD_INPUT, f"workdir not found: {workdir}")

    # Lazy imports so missing deps emit a clean MISSING_DEP error
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as e:
        die(ExitCode.MISSING_DEP,
            f"missing dependency: {e}. Run: pip install --user pytesseract Pillow",
            dependency=str(e).split()[-1] if e.args else "pytesseract")

    explicit_path = find_tesseract()
    if explicit_path:
        import pytesseract as pt  # type: ignore
        pt.pytesseract.tesseract_cmd = explicit_path

    meta = load_meta(workdir)
    frames_info = meta.get("frames") or {}
    timestamps = frames_info.get("timestamps_by_frame", {}) or {}
    frames_dir = Path(frames_info.get("frames_dir", workdir / "frames"))

    if not timestamps:
        die(ExitCode.BAD_INPUT,
            "no frames found in meta.json -- run frames.py first")

    emit("start", step="ocr", language=args.lang,
         frame_count=len(timestamps),
         tesseract_path=explicit_path or "PATH")
    t0 = time.time()

    PROGRESS_EVERY_N = 5
    sections: list[tuple[str, float, str]] = []
    frames_total = 0
    ordered_names = sorted(timestamps.keys())
    total_planned = len(ordered_names)
    for i, fname in enumerate(ordered_names, 1):
        frame_path = frames_dir / fname
        if not frame_path.exists():
            continue
        frames_total += 1
        try:
            text = ocr_one(frame_path, args.lang, psm=args.psm)
        except Exception as e:
            emit("warning", step="ocr", frame=fname, msg=f"OCR failed: {e}")
            continue
        # Skip frames where OCR found little or no text
        clean_chars = sum(1 for c in text if not c.isspace())
        if clean_chars < args.min_text_length:
            continue
        sections.append((fname, float(timestamps[fname]), text))
        # Emit progress every N frames (and on the final frame)
        if i % PROGRESS_EVERY_N == 0 or i == total_planned:
            emit("progress", step="ocr",
                 done=i, total=total_planned,
                 with_text=len(sections),
                 elapsed_seconds=round(time.time() - t0, 1))

    # Write ocr.txt atomically
    lines: list[str] = []
    for fname, ts, text in sections:
        mm = int(ts // 60)
        ss = int(ts % 60)
        lines.append(f"[{fname} @ {mm:02d}:{ss:02d}]")
        lines.append(text)
        lines.append("")  # blank line between sections
    ocr_path = workdir / "ocr.txt"
    staging = atomic_path(ocr_path)
    staging.write_text("\n".join(lines), encoding="utf-8")
    finalize(staging, ocr_path)

    # Update meta.json
    elapsed = round(time.time() - t0, 2)
    meta["ocr"] = {
        "path": str(ocr_path),
        "frames_total": frames_total,
        "frames_with_text": len(sections),
        "language": args.lang,
        "min_text_length": args.min_text_length,
        "elapsed_seconds": elapsed,
    }
    write_meta(workdir, meta)

    emit("complete", step="ocr",
         frames_total=frames_total,
         frames_with_text=len(sections),
         duration_seconds=elapsed,
         ocr_path=str(ocr_path))

    print(json.dumps({
        "ocr_path": str(ocr_path),
        "frames_with_text": len(sections),
        "frames_total": frames_total,
        "language": args.lang,
        "elapsed_seconds": elapsed,
    }))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
