"""LLM-driven highlight selection: pick the N most relevant moments from a
video's transcript based on a user prompt.

Replaces the "evenly distributed" heuristic in `post_to_jira.py --style summary`
with a semantic selection. The user describes what they care about
("highlight only bug-related parts", "find every mention of pricing",
"show me the parts about caching") and Claude returns timestamps + reasons.

Reads `<workdir>/transcript.md` (prose paragraphs with original-video
timestamps). Writes `<workdir>/highlights.json` and updates `meta.json`
with a `highlights` block. Stdout: the JSON list of picked moments.

API key resolution (Anthropic):
  1. --anthropic-api-key flag (one-shot, not persisted)
  2. ANTHROPIC_API_KEY env var
  3. anthropic_api_key field in ~/.watch-video/credentials.json

Cost on default model (Haiku 4.5): ~$0.001-0.005 per video transcript.

Usage:
    python highlights.py <workdir> --prompt "highlight only bug-related moments"
    python highlights.py <workdir> --prompt "..." --max-n 8 --model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_N = 5
DEFAULT_PROMPT = (
    "Identify the most informative or distinctive moments -- the parts a "
    "reviewer would actually need to see to understand what this video shows."
)
DEFAULT_CREDS_PATH = Path.home() / ".watch-video" / "credentials.json"

# Matches "(_MM:SS_)" prefix used in transcript.md prose paragraphs.
PARA_TS_RE = re.compile(r"^\(_(\d+):(\d+)_\)\s+", re.MULTILINE)

PROMPT_TEMPLATE = """You are analyzing a narrated video transcript to pick the most relevant moments.

USER REQUEST:
{user_request}

TRANSCRIPT (timestamps are in original-video time, format MM:SS):
{transcript}

TASK:
Identify up to {max_n} moments matching the user's request, in chronological order.

Return ONLY a JSON array (no markdown fence, no explanation). Each object has:
- "timestamp": "MM:SS" exactly matching a timestamp from the transcript
- "reason": one short sentence on why this moment matches the request

