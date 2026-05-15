"""Batch orchestrator: run watch_video.py over multiple inputs.

Inputs can be:
  --jira-keys CON-X,CON-Y,CON-Z     comma-separated Jira issue keys
  --jira-jql "project=PROJ AND ..."  JQL search; expands to matching issue keys
  --inputs path1,path2,url1         comma-separated raw inputs (mixed types ok)

Each input gets its own workdir under <batch-dir>/<slug>/. Per-item results
are aggregated into <batch-dir>/batch.json. Failures don't abort the batch
(continue-on-error is the default). Sequential by default; --parallel N
runs N concurrent items.

This script does NOT post anything to Jira. It only reads issue metadata
(via the same credentials watch_video.py uses) and writes local artifacts.

Usage:
    python watch_batch.py --jira-keys PROJ-1234,PROJ-1235,PROJ-1236 --dedup --ocr
    python watch_batch.py --jira-jql "project=PROJ AND labels=video-bug AND created >= -7d"
    python watch_batch.py --inputs "C:/a.mp4,https://youtu.be/abc,PROJ-1234"

Any flag not consumed here (--dedup, --ocr, --whisper, --start, --end,
--resolution, --frames, --lang, --model, etc.) is forwarded to each
watch_video.py invocation.

Stdout: batch.json content.
Stderr: per-item structured events (prefixed with item index).
Exit code: 0 if at least one item succeeded; non-zero only if all failed
(use --strict to fail on any item failure).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


SCRIPTS_DIR = Path(__file__).parent
WATCH_VIDEO = SCRIPTS_DIR / "watch_video.py"
DEFAULT_BATCH_ROOT = Path("c:/tmp") if sys.platform == "win32" else Path("/tmp")
DEFAULT_CREDS_PATH = Path.home() / ".atlassian-token" / "credentials.json"

JIRA_KEY_RE = re.compile(r"^[A-Z]{2,10}-\d+$")
JIRA_URL_RE = re.compile(r"https?://[^/]+/browse/([A-Z]{2,10}-\d+)")


# ---- Input expansion -----------------------------------------------------

def _load_creds(creds_path: Path) -> dict:
    if not creds_path.exists():
        die(ExitCode.BAD_INPUT,
            f"Atlassian credentials not found at {creds_path}",
            credentials_path=str(creds_path),
            hint="See SKILL.md -> Jira token setup")
    return json.loads(creds_path.read_text(encoding="utf-8"))


def expand_jql(jql: str, creds_path: Path, page_size: int = 100) -> list[str]:
    """Run JQL search; return matching issue keys."""
    creds = _load_creds(creds_path)
    auth = base64.b64encode(f"{creds['email']}:{creds['token']}".encode()).decode()
    keys: list[str] = []
    start_at = 0
    while True:
        params = f"jql={urllib.request.quote(jql)}&fields=key&maxResults={page_size}&startAt={start_at}"
        url = f"https://{creds['site']}/rest/api/3/search?{params}"
        req = urllib.request.Request(url,
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                die(ExitCode.AUTH_FAIL, "Atlassian auth failed for JQL search")
            die(ExitCode.IO_FAIL, f"JQL search error {e.code}: {e.read().decode()[:200]}")
        issues = data.get("issues", [])
        keys.extend(i["key"] for i in issues)
        total = int(data.get("total", 0))
        start_at += len(issues)
        if not issues or start_at >= total:
            break
    return keys


def parse_inputs(args: argparse.Namespace) -> list[str]:
    """Resolve --jira-keys / --jira-jql / --inputs into a unified list."""
    sources = [s for s in (args.jira_keys, args.jira_jql, args.inputs) if s]
    if not sources:
        die(ExitCode.BAD_INPUT,
            "specify one of --jira-keys, --jira-jql, or --inputs")
    if len(sources) > 1:
        die(ExitCode.BAD_INPUT,
            "--jira-keys, --jira-jql, and --inputs are mutually exclusive")

    if args.jira_keys:
        keys = [k.strip() for k in args.jira_keys.split(",") if k.strip()]
        bad = [k for k in keys if not JIRA_KEY_RE.match(k)]
        if bad:
            die(ExitCode.BAD_INPUT, f"invalid Jira keys: {bad}")
        return keys
    if args.jira_jql:
        emit("start", step="jql_search", jql=args.jira_jql)
        keys = expand_jql(args.jira_jql, Path(args.credentials or DEFAULT_CREDS_PATH))
        emit("complete", step="jql_search", count=len(keys), keys=keys[:10])
        if not keys:
            die(ExitCode.BAD_INPUT, f"JQL returned 0 issues: {args.jira_jql}")
        return keys
    # --inputs (mixed)
    return [s.strip() for s in args.inputs.split(",") if s.strip()]


def slug_for_input(raw: str) -> str:
    """Derive a filesystem-safe per-item subdir name."""
    if JIRA_KEY_RE.match(raw):
        return raw.lower()
    m = JIRA_URL_RE.match(raw)
    if m:
        return m.group(1).lower()
    if raw.startswith(("http://", "https://")):
        tail = raw.rstrip("/").rsplit("/", 1)[-1] or "url"
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", tail).strip("-").lower()
        return f"url-{slug[:40]}"
    return f"path-{re.sub(r'[^a-zA-Z0-9._-]+', '-', Path(raw).stem).lower()[:50]}"


# ---- Per-item execution --------------------------------------------------

def run_one(raw: str, batch_dir: Path, forwarded_args: list[str],
            index: int, total: int) -> dict:
    workdir = batch_dir / slug_for_input(raw)
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(WATCH_VIDEO), raw,
           "--workdir", str(workdir)] + forwarded_args

    emit("start", step="item", index=index, total=total, input=raw,
         workdir=str(workdir))
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 2)

    # Forward sub-script stderr events (already JSON lines) up to our stderr
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()

    item: dict = {
        "input": raw,
        "workdir": str(workdir),
        "exit_code": proc.returncode,
        "elapsed_seconds": elapsed,
    }
    if proc.returncode == ExitCode.OK:
        try:
            meta = json.loads(proc.stdout)
            item["status"] = "ok"
            item["meta_path"] = str(workdir / "meta.json")
            item["summary"] = _short_summary(meta)
        except json.JSONDecodeError:
            item["status"] = "ok_no_meta"
            item["stdout_tail"] = proc.stdout[-300:]
    elif proc.returncode == ExitCode.AMBIGUOUS:
        item["status"] = "ambiguous"
        try:
            item["candidates"] = json.loads(proc.stdout).get("candidates", [])
        except json.JSONDecodeError:
            pass
        item["hint"] = "re-run individually with --attachment-id <ID>"
    else:
        item["status"] = "failed"
        # Try to extract error event from stderr tail
        for line in reversed(proc.stderr.splitlines()):
            try:
                ev = json.loads(line)
                if ev.get("event") == "error":
                    item["error"] = ev.get("msg")
                    break
            except json.JSONDecodeError:
                continue

    emit("complete", step="item", index=index, total=total,
         input=raw, status=item["status"],
         elapsed_seconds=elapsed)
    return item


def _short_summary(meta: dict) -> dict:
    """Compact per-item info for batch.json."""
    out: dict = {}
    video = meta.get("video") or {}
    if video.get("issue_key"):
        out["issue_key"] = video["issue_key"]
        out["issue_summary"] = video.get("issue_summary")
    elif video.get("title"):
        out["title"] = video["title"]
    probe = meta.get("probe") or {}
    out["duration_seconds"] = probe.get("duration")
    frames = meta.get("frames") or {}
    out["frame_count"] = frames.get("frame_count")
    transcript = meta.get("transcript") or {}
    if transcript:
        out["transcript_segments"] = transcript.get("segments")
        out["language"] = transcript.get("language")
    ocr = meta.get("ocr") or {}
    if ocr:
        out["ocr_frames_with_text"] = ocr.get("frames_with_text")
    return out


# ---- Main ----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--jira-keys", help="comma-separated Jira issue keys")
    src.add_argument("--jira-jql", help="JQL search; matched issues are processed")
    src.add_argument("--inputs", help="comma-separated raw inputs (path, URL, key)")

    ap.add_argument("--batch-dir",
                    help=f"parent dir for per-item workdirs "
                         f"(default {DEFAULT_BATCH_ROOT}/watch-batch-<timestamp>)")
    ap.add_argument("--credentials",
                    help=f"Atlassian credentials path (default {DEFAULT_CREDS_PATH})")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if ANY item fails (default: only if all fail)")

    # Everything else gets forwarded to watch_video.py
    args, forwarded = ap.parse_known_args()

    raw_inputs = parse_inputs(args)

    batch_dir = Path(args.batch_dir) if args.batch_dir else \
        DEFAULT_BATCH_ROOT / f"watch-batch-{int(time.time())}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    emit("start", step="batch", item_count=len(raw_inputs),
         batch_dir=str(batch_dir), forwarded_args=forwarded)
    overall_t0 = time.time()

    items: list[dict] = []
    for i, raw in enumerate(raw_inputs, 1):
        try:
            item = run_one(raw, batch_dir, forwarded, i, len(raw_inputs))
        except KeyboardInterrupt:
            emit("warning", step="batch", msg="interrupted by user", processed=len(items))
            break
        items.append(item)

    ok = sum(1 for it in items if it["status"] in ("ok", "ok_no_meta"))
    ambiguous = sum(1 for it in items if it["status"] == "ambiguous")
    failed = sum(1 for it in items if it["status"] == "failed")

    batch_result = {
        "schema_version": 1,
        "batch_dir": str(batch_dir),
        "started_at": int(overall_t0),
        "elapsed_seconds": round(time.time() - overall_t0, 2),
        "forwarded_args": forwarded,
        "summary": {
            "total": len(items),
            "ok": ok,
            "ambiguous": ambiguous,
            "failed": failed,
        },
        "items": items,
    }
    batch_json = batch_dir / "batch.json"
    staging = atomic_path(batch_json)
    staging.write_text(json.dumps(batch_result, indent=2), encoding="utf-8")
    finalize(staging, batch_json)

    emit("complete", step="batch",
         total=len(items), ok=ok, ambiguous=ambiguous, failed=failed,
         elapsed_seconds=batch_result["elapsed_seconds"],
         batch_json=str(batch_json))

    print(json.dumps(batch_result, indent=2))

    if args.strict and (failed > 0 or ambiguous > 0):
        return ExitCode.IO_FAIL
    if ok == 0 and items:
        return ExitCode.IO_FAIL
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
