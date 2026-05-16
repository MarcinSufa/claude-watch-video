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


DEFAULT_PROVIDER = "anthropic"
PROVIDER_DEFAULT_MODEL = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "groq":      "llama-3.1-70b-versatile",
}
PROVIDER_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "groq":      "GROQ_API_KEY",
}
PROVIDER_CREDS_FIELD = {
    "anthropic": "anthropic_api_key",
    "openai":    "openai_api_key",
    "groq":      "groq_api_key",
}
# Groq exposes an OpenAI-compatible Chat Completions endpoint; we use the
# openai SDK with a base_url override to talk to it (avoids a separate dep).
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Back-compat alias -- earlier versions exported DEFAULT_MODEL referring to
# the Anthropic default. Keep the name working for any external callers.
DEFAULT_MODEL = PROVIDER_DEFAULT_MODEL["anthropic"]
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


def _load_api_key(provider: str, explicit: str | None, creds_path: Path) -> str:
    env_var = PROVIDER_ENV_VAR[provider]
    creds_field = PROVIDER_CREDS_FIELD[provider]
    if explicit:
        return explicit
    if os.environ.get(env_var):
        return os.environ[env_var]
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(ExitCode.BAD_INPUT, f"{creds_path} is not valid JSON: {e}")
        key = creds.get(creds_field)
        if key:
            return key
    die(
        ExitCode.AUTH_FAIL,
        f"No {provider} API key found. Set one of:\n"
        f"  - env var {env_var}\n"
        f"  - '{creds_field}' field in {creds_path}\n"
        "  - --anthropic-api-key flag (one-shot, not persisted)",
        provider=provider,
        env_var=env_var,
        creds_path=str(creds_path),
    )


# Back-compat alias -- some callers still import _load_anthropic_key directly.
def _load_anthropic_key(explicit: str | None, creds_path: Path) -> str:
    return _load_api_key("anthropic", explicit, creds_path)


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


def _call_anthropic(full_prompt: str, model: str, api_key: str) -> tuple[str, int | None, int | None]:
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "anthropic SDK not installed. Run: pip install --user anthropic",
            dependency="anthropic")
    client = anthropic.Anthropic(api_key=api_key)
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
    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    usage = getattr(response, "usage", None)
    return text, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def _call_openai_compatible(full_prompt: str, model: str, api_key: str,
                            base_url: str | None) -> tuple[str, int | None, int | None]:
    """Used for both 'openai' (no base_url) and 'groq' (base_url override).
    Both expose the same OpenAI Chat Completions shape."""
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
        from openai import APIStatusError, APIConnectionError  # type: ignore[import-not-found]
    except ImportError:
        die(ExitCode.MISSING_DEP,
            "openai SDK not installed. Run: pip install --user openai",
            dependency="openai")
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": full_prompt}],
        )
    except APIStatusError as e:
        if getattr(e, "status_code", None) == 401:
            die(ExitCode.AUTH_FAIL, f"API auth failed: {e}")
        die(ExitCode.IO_FAIL, f"API error: {e}")
    except APIConnectionError as e:
        die(ExitCode.TIMEOUT, f"network error: {e}")
    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    return text, (getattr(usage, "prompt_tokens", None) if usage else None), \
                 (getattr(usage, "completion_tokens", None) if usage else None)


def pick_highlights(workdir: Path, prompt: str, max_n: int, model: str,
                    api_key: str, provider: str = "anthropic") -> dict:
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

    full_prompt = PROMPT_TEMPLATE.format(
        user_request=prompt.strip(),
        transcript=transcript_text,
        max_n=max_n,
    )

    emit("start", step="highlights",
         provider=provider, model=model, max_n=max_n,
         prompt_chars=len(prompt),
         transcript_chars=len(transcript_text))
    t0 = time.time()
    if provider == "anthropic":
        response_text, tokens_in, tokens_out = _call_anthropic(full_prompt, model, api_key)
    elif provider == "openai":
        response_text, tokens_in, tokens_out = _call_openai_compatible(
            full_prompt, model, api_key, base_url=None)
    elif provider == "groq":
        response_text, tokens_in, tokens_out = _call_openai_compatible(
            full_prompt, model, api_key, base_url=GROQ_BASE_URL)
    else:
        die(ExitCode.BAD_INPUT, f"unknown highlights provider: {provider}")

    elapsed = round(time.time() - t0, 2)
    response_text = _strip_code_fences(response_text)

    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError as e:
        die(ExitCode.IO_FAIL,
            f"LLM response was not valid JSON: {e}",
            response_tail=response_text[-300:])
    if not isinstance(raw, list):
        die(ExitCode.IO_FAIL, "LLM response was not a JSON array")

    validated = _validate_highlights(raw, available_ts)

    result = {
        "prompt": prompt,
        "provider": provider,
        "model": model,
        "max_n": max_n,
        "elapsed_seconds": elapsed,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "highlights": validated,
        "skipped_by_validator": len(raw) - len(validated),
    }
    emit("complete", step="highlights",
         duration_seconds=elapsed,
         picked=len(validated),
         skipped_by_validator=result["skipped_by_validator"],
         tokens_input=tokens_in,
         tokens_output=tokens_out)
    return result


