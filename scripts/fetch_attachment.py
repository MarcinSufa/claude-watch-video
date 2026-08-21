"""Download NON-video Jira attachments (images, PDFs, anything) to local files.

fetch.py's Jira mode exists to feed the video pipeline, so it only ever picks
`video/*`. This script is the general case: pick attachments by mimeType
prefix, write them to a directory, print the paths. The caller (an agent, via
the MCP tool `fetch_jira_attachment`) then opens them with an ordinary file
read -- which is the whole point, since the sandbox and the auto-mode
classifier block an agent from making the HTTPS call itself.

READ-ONLY: nothing here writes to Jira.

Stdout: one JSON array on success, one object per attachment:
    [{"id", "filename", "mime_type", "size_bytes", "path", "skipped"?}, ...]
Stderr: structured JSON events (see _common.emit).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ExitCode, atomic_path, die, emit, finalize  # noqa: E402
import fetch  # noqa: E402


# Attachments larger than this are refused (single-id mode) or skipped and
# reported (bulk mode). An agent reads these files into its context; a
# 200 MB one is never what was wanted, and the range downloader would spend
# minutes on it.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

DEFAULT_OUTDIR_ROOT = Path("c:/tmp") if sys.platform == "win32" else Path("/tmp")


def _default_outdir(jira_key: str) -> Path:
    return DEFAULT_OUTDIR_ROOT / f"jira-{fetch.sanitize_filename(jira_key, 'issue')}"


def _human(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{n} bytes"


def _download_one(attachment: dict, outdir: Path, auth: str, site: str,
                  used_names: set[str], max_bytes: int) -> dict:
    """Fetch one attachment into outdir. Returns the result record.

    Raises fetch.AttachmentTooLarge when the body exceeds max_bytes; the
    caller decides whether that aborts the run or skips one file.
    """
    # Both the id and the filename come from the API response, so neither is
    # trusted as a path component.
    att_id = str(attachment["id"])
    safe_id = fetch.sanitize_filename(att_id, fallback="attachment")
    name = fetch.sanitize_filename(attachment.get("filename") or "",
                                   fallback=f"attachment-{safe_id}")
    if name in used_names:
        name = f"{safe_id}-{name}"
    used_names.add(name)

    url = attachment["content"]
    host = urllib.parse.urlparse(url).netloc
    if host.lower() != site.lower():
        die(ExitCode.BAD_INPUT,
            f"attachment {att_id} points at {host or url!r}, not the "
            f"credentialed site {site}; refusing to send credentials there",
            attachment_id=att_id)

    dest = outdir / name
    if dest.resolve().parent != outdir:
        # Belt and braces: sanitize_filename already flattens separators.
        die(ExitCode.BAD_INPUT,
            f"refusing to write attachment {att_id} outside {outdir}",
            attachment_id=att_id)
    staging = atomic_path(dest)
    emit("start", step="download_attachment", attachment_id=att_id,
         filename=attachment.get("filename"), size_bytes=attachment.get("size"))
    t0 = time.time()
    try:
        total = fetch._download_with_ranges(url, staging, auth, max_bytes=max_bytes)
    except KeyboardInterrupt:
        staging.unlink(missing_ok=True)
        raise
    except fetch.AttachmentTooLarge:
        staging.unlink(missing_ok=True)
        raise

    expected = attachment.get("size")
    if expected and total != expected:
        staging.unlink(missing_ok=True)
        die(ExitCode.IO_FAIL,
            f"download size mismatch for {att_id}: got {total}, expected {expected}")
    finalize(staging, dest)
    emit("complete", step="download_attachment",
         duration_seconds=round(time.time() - t0, 2),
         output=str(dest), bytes=total)

    return {
        "id": att_id,
        "filename": attachment.get("filename"),
        "mime_type": attachment.get("mimeType"),
        "size_bytes": total,
        "path": str(dest),
    }


def run_inproc(
    jira_key: str,
    mime_prefix: str = "image/",
    attachment_id: str | None = None,
    outdir: str | None = None,
    credentials: str | None = None,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> list[dict]:
    """Download the matching attachments of `jira_key`. Returns result records.

    mime_prefix "" matches every attachment. attachment_id overrides the
    prefix filter and selects exactly one attachment.
    """
    creds_path = Path(credentials) if credentials else fetch.DEFAULT_CREDS_PATH
    creds = fetch._load_creds(creds_path)

    # attachment_id addresses one specific file, so enumerate everything and
    # match on id; otherwise let the server-side list drive the selection.
    issue, matched = fetch._enumerate_attachments(
        jira_key, creds, "" if attachment_id else mime_prefix)

    if attachment_id is not None:
        matched = [a for a in matched if str(a["id"]) == str(attachment_id)]
        if not matched:
            die(ExitCode.BAD_INPUT,
                f"attachment-id {attachment_id} not found on {jira_key}",
                available_ids=[str(a["id"])
                               for a in issue["fields"].get("attachment", [])])

    if not matched:
        present = sorted({a.get("mimeType", "?")
                          for a in issue["fields"].get("attachment", [])})
        die(ExitCode.BAD_INPUT,
            f"no attachment on {jira_key} matches mimeType prefix "
            f"{mime_prefix!r}. Present: {', '.join(present) or 'none'}",
            present_mime_types=present)

    outpath = Path(outdir).expanduser() if outdir else _default_outdir(jira_key)
    outpath = outpath.resolve()
    outpath.mkdir(parents=True, exist_ok=True)

    auth = fetch._basic_auth(creds["email"], creds["token"])
    results: list[dict] = []
    used_names: set[str] = set()

    def _too_large_record(att: dict, observed: int) -> dict:
        emit("warning", step="download_attachment",
             msg=f"skipping {att.get('filename')}: {_human(observed)} exceeds "
                 f"{_human(max_bytes)} limit",
             attachment_id=str(att["id"]))
        return {
            "id": str(att["id"]),
            "filename": att.get("filename"),
            "mime_type": att.get("mimeType"),
            "size_bytes": observed,
            "path": None,
            "skipped": "too_large",
        }

    for att in matched:
        # The declared size is a hint that saves a request; the real ceiling
        # is enforced against the bytes on the wire inside _download_one.
        declared = att.get("size") or 0
        if declared > max_bytes:
            if attachment_id is not None:
                die(ExitCode.BAD_INPUT,
                    f"attachment {att['id']} ({att.get('filename')}) is "
                    f"{_human(declared)}, over the {_human(max_bytes)} limit")
            results.append(_too_large_record(att, declared))
            continue
        try:
            results.append(
                _download_one(att, outpath, auth, creds["site"], used_names, max_bytes))
        except fetch.AttachmentTooLarge as e:
            if attachment_id is not None:
                die(ExitCode.BAD_INPUT,
                    f"attachment {att['id']} ({att.get('filename')}) is "
                    f"{_human(e.observed)} on the wire, over the "
                    f"{_human(max_bytes)} limit")
            # One fat attachment must not kill the rest of the batch.
            results.append(_too_large_record(att, e.observed))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download Jira attachments by mimeType prefix (read-only).")
    ap.add_argument("jira_key", help="Jira issue key, e.g. PROJ-1234")
    ap.add_argument("--mime-prefix", default="image/",
                    help="mimeType prefix filter; '' matches everything")
    ap.add_argument("--attachment-id",
                    help="download exactly this attachment, ignoring the prefix")
    ap.add_argument("--outdir",
                    help="destination dir (default: <tmp>/jira-<KEY>/)")
    ap.add_argument("--max-bytes", type=int, default=MAX_ATTACHMENT_BYTES,
                    help="per-file size ceiling")
    ap.add_argument("--credentials", default=str(fetch.DEFAULT_CREDS_PATH),
                    help="path to the Atlassian credentials JSON")
    args = ap.parse_args()

    results = run_inproc(
        jira_key=args.jira_key,
        mime_prefix=args.mime_prefix,
        attachment_id=args.attachment_id,
        outdir=args.outdir,
        credentials=args.credentials,
        max_bytes=args.max_bytes,
    )
    print(json.dumps(results))
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
