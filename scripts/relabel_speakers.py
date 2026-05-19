"""Relabel anonymous speakers (S0 / S1 / ...) in a watch-video workdir
with real names provided by the caller.

This is the v2.3.1 follow-on to v2.3.0's Deepgram diarization. After a
`--whisper deepgram` run, transcripts and `speakers.json` carry anonymous
speaker ids (`S0`, `S1`, ...). This script rewrites those tags with
human-meaningful names, atomically, in place.

Designed to be agent-friendly: the typical caller is the Claude Code skill
agent after it has inspected `speakers.json` and inferred names from the
first-utterance text (intros, "I'm Joe..." patterns, the host addressing
the guest by name, etc.).

Inputs:
    <workdir>           the same workdir watch_video.py produced
    --names "S0=Joe,S1=Naval"           (comma-separated key=value)
    --names-json '{"S0":"Joe","S1":"Naval"}'   (full JSON)

Either input form is accepted. `--names-json` wins if both are given. Use
JSON when names contain commas (e.g. `"Smith, Jr."`).

What gets rewritten (atomically):
    transcript.md       `**S0** (_MM:SS_) ...` -> `**Joe** (_MM:SS_) ...`
    transcript.txt      `[MM:SS] S0: ...`       -> `[MM:SS] Joe: ...`
    speakers.json       adds a `name` field per speaker; `id` preserved
    report.md/.html/.docx   regenerated via report.py if they already
                            existed (otherwise skipped -- nothing stale
                            left behind)

Stdout: one JSON object summarising the rewrite.
Stderr: structured JSON events (start / complete / warning).
Exit codes: see _common.ExitCode.

Usage example:
    python scripts/relabel_speakers.py C:/tmp/watch-podcast \
        --names "S0=Joe Rogan,S1=Naval Ravikant"
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


# Matches `**S0**`, `**S1**`, etc. at the start of a transcript.md paragraph.
# Captures the speaker id so re.sub can substitute the real name.
_MD_SPEAKER_RE = re.compile(r"\*\*(S\d+)\*\*")

# Matches `[MM:SS] S0: ` form used in transcript.txt -- timestamp THEN speaker.
# Capturing the timestamp lets us keep it; we only swap the speaker token.
_TXT_SPEAKER_RE = re.compile(r"^(\[\d{1,2}:\d{2}\])\s+(S\d+):\s+", re.MULTILINE)


def _parse_names_arg(comma_form: str | None, json_form: str | None) -> dict[str, str]:
    """Build the {speaker_id: real_name} map from CLI args.

    --names-json wins if both are given. Validates that all keys look like
    S<digits>; lets the caller's typos surface as a clean error instead of
    silently producing a no-op rewrite.
    """
    if json_form:
        try:
            parsed = json.loads(json_form)
        except json.JSONDecodeError as e:
            die(ExitCode.BAD_INPUT, f"--names-json is not valid JSON: {e}")
        if not isinstance(parsed, dict):
            die(ExitCode.BAD_INPUT,
                "--names-json must be a JSON object like "
                '{"S0":"Joe","S1":"Naval"}')
        result = {str(k): str(v) for k, v in parsed.items()}
    elif comma_form:
        result = {}
        for pair in comma_form.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                die(ExitCode.BAD_INPUT,
                    f"--names entry has no '=': {pair!r}. "
                    f"Expected form: 'S0=Joe,S1=Naval'. "
                    f"For names containing commas, use --names-json.")
            sid, _, name = pair.partition("=")
            sid = sid.strip()
            name = name.strip()
            if not sid or not name:
                die(ExitCode.BAD_INPUT,
                    f"--names entry has empty id or name: {pair!r}")
            result[sid] = name
    else:
        die(ExitCode.BAD_INPUT,
            "must pass --names 'S0=Joe,S1=Naval' OR "
            "--names-json '{\"S0\":\"Joe\",\"S1\":\"Naval\"}'")

    for sid in result:
        if not re.fullmatch(r"S\d+", sid):
            die(ExitCode.BAD_INPUT,
                f"speaker id {sid!r} doesn't match the 'S<digits>' format "
                f"used by the diarizer. Did you mean 'S0' instead of '{sid}'?")
    if not result:
        die(ExitCode.BAD_INPUT, "no name mappings provided")
    return result


def _load_speakers(workdir: Path) -> dict:
    """Read speakers.json. Die helpfully if it doesn't exist (the run
    probably didn't use --whisper deepgram)."""
    p = workdir / "speakers.json"
    if not p.exists():
        die(ExitCode.BAD_INPUT,
            f"speakers.json not found at {p}. The workdir was probably "
            f"not produced by --whisper deepgram (other providers don't "
            f"do diarization). Re-run watch_video.py with --whisper "
            f"deepgram to generate it.")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(ExitCode.IO_FAIL, f"{p} is not valid JSON: {e}")