def _ts_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0.0


def _nearest_frame(target_seconds: float,
                   timestamps_by_frame: dict[str, float]) -> str | None:
    """Return the frame filename whose timestamp is closest to target_seconds."""
    if not timestamps_by_frame:
        return None
    return min(timestamps_by_frame.items(),
               key=lambda kv: abs(float(kv[1]) - target_seconds))[0]


def _find_transcript_excerpt(transcript_md: str, timestamp: str) -> str:
    """Look up the prose paragraph in transcript.md whose timestamp matches.

    Tries both zero-padded ('02:41') and non-padded ('2:41') minute forms
    since different invocations may store timestamps slightly differently.
    Stops at the next paragraph header so excerpts don't bleed into the next.
    """
    try:
        mm, ss = timestamp.split(":")
    except ValueError:
        return ""
    candidates = []
    try:
        mm_int = int(mm)
        candidates.append(f"{mm_int:02d}:{ss}")
        candidates.append(f"{mm_int}:{ss}")
    except ValueError:
        candidates.append(timestamp)
    for ts in candidates:
        pat = re.compile(
            rf"^\(_{re.escape(ts)}_\)\s+(.+?)(?=\n\(_\d+:\d+_\)|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        m = pat.search(transcript_md)
        if m:
            return m.group(1).strip().replace("\n", " ")
    return ""


def render_highlights_md(workdir: Path, info: dict) -> str:
    """Render the highlights as a viewable Markdown report.

    Format: header (video metadata + the user's prompt) + one section per
    picked moment, each with timestamp heading, frame thumbnail, "why this
    matters" reason, and transcript excerpt for context.
    """
    meta_path = workdir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    video = meta.get("video") or {}
    issue_key = video.get("issue_key")
    if issue_key:
        page_title = f"Highlights — {issue_key} ({video.get('issue_summary', '')})".rstrip(" -()")
    elif video.get("title"):
        page_title = f"Highlights — {video['title']}"
    else:
        page_title = "Highlights"

    timestamps_by_frame: dict[str, float] = (
        meta.get("frames") or {}).get("timestamps_by_frame", {}) or {}
    transcript_md = ""
    transcript_path = workdir / "transcript.md"
    if transcript_path.exists():
        transcript_md = transcript_path.read_text(encoding="utf-8")

    lines = [
        f"# {page_title}",
        "",
        f"> Generated for prompt: **\"{info['prompt']}\"**",
        f"> Model: `{info['model']}` · {len(info['highlights'])} moments picked"
        f" of max {info['max_n']}",
        "",
    ]
    if not info["highlights"]:
        lines.append("_No moments matched the prompt._")
        return "\n".join(lines)

    for h in info["highlights"]:
        ts = h["timestamp"]
        reason = h["reason"]
        ts_seconds = _ts_to_seconds(ts)
        frame_name = _nearest_frame(ts_seconds, timestamps_by_frame)
        excerpt = _find_transcript_excerpt(transcript_md, ts)
        lines.append(f"## {ts}")
        lines.append("")
        if frame_name:
            lines.append(f"![{ts}](frames/{frame_name})")
            lines.append("")
        lines.append(f"**Why this matters:** {reason}")
        lines.append("")
        if excerpt:
            lines.append(f"> {excerpt}")
            lines.append("")

    lines.append("---")
    lines.append("_Generated by `/watch-video` skill `highlights` step._")
    return "\n".join(lines)


def render_highlights_html(workdir: Path, info: dict, md_text: str) -> str:
    """Self-contained HTML version of highlights.md with base64 frames.

    Uses the same CSS theme as report.py's render_html for consistency.
    """
    # Lazy import to avoid coupling
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from report import HTML_CSS, _embed_image_as_data_uri, _h  # type: ignore
    except ImportError:
        # Minimal fallback if report.py changes structure
        HTML_CSS = "body { font-family: sans-serif; max-width: 900px; margin: 2em auto; }"
        def _embed_image_as_data_uri(p: Path) -> str:  # type: ignore
            import base64
            return f"data:image/jpeg;base64,{base64.b64encode(p.read_bytes()).decode()}"
        import html as _html
        def _h(text: str) -> str:  # type: ignore
            return _html.escape(text, quote=False)

    meta_path = workdir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    video = meta.get("video") or {}
    if video.get("issue_key"):
        page_title = f"Highlights — {video['issue_key']}"
    elif video.get("title"):
        page_title = f"Highlights — {video['title']}"
    else:
        page_title = "Highlights"

    timestamps_by_frame: dict[str, float] = (
        meta.get("frames") or {}).get("timestamps_by_frame", {}) or {}
    transcript_md = ""
    transcript_path = workdir / "transcript.md"
    if transcript_path.exists():
        transcript_md = transcript_path.read_text(encoding="utf-8")

    parts: list[str] = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>{_h(page_title)}</title>",
        "<style>", HTML_CSS, "</style>",
        "</head>",
        "<body>",
        f"<h1>{_h(page_title)}</h1>",
        "<div class='evidence-banner'>",
        f"<strong>Highlights</strong> generated for prompt: "
        f"<em>\"{_h(info['prompt'])}\"</em><br>",
        f"Model: <code>{_h(info['model'])}</code> · "
        f"{len(info['highlights'])} moments picked of max {info['max_n']}",
        "</div>",
    ]

    if not info["highlights"]:
        parts.append("<p>No moments matched the prompt.</p>")
    else:
        for h in info["highlights"]:
            ts = h["timestamp"]
            reason = h["reason"]
            ts_seconds = _ts_to_seconds(ts)
            frame_name = _nearest_frame(ts_seconds, timestamps_by_frame)
            excerpt = _find_transcript_excerpt(transcript_md, ts)
            parts.append("<section class='timeline-entry'>")
            parts.append(f"<h2>{_h(ts)}</h2>")
            if frame_name:
                frame_path = workdir / "frames" / frame_name
                if frame_path.exists():
                    parts.append(
                        f"<img src='{_embed_image_as_data_uri(frame_path)}' alt='{_h(ts)}'>"
                    )
            parts.append(
                f"<p><strong>Why this matters:</strong> {_h(reason)}</p>"
            )
            if excerpt:
                parts.append(f"<blockquote><p>{_h(excerpt)}</p></blockquote>")
            parts.append("</section>")

    parts.append(
        "<p class='footer'><em>Generated by <code>/watch-video</code> skill "
        "<code>highlights</code> step.</em></p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def write_highlights(workdir: Path, info: dict) -> dict:
    """Persist highlights.json + highlights.md + highlights.html and embed in meta.json.

    Returns a dict with the three written paths.
    """
    # JSON (machine-readable, used by post_to_jira.py to pick moments)
    json_path = workdir / "highlights.json"
    json_staging = atomic_path(json_path)
    json_staging.write_text(json.dumps(info, indent=2), encoding="utf-8")
    finalize(json_staging, json_path)

    # Markdown (paste-ready report)
    md_path = workdir / "highlights.md"
    md_text = render_highlights_md(workdir, info)
    md_staging = atomic_path(md_path)
    md_staging.write_text(md_text, encoding="utf-8")
    finalize(md_staging, md_path)

    # HTML (self-contained, base64 frames -- open in any browser)
    html_path = workdir / "highlights.html"
    html_text = render_highlights_html(workdir, info, md_text)
    html_staging = atomic_path(html_path)
    html_staging.write_text(html_text, encoding="utf-8")
    finalize(html_staging, html_path)

    # Update meta.json
    meta_path = workdir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        meta["highlights"] = {
            "json_path": str(json_path),
            "md_path": str(md_path),
            "html_path": str(html_path),
            "prompt": info["prompt"],
            "model": info["model"],
            "count": len(info["highlights"]),
            "generated_at": int(time.time()),
        }
        meta_staging = atomic_path(meta_path)
        meta_staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        finalize(meta_staging, meta_path)

    return {
        "json_path": str(json_path),
        "md_path": str(md_path),
        "html_path": str(html_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pick the most relevant moments from a transcript using Claude.")
    ap.add_argument("workdir")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT,
                    help="user request driving the selection (default: most informative moments)")
    ap.add_argument("--max-n", type=int, default=DEFAULT_MAX_N,
                    help=f"max highlights to return (default {DEFAULT_MAX_N})")
    ap.add_argument("--provider",
                    choices=("anthropic", "openai", "groq"),
                    default=DEFAULT_PROVIDER,
                    help="LLM provider for picking highlights. 'openai' and "
                         "'groq' use the openai SDK (Groq exposes an "
                         "OpenAI-compatible endpoint). "
                         f"Default: {DEFAULT_PROVIDER}.")
    ap.add_argument("--model", default=None,
                    help="Model id. Defaults vary by provider: "
                         f"anthropic={PROVIDER_DEFAULT_MODEL['anthropic']}, "
                         f"openai={PROVIDER_DEFAULT_MODEL['openai']}, "
                         f"groq={PROVIDER_DEFAULT_MODEL['groq']}.")
    ap.add_argument("--anthropic-api-key", default=None,
                    help="API key for the chosen provider. WARNING: visible "
                         "in shell history; prefer env var or credentials "
                         "file. Flag name kept for back-compat; works for any "
                         "provider.")
    ap.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH),
                    help=f"credentials JSON path (default {DEFAULT_CREDS_PATH})")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.exists():
        die(ExitCode.BAD_INPUT, f"workdir not found: {workdir}")

    model = args.model or PROVIDER_DEFAULT_MODEL[args.provider]
    api_key = _load_api_key(args.provider, args.anthropic_api_key,
                            Path(args.credentials))
    info = pick_highlights(workdir, args.prompt, args.max_n, model, api_key,
                           provider=args.provider)
    paths = write_highlights(workdir, info)
    print(json.dumps({**info, **paths}))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
