"""Resolve a video input (URL, local path, Jira attachment, or "auto-pick") to a local file.

Modes (mutually exclusive):
    --path <PATH>             Use the local file as-is (no copy)
    --url <URL>               Download via yt-dlp into <workdir>/source.<ext>
    --jira-key <KEY>          Full-auto: read API token, list issue attachments,
                              range-download the (chosen) video attachment.
    --auto-downloads          Pick most recently modified video in ~/Downloads/
                              within --since-seconds window (default 300s).

For --jira-key with multiple video attachments:
    - With no --attachment-id flag: emits candidates JSON, exits with AMBIGUOUS (5).
    - With --attachment-id <ID>: downloads that specific attachment.

Stdout: one JSON object on success (or candidates list when AMBIGUOUS).
Stderr: structured JSON events.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402


VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".flv")
DEFAULT_CREDS_PATH = Path.home() / ".atlassian-token" / "credentials.json"


# ---- URL mode --------------------------------------------------------------

def _require_module(name: str, install_hint: str) -> None:
    """Verify a Python module is importable; die with MISSING_DEP if not."""
    import importlib.util
    if importlib.util.find_spec(name) is None:
        die(ExitCode.MISSING_DEP,
            f"{name} not installed. {install_hint}",
            dependency=name)


def fetch_url(url: str, workdir: Path) -> tuple[Path, dict]:
    """yt-dlp the URL into workdir/<title>.<ext>. Returns (path, metadata_dict).

    Uses yt-dlp's Python API in one shot to get title/uploader/source_url
    alongside the download (instead of two network calls)."""
    _require_module("yt_dlp", "Run: pip install --user yt-dlp")
    workdir.mkdir(parents=True, exist_ok=True)

    emit("start", step="yt_dlp", url=url)
    t0 = time.time()

    from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
    from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]

    ydl_opts = {
        "outtmpl": str(workdir / "%(title).100B.%(ext)s"),
        "restrictfilenames": True,  # filesystem-safe ASCII filenames
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Also pull captions if available -- preferring manual ("real") over
        # auto-generated. transcribe.py can use these instead of paying for
        # Whisper. Whisper still runs as a fallback when no captions exist.
        "writesubtitles": False,
        "writeautomaticsub": False,
        "subtitlesformat": "vtt",
        "subtitleslangs": ["en", "en-US", "en-GB"],
    }
    def _ydl_run(opts: dict) -> dict:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = _ydl_run(ydl_opts)
    except DownloadError as e:
        err_msg = str(e)
        # YouTube's subtitle endpoint rate-limits aggressively (429 / Too Many
        # Requests). The video itself is downloadable; only the captions
        # request is gated. Retry once without subtitles and let transcribe
        # fall back to local Whisper. Keeps the pipeline usable when YouTube
        # is throttling.
        is_subtitle_rate_limit = (
            "429" in err_msg
            or "Too Many Requests" in err_msg
            or "Sign in to confirm" in err_msg
        )
        if is_subtitle_rate_limit:
            emit("warning", step="yt_dlp",
                 msg="captions endpoint rate-limited; retrying without subtitles. "
                     "transcribe will fall back to local Whisper.",
                 first_error=err_msg.splitlines()[0][:200])
            retry_opts = {**ydl_opts,
                          "writesubtitles": False,
                          "writeautomaticsub": False}
            try:
                info = _ydl_run(retry_opts)
            except DownloadError as e2:
                die(ExitCode.IO_FAIL,
                    f"yt-dlp failed even without captions: {e2}")
        else:
            die(ExitCode.IO_FAIL, f"yt-dlp failed (network or unsupported URL): {e}")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        die(ExitCode.IO_FAIL, f"yt-dlp unexpected error: {e}")

    # Resolve the actual file yt-dlp wrote
    written = info.get("requested_downloads", [{}])[0].get("filepath") if info else None
    if written and Path(written).exists():
        path = Path(written)
    else:
        candidates = sorted(
            [p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            die(ExitCode.IO_FAIL, f"yt-dlp succeeded but no video found in {workdir}")
        path = candidates[0]

    # Locate caption file if yt-dlp pulled one. yt-dlp writes them next to the
    # video as <stem>.<lang>.vtt; we pick the first that exists.
    captions_path: Path | None = None
    captions_kind: str | None = None  # "manual" | "automatic" | None
    requested_subs = (info or {}).get("requested_subtitles") or {}
    if requested_subs:
        # Prefer manual over automatic; yt-dlp marks automatic ones with
        # "_automatic_captions" provenance under info, but the simpler heuristic
        # works: look for .vtt files written next to the video, manual first.
        sub_candidates = sorted(
            [p for p in workdir.iterdir() if p.is_file() and p.suffix == ".vtt"],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if sub_candidates:
            captions_path = sub_candidates[0]
            # We can't easily tell manual vs automatic post-hoc; the
            # requested_subtitles dict has _automatic flag, peek at first
            first_lang_meta = next(iter(requested_subs.values()), {})
            captions_kind = ("automatic"
                             if first_lang_meta.get("_auto") else "manual")

    metadata = {
        "source_url": (info or {}).get("webpage_url", url),
        "title": (info or {}).get("title"),
        "uploader": (info or {}).get("uploader"),
        "extractor": (info or {}).get("extractor"),
        "upload_date": (info or {}).get("upload_date"),
        "captions_path": str(captions_path) if captions_path else None,
        "captions_kind": captions_kind,
    }
    emit("complete", step="yt_dlp",
         duration_seconds=round(time.time() - t0, 2),
         output=str(path), title=metadata["title"],
         captions_path=metadata["captions_path"],
         captions_kind=metadata["captions_kind"])
    return path, metadata


# ---- Auto-downloads mode --------------------------------------------------

def auto_from_downloads(since_seconds: int) -> Path:
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        die(ExitCode.BAD_INPUT, f"~/Downloads not found at {downloads}")
    now = time.time()
    matches: list[tuple[float, Path]] = []
    for p in downloads.iterdir():
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        age = now - p.stat().st_mtime
        if age <= since_seconds:
            matches.append((p.stat().st_mtime, p))
    if not matches:
        die(ExitCode.BAD_INPUT,
            f"No video in {downloads} modified in last {since_seconds}s. Download first.")
    matches.sort(reverse=True)
    return matches[0][1]


# ---- Jira mode -------------------------------------------------------------

def _load_creds(path: Path) -> dict:
    if not path.exists():
        die(ExitCode.BAD_INPUT,
            f"Atlassian credentials not found at {path}",
            credentials_path=str(path),
            hint="See SKILL.md → Jira token setup")
    creds = json.loads(path.read_text(encoding="utf-8"))
    for key in ("email", "token", "site"):
        if not creds.get(key):
            die(ExitCode.BAD_INPUT, f"credentials.json missing field: {key}")
    return creds


def _basic_auth(email: str, token: str) -> str:
    return base64.b64encode(f"{email}:{token}".encode()).decode()


def _enumerate_attachments(jira_key: str, creds: dict) -> tuple[dict, list[dict]]:
    """Return (issue_meta, video_attachments)."""
    auth = _basic_auth(creds["email"], creds["token"])
    url = f"https://{creds['site']}/rest/api/3/issue/{jira_key}?fields=attachment,summary"
    req = urllib.request.Request(url,
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            issue = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            die(ExitCode.AUTH_FAIL,
                "Atlassian auth failed. Verify email matches the Atlassian login "
                "account (not necessarily the Jira profile email).")
        if e.code == 404:
            die(ExitCode.BAD_INPUT, f"Issue {jira_key} not found or no access")
        die(ExitCode.IO_FAIL, f"Atlassian API error {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        die(ExitCode.TIMEOUT, f"network error reaching Atlassian: {e}")

    attachments = issue.get("fields", {}).get("attachment", [])
    videos = [a for a in attachments if a.get("mimeType", "").startswith("video/")]
    return issue, videos


def _download_with_ranges(url: str, dest: Path, auth: str,
                          chunk_bytes: int = 4 * 1024 * 1024,
                          max_retries: int = 5) -> int:
    """Range-download to dest with retries. Returns total bytes written.

    Atlassian's media CDN closes connections early on large files; range requests
    fetch in 4MB chunks so dropped connections only kill one chunk's worth.

    On any failure (network exhaustion, Ctrl-C, etc.) the partial dest file is
    removed before re-raising / dying.
    """
    try:
        head_req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(head_req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", "0"))
            if not total:
                # Server didn't advertise size: stream whole thing
                with open(dest, "wb") as out:
                    shutil.copyfileobj(resp, out)
                return dest.stat().st_size

        written = 0
        with open(dest, "wb") as out:
            while written < total:
                end = min(written + chunk_bytes - 1, total - 1)
                for attempt in range(max_retries):
                    try:
                        req = urllib.request.Request(url, headers={
                            "Authorization": f"Basic {auth}",
                            "Range": f"bytes={written}-{end}",
                            "Accept-Encoding": "identity",
                        })
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            chunk = resp.read()
                            out.write(chunk)
                            written += len(chunk)
                        if written % (16 * 1024 * 1024) == 0 or written == total:
                            emit("progress", step="download", bytes=written, total=total)
                        break
                    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                        if attempt == max_retries - 1:
                            dest.unlink(missing_ok=True)
                            die(ExitCode.IO_FAIL,
                                f"range request failed after {max_retries} retries "
                                f"at byte {written}/{total}: {e}")
                        time.sleep(1.5 * (attempt + 1))
        return written
    except KeyboardInterrupt:
        dest.unlink(missing_ok=True)
        raise
    except urllib.error.URLError as e:
        dest.unlink(missing_ok=True)
        die(ExitCode.TIMEOUT, f"network error during download: {e}")


def fetch_jira(jira_key: str, workdir: Path, creds_path: Path,
               attachment_id: str | None) -> tuple[Path, dict]:
    creds = _load_creds(creds_path)
    issue, videos = _enumerate_attachments(jira_key, creds)

    if not videos:
        die(ExitCode.BAD_INPUT,
            f"No video attachments on {jira_key} "
            f"({len(issue['fields'].get('attachment', []))} non-video attachment(s))")

    # Multi-attachment handling
    if attachment_id is not None:
        match = [a for a in videos if a["id"] == attachment_id]
        if not match:
            die(ExitCode.BAD_INPUT,
                f"attachment-id {attachment_id} not found in {jira_key}'s video attachments",
                available_ids=[a["id"] for a in videos])
        attachment = match[0]
    elif len(videos) > 1:
        # AMBIGUOUS: surface candidates as JSON, caller picks
        candidates = [
            {
                "id": a["id"],
                "filename": a["filename"],
                "mime_type": a["mimeType"],
                "size_bytes": a["size"],
                "created": a.get("created"),
                "author": a.get("author", {}).get("displayName"),
            }
            for a in videos
        ]
        emit("warning", step="jira_attachments",
             msg=f"{len(videos)} video attachments on {jira_key}",
             count=len(videos))
        print(json.dumps({
            "ambiguous": True,
            "issue_key": issue["key"],
            "issue_summary": issue["fields"]["summary"],
            "candidates": candidates,
            "hint": "re-run with --attachment-id <id>",
        }))
        sys.exit(ExitCode.AMBIGUOUS)
    else:
        attachment = videos[0]

    # Download
    workdir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in attachment["filename"])
    dest = workdir / safe_name
    staging = atomic_path(dest)

    auth = _basic_auth(creds["email"], creds["token"])
    emit("start", step="download",
         attachment_id=attachment["id"], filename=attachment["filename"],
         size_bytes=attachment["size"])
    t0 = time.time()
    try:
        total = _download_with_ranges(attachment["content"], staging, auth)
    except KeyboardInterrupt:
        staging.unlink(missing_ok=True)
        raise

    expected = attachment.get("size")
    if expected and total != expected:
        staging.unlink(missing_ok=True)
        die(ExitCode.IO_FAIL,
            f"download size mismatch: got {total}, expected {expected}")
    finalize(staging, dest)
    emit("complete", step="download",
         duration_seconds=round(time.time() - t0, 2),
         output=str(dest), bytes=total)

    return dest, {
        "issue_key": issue["key"],
        "issue_summary": issue["fields"]["summary"],
        "attachment_id": attachment["id"],
        "attachment_name": attachment["filename"],
        "mime_type": attachment["mimeType"],
        "size_bytes": total,
        "site": creds["site"],
    }


# ---- Entry point ----------------------------------------------------------

def run_inproc(
    workdir: Path,
    url: str | None = None,
    path: str | None = None,
    jira_key: str | None = None,
    auto_downloads: bool = False,
    attachment_id: str | None = None,
    since_seconds: int = 300,
    credentials: str | None = None,
) -> dict:
    """Pure function for in-process invocation. See probe.run_inproc docstring.

    Exactly one of {url, path, jira_key, auto_downloads=True} must be provided.
    """
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if credentials is None:
        credentials = str(DEFAULT_CREDS_PATH)

    extra: dict = {}
    if url:
        downloaded, extra = fetch_url(url, workdir)
        source = "url"
    elif path:
        p = Path(path).resolve()
        if not p.exists():
            die(ExitCode.BAD_INPUT, f"file not found: {p}")
        downloaded = p
        source = "path"
    elif jira_key:
        downloaded, extra = fetch_jira(
            jira_key, workdir, Path(credentials), attachment_id,
        )
        source = "jira"
    elif auto_downloads:
        downloaded = auto_from_downloads(since_seconds)
        source = "auto-downloads"
    else:
        die(ExitCode.BAD_INPUT,
            "fetch.run_inproc requires one of: url, path, jira_key, or auto_downloads=True")

    return {
        "path": str(downloaded),
        "source": source,
        "size_bytes": downloaded.stat().st_size,
        "mtime": int(downloaded.stat().st_mtime),
        **extra,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--path")
    src.add_argument("--jira-key", help="Jira issue key, e.g. PROJ-1234")
    src.add_argument("--auto-downloads", action="store_true")
    ap.add_argument("--attachment-id",
                    help="(--jira-key only) disambiguate when multiple video attachments")
    ap.add_argument("--since-seconds", type=int, default=300,
                    help="(--auto-downloads only) max file age")
    ap.add_argument("--credentials", default=str(DEFAULT_CREDS_PATH),
                    help="(--jira-key only) path to Atlassian credentials JSON")
    args = ap.parse_args()
    info = run_inproc(
        workdir=Path(args.workdir),
        url=args.url,
        path=args.path,
        jira_key=args.jira_key,
        auto_downloads=args.auto_downloads,
        attachment_id=args.attachment_id,
        since_seconds=args.since_seconds,
        credentials=args.credentials,
    )
    print(json.dumps(info))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