def _validate_ids_against_speakers(name_map: dict[str, str],
                                   speakers_data: dict) -> list[str]:
    """Ensure every speaker id in the name map actually appears in
    speakers.json. Returns the list of known ids for the warning message.
    Doesn't die: the user might pass S2 when only S0/S1 exist; we warn
    and proceed with the matched ids."""
    known_ids = {s["id"] for s in speakers_data.get("speakers", [])}
    unknown = set(name_map.keys()) - known_ids
    if unknown:
        emit("warning", step="relabel",
             msg=f"name map references unknown speaker ids "
                 f"{sorted(unknown)} that aren't in speakers.json "
                 f"(known: {sorted(known_ids)}). These will be ignored.",
             unknown_ids=sorted(unknown),
             known_ids=sorted(known_ids))
    return sorted(known_ids)


def _rewrite_text_atomic(path: Path, transform) -> bool:
    """Read path, apply transform(str) -> str, write back atomically.
    Returns True if any substitution happened (transform returned different
    text), False otherwise. Returns False if the file doesn't exist
    (transcribe step may have been skipped on a silent video)."""
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8")
    rewritten = transform(original)
    if rewritten == original:
        return False
    staging = atomic_path(path)
    staging.write_text(rewritten, encoding="utf-8")
    finalize(staging, path)
    return True


def _make_md_transform(name_map: dict[str, str]):
    """Build a callable that rewrites **S0** -> **Joe** for known ids only."""
    def _sub(match: re.Match) -> str:
        sid = match.group(1)
        name = name_map.get(sid)
        return f"**{name}**" if name else match.group(0)
    return lambda text: _MD_SPEAKER_RE.sub(_sub, text)


def _make_txt_transform(name_map: dict[str, str]):
    """Build a callable that rewrites '[00:15] S0: ...' -> '[00:15] Joe: ...'
    while preserving the timestamp and the trailing text."""
    def _sub(match: re.Match) -> str:
        ts = match.group(1)
        sid = match.group(2)
        name = name_map.get(sid)
        if not name:
            return match.group(0)
        return f"{ts} {name}: "
    return lambda text: _TXT_SPEAKER_RE.sub(_sub, text)


def _update_speakers_json(workdir: Path, speakers_data: dict,
                          name_map: dict[str, str]) -> dict:
    """Add a `name` field per speaker for those we have names for.
    Preserves `id` so callers can still cross-reference the original
    anonymous label. Returns the new structure."""
    new_speakers = []
    for s in speakers_data.get("speakers", []):
        s = dict(s)  # shallow copy; don't mutate caller's dict
        if s["id"] in name_map:
            s["name"] = name_map[s["id"]]
        new_speakers.append(s)
    new_data = {**speakers_data, "speakers": new_speakers,
                "relabeled_at": int(time.time())}
    p = workdir / "speakers.json"
    staging = atomic_path(p)
    staging.write_text(json.dumps(new_data, indent=2), encoding="utf-8")
    finalize(staging, p)
    return new_data