If fewer than {max_n} truly match, return fewer. Do not pad.
If nothing matches, return an empty array [].
"""


def _load_anthropic_key(explicit: str | None, creds_path: Path) -> str:
    if explicit:
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(ExitCode.BAD_INPUT, f"{creds_path} is not valid JSON: {e}")
        key = creds.get("anthropic_api_key")
        if key:
            return key
    die(
        ExitCode.AUTH_FAIL,
        "No Anthropic API key found. Set one of:\n"
        "  - env var ANTHROPIC_API_KEY\n"
        f"  - 'anthropic_api_key' field in {creds_path}\n"
        "  - --anthropic-api-key flag (one-shot, not persisted)",
        env_var="ANTHROPIC_API_KEY",
        creds_path=str(creds_path),
    )


def _extract_available_timestamps(transcript_text: str) -> set[str]:
    """Returns the set of MM:SS strings that appear as paragraph markers."""
    found: set[str] = set()
    for m in PARA_TS_RE.finditer(transcript_text):
        mm, ss = int(m.group(1)), int(m.group(2))
        found.add(f"{mm:02d}:{ss:02d}")
        # Also accept M:SS without leading zero
        found.add(f"{mm}:{ss:02d}")
    return found


def _strip_code_fences(text: str) -> str:
    """If the model wraps JSON in ```json ... ```, unwrap it."""
    text = text.strip()
    if text.startswith("```"):
        # Drop first fence line + last fence line
        lines = text.splitlines()
        if lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    return text


def _validate_highlights(raw: list, available_timestamps: set[str]) -> list[dict]:
    """Filter to well-formed entries whose timestamps appear in the transcript."""
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts = item.get("timestamp")
        reason = item.get("reason")
        if not isinstance(ts, str) or not isinstance(reason, str):
            continue
        # Normalize MM:SS (pad to 2 digits)
        m = re.match(r"^(\d{1,2}):(\d{2})$", ts.strip())
        if not m:
            continue
        normalized = f"{int(m.group(1)):02d}:{m.group(2)}"
        if normalized not in available_timestamps and ts not in available_timestamps:
            # Model hallucinated a timestamp; skip
            emit("warning", step="highlights",
                 msg=f"model returned timestamp {ts!r} not present in transcript; skipping")
            continue
        out.append({"timestamp": normalized, "reason": reason.strip()})
    return out


def pick_highlights(workdir: Path, prompt: str, max_n: int, model: str,
                    api_key: str) -> dict:
    transcript_md = workdir / "transcript.md"
    if not transcript_md.exists():
        die(ExitCode.BAD_INPUT,
            f"transcript.md not found at {transcript_md}; run watch_video.py first "
            f"(highlights need a transcript to operate on)")
    transcript_text = transcript_md.read_text(encoding="utf-8")
    available_ts = _extract_available_timestamps(transcript_text)
    if not available_ts:
        die(ExitCode.BAD_INPUT,
            "transcript.md has no timestamped paragraphs; nothing to highlight")

    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "anthropic SDK not installed. Run: pip install --user anthropic",
            dependency="anthropic")

    client = anthropic.Anthropic(api_key=api_key)

    full_prompt = PROMPT_TEMPLATE.format(
        user_request=prompt.strip(),
        transcript=transcript_text,
        max_n=max_n,
    )

    emit("start", step="highlights",
         model=model, max_n=max_n,
         prompt_chars=len(prompt),
         transcript_chars=len(transcript_text))
    t0 = time.time()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": full_prompt}],
        )
    except anthropic.APIStatusError as e:
        if e.status_code == 401:
            die(ExitCode.AUTH_FAIL, f"Anthropic API auth failed: {e}")
        die(ExitCode.IO_FAIL, f"Anthropic API error {e.status_code}: {e}")
    except anthropic.APIConnectionError as e:
        die(ExitCode.TIMEOUT, f"network error reaching Anthropic: {e}")

    elapsed = round(time.time() - t0, 2)
    response_text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            response_text += block.text
    response_text = _strip_code_fences(response_text)

    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError as e:
        die(ExitCode.IO_FAIL,
            f"Claude response was not valid JSON: {e}",
            response_tail=response_text[-300:])
    if not isinstance(raw, list):
        die(ExitCode.IO_FAIL, "Claude response was not a JSON array")

    validated = _validate_highlights(raw, available_ts)

    usage = getattr(response, "usage", None)
    result = {
        "prompt": prompt,
        "model": model,
        "max_n": max_n,
        "elapsed_seconds": elapsed,
        "tokens_input": getattr(usage, "input_tokens", None),
        "tokens_output": getattr(usage, "output_tokens", None),
        "highlights": validated,
        "skipped_by_validator": len(raw) - len(validated),
    }
    emit("complete", step="highlights",
         duration_seconds=elapsed,
         picked=len(validated),
         skipped_by_validator=result["skipped_by_validator"],
         tokens_input=result["tokens_input"],
         tokens_output=result["tokens_output"])
    return result


def write_highlights(workdir: Path, info: dict) -> Path:
    """Persist highlights.json and embed in meta.json."""
    highlights_path = workdir / "highlights.json"
    staging = atomic_path(highlights_path)
    staging.write_text(json.dumps(info, indent=2), encoding="utf-8")
    finalize(staging, highlights_path)

    meta_path = workdir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        meta["highlights"] = {
            "path": str(highlights_path),
            "prompt": info["prompt"],
            "model": info["model"],
            "count": len(info["highlights"]),
            "generated_at": int(time.time()),
        }
        meta_staging = atomic_path(meta_path)
        meta_staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        finalize(meta_staging, meta_path)
    return highlights_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pick the most relevant moments from a transcript using Claude.")
    ap.add_argument("workdir")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT,
                    help="user request driving the selection (default: most informative moments)")
    ap.add_argument("--max-n", type=int, default=DEFAULT_MAX_N,
                    help=f"max highlights to return (default {DEFAULT_MAX_N})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Anthropic model id (default {DEFAULT_MODEL})")
    ap.add_argument("--anthropic-api-key", default=None,
                    help="API key. WARNING: visible in shell history; prefer env "
                         "ANTHROPIC_API_KEY or credentials file.")
    ap.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH),
                    help=f"credentials JSON path (default {DEFAULT_CREDS_PATH})")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        die(ExitCode.BAD_INPUT, f"workdir not found: {workdir}")

    api_key = _load_anthropic_key(args.anthropic_api_key, Path(args.credentials))
    info = pick_highlights(workdir, args.prompt, args.max_n, args.model, api_key)
    write_highlights(workdir, info)
    print(json.dumps(info))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