def _regenerate_report_if_present(workdir: Path) -> dict:
    """If a report exists in workdir, regenerate it so its content reflects
    the relabeled transcript. Cheaper than re-running the whole pipeline:
    just calls report.py against the same workdir. Skips if no report
    exists (nothing to keep in sync)."""
    report_md = workdir / "report.md"
    if not report_md.exists():
        return {"regenerated": False, "reason": "no report.md in workdir"}

    emit("start", step="regenerate_report",
         reason="speakers were relabeled; transcript-derived report content "
                "would otherwise be stale")
    t0 = time.time()

    report_script = Path(__file__).parent / "report.py"
    cmd = [sys.executable, str(report_script), str(workdir)]
    # Mirror whichever report formats already exist on disk.
    if not (workdir / "report.html").exists():
        cmd.append("--no-html")
    if not (workdir / "report.docx").exists():
        cmd.append("--no-docx")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        emit("warning", step="regenerate_report",
             msg=f"report.py exited {proc.returncode}; transcript is "
                 f"relabeled but report files may be stale",
             stderr_tail=(proc.stderr or "").strip()[-200:])
        return {"regenerated": False, "exit_code": proc.returncode,
                "stderr_tail": (proc.stderr or "").strip()[-200:]}

    emit("complete", step="regenerate_report",
         duration_seconds=round(time.time() - t0, 2))
    return {
        "regenerated": True,
        "duration_seconds": round(time.time() - t0, 2),
        "report_md": str(report_md),
        "report_html": (str(workdir / "report.html")
                        if (workdir / "report.html").exists() else None),
        "report_docx": (str(workdir / "report.docx")
                        if (workdir / "report.docx").exists() else None),
    }


def relabel(workdir: Path, name_map: dict[str, str]) -> dict[str, Any]:
    """Pure function for in-process invocation (mirrors the run_inproc
    pattern used by probe/frames/etc. for the v2.1.0 in-proc refactor).

    Returns a result dict; doesn't print/exit. The caller (main() below
    or another orchestrator) is responsible for surfacing the result.
    """
    if not workdir.is_dir():
        die(ExitCode.BAD_INPUT, f"workdir is not a directory: {workdir}")

    speakers_data = _load_speakers(workdir)
    known_ids = _validate_ids_against_speakers(name_map, speakers_data)

    emit("start", step="relabel",
         workdir=str(workdir),
         name_map={k: v for k, v in name_map.items() if k in known_ids},
         requested_ids=sorted(name_map.keys()),
         known_ids=known_ids)
    t0 = time.time()

    md_changed = _rewrite_text_atomic(
        workdir / "transcript.md", _make_md_transform(name_map))
    txt_changed = _rewrite_text_atomic(
        workdir / "transcript.txt", _make_txt_transform(name_map))
    updated_speakers = _update_speakers_json(workdir, speakers_data, name_map)
    report_result = _regenerate_report_if_present(workdir)

    elapsed = round(time.time() - t0, 2)
    emit("complete", step="relabel",
         duration_seconds=elapsed,
         transcript_md_rewritten=md_changed,
         transcript_txt_rewritten=txt_changed,
         report_regenerated=report_result.get("regenerated", False))

    return {
        "workdir": str(workdir),
        "applied_name_map": {k: v for k, v in name_map.items()
                             if k in known_ids},
        "ignored_unknown_ids": sorted(set(name_map.keys()) - set(known_ids)),
        "transcript_md_rewritten": md_changed,
        "transcript_txt_rewritten": txt_changed,
        "speakers_json": str(workdir / "speakers.json"),
        "speakers": updated_speakers["speakers"],
        "report": report_result,
        "elapsed_seconds": elapsed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Relabel S0/S1/... speaker tags in a workdir with real names.")
    ap.add_argument("workdir",
                    help="watch-video workdir produced by --whisper deepgram")
    ap.add_argument("--names", default=None,
                    help="Comma-separated speaker map: 'S0=Joe,S1=Naval'. "
                         "For names containing commas, use --names-json instead.")
    ap.add_argument("--names-json", default=None,
                    help='JSON object form: \'{"S0":"Joe","S1":"Naval"}\'. '
                         "Wins if both --names and --names-json are passed.")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    name_map = _parse_names_arg(args.names, args.names_json)
    result = relabel(workdir, name_map)
    print(json.dumps(result, indent=2))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
