"""Unit tests for scripts/fetch_attachment.py (non-video Jira attachment fetch).

These are hermetic: no network, no Atlassian. urlopen and the range-downloader
are monkeypatched. What is under test is the mimeType filtering, the path
sanitization, the size gate, the exit codes, and the guarantee that the API
token never reaches stdout, stderr, or the returned JSON.

Framework matches the rest of mcp-server/tests: pytest.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import fetch  # noqa: E402
import fetch_attachment  # noqa: E402
from _common import ExitCode  # noqa: E402


FAKE_TOKEN = "ATATT-fake-token-do-not-leak-9f8e7d6c"

ATTACHMENTS = [
    {"id": "1001", "filename": "shot1.png", "mimeType": "image/png",
     "size": 98816, "content": "https://example.atlassian.net/rest/api/3/attachment/content/1001"},
    {"id": "1002", "filename": "shot2.png", "mimeType": "image/png",
     "size": 9534, "content": "https://example.atlassian.net/rest/api/3/attachment/content/1002"},
    {"id": "1003", "filename": "repro.mp4", "mimeType": "video/mp4",
     "size": 4096, "content": "https://example.atlassian.net/rest/api/3/attachment/content/1003"},
    {"id": "1004", "filename": "spec.pdf", "mimeType": "application/pdf",
     "size": 2048, "content": "https://example.atlassian.net/rest/api/3/attachment/content/1004"},
]

ISSUE = {
    "key": "PROJ-1234",
    "fields": {"summary": "A ticket with pictures", "attachment": ATTACHMENTS},
}


@pytest.fixture
def creds_file(tmp_path: Path) -> Path:
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({
        "email": "someone@example.com",
        "token": FAKE_TOKEN,
        "site": "example.atlassian.net",
    }), encoding="utf-8")
    return p


@pytest.fixture
def fake_issue(monkeypatch):
    """Make the Jira REST call return ISSUE without touching the network."""
    def _urlopen(req, timeout=None):
        return io.BytesIO(json.dumps(ISSUE).encode("utf-8"))
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _urlopen)


@pytest.fixture
def fake_download(monkeypatch):
    """Write `size` bytes of PNG-ish content instead of downloading."""
    by_url = {a["content"]: a for a in ATTACHMENTS}

    def _dl(url, dest, auth, **kwargs):
        att = by_url[url]
        payload = b"\x89PNG\r\n\x1a\n" + b"x" * (att["size"] - 8)
        dest.write_bytes(payload)
        return len(payload)

    monkeypatch.setattr(fetch, "_download_with_ranges", _dl)
    return by_url


# ---- mimeType filtering ---------------------------------------------------

def test_default_prefix_takes_images_only(creds_file, tmp_path, fake_issue, fake_download):
    got = fetch_attachment.run_inproc(
        "PROJ-1234", outdir=str(tmp_path / "out"), credentials=str(creds_file))
    assert [a["id"] for a in got] == ["1001", "1002"]
    assert all(a["mime_type"] == "image/png" for a in got)
    for a in got:
        assert Path(a["path"]).is_file()
        assert Path(a["path"]).stat().st_size == a["size_bytes"]


def test_video_prefix_still_selects_video(creds_file, tmp_path, fake_issue, fake_download):
    got = fetch_attachment.run_inproc(
        "PROJ-1234", mime_prefix="video/", outdir=str(tmp_path / "out"),
        credentials=str(creds_file))
    assert [a["id"] for a in got] == ["1003"]


def test_empty_prefix_takes_everything(creds_file, tmp_path, fake_issue, fake_download):
    got = fetch_attachment.run_inproc(
        "PROJ-1234", mime_prefix="", outdir=str(tmp_path / "out"),
        credentials=str(creds_file))
    assert [a["id"] for a in got] == ["1001", "1002", "1003", "1004"]


def test_attachment_id_selects_one_regardless_of_prefix(
        creds_file, tmp_path, fake_issue, fake_download):
    got = fetch_attachment.run_inproc(
        "PROJ-1234", attachment_id="1004", mime_prefix="",
        outdir=str(tmp_path / "out"), credentials=str(creds_file))
    assert len(got) == 1 and got[0]["filename"] == "spec.pdf"


def test_unknown_attachment_id_is_bad_input(creds_file, tmp_path, fake_issue, fake_download):
    with pytest.raises(SystemExit) as e:
        fetch_attachment.run_inproc(
            "PROJ-1234", attachment_id="99999", mime_prefix="",
            outdir=str(tmp_path / "out"), credentials=str(creds_file))
    assert e.value.code == ExitCode.BAD_INPUT


# ---- no matches -----------------------------------------------------------

def test_no_matching_attachments_exits_bad_input(
        creds_file, tmp_path, fake_issue, fake_download, capsys):
    with pytest.raises(SystemExit) as e:
        fetch_attachment.run_inproc(
            "PROJ-1234", mime_prefix="audio/", outdir=str(tmp_path / "out"),
            credentials=str(creds_file))
    assert e.value.code == ExitCode.BAD_INPUT
    err = capsys.readouterr().err
    # The error must name what IS there, so the caller can retry usefully.
    assert "image/png" in err and "application/pdf" in err


# ---- 401 ------------------------------------------------------------------

def test_atlassian_401_exits_auth_fail(creds_file, tmp_path, monkeypatch):
    def _raise_401(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"nope"))
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _raise_401)
    with pytest.raises(SystemExit) as e:
        fetch_attachment.run_inproc(
            "PROJ-1234", outdir=str(tmp_path / "out"), credentials=str(creds_file))
    assert e.value.code == ExitCode.AUTH_FAIL


# ---- path traversal -------------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../../x.png", "..\\..\\x.png", "/etc/passwd", "C:\\Windows\\a.png",
    "..", ".", "", "  ", "sub/dir/x.png",
    # Windows legacy device names. Measured on Windows 11 26200 + CPython
    # 3.14: "CON.png"/"nul.png"/"COM1.png"/"LPT1" under a directory path all
    # create ordinary files (n=1), so these are here as a canary, not because
    # a device write is known to happen. Trailing dots and spaces ARE stripped
    # by the OS at create time -- see the collision test below.
    "CON.png", "nul.png", "COM1.png", "LPT1", "x.png.", "x.png ",
])
def test_filename_cannot_escape_outdir(evil, creds_file, tmp_path, monkeypatch):
    issue = {"key": "PROJ-1", "fields": {"summary": "s", "attachment": [
        {"id": "1", "filename": evil, "mimeType": "image/png", "size": 8,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/1"}]}}
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda req, timeout=None: io.BytesIO(json.dumps(issue).encode()))
    monkeypatch.setattr(
        fetch, "_download_with_ranges",
        lambda url, dest, auth, **kw: (dest.write_bytes(b"\x89PNG\r\n\x1a\n"), 8)[1])

    outdir = (tmp_path / "out").resolve()
    got = fetch_attachment.run_inproc(
        "PROJ-1", outdir=str(outdir), credentials=str(creds_file))
    written = Path(got[0]["path"]).resolve()
    assert written.parent == outdir, f"{evil!r} escaped to {written}"
    assert written.is_file()


def test_names_that_collide_after_sanitizing_stay_separate_files(
        creds_file, tmp_path, monkeypatch):
    """'a.png', 'a.png.' and 'a/png' all reduce toward the same name -- and
    Windows additionally strips trailing dots and spaces at create time. Each
    attachment must still end up as its own readable file."""
    names = ["a.png", "a.png.", "a.png ", "a.png"]
    issue = {"key": "PROJ-1", "fields": {"summary": "s", "attachment": [
        {"id": str(i), "filename": n, "mimeType": "image/png", "size": 8,
         "content": f"https://example.atlassian.net/rest/api/3/attachment/content/{i}"}
        for i, n in enumerate(names)]}}
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda req, timeout=None: io.BytesIO(json.dumps(issue).encode()))
    monkeypatch.setattr(
        fetch, "_download_with_ranges",
        lambda url, dest, auth, **kw: (dest.write_bytes(b"\x89PNG\r\n\x1a\n"), 8)[1])

    got = fetch_attachment.run_inproc(
        "PROJ-1", outdir=str(tmp_path / "out"), credentials=str(creds_file))
    paths = [Path(a["path"]) for a in got]
    assert len({p.name for p in paths}) == len(names), f"names collided: {paths}"
    for p in paths:
        assert p.is_file()
    # Distinct strings are not enough: 'a.png.' and 'a.png' are the SAME file
    # on Windows, so count what actually landed on disk.
    on_disk = list((tmp_path / "out").iterdir())
    assert len(on_disk) == len(names), (
        f"{len(names)} attachments produced {len(on_disk)} files: {on_disk}")


# ---- size limit -----------------------------------------------------------

def test_oversize_single_attachment_is_refused(creds_file, tmp_path, fake_issue, fake_download):
    with pytest.raises(SystemExit) as e:
        fetch_attachment.run_inproc(
            "PROJ-1234", attachment_id="1001", outdir=str(tmp_path / "out"),
            credentials=str(creds_file), max_bytes=1000)
    assert e.value.code == ExitCode.BAD_INPUT


def test_oversize_in_bulk_is_skipped_not_silent(creds_file, tmp_path, fake_issue, fake_download):
    got = fetch_attachment.run_inproc(
        "PROJ-1234", outdir=str(tmp_path / "out"),
        credentials=str(creds_file), max_bytes=50_000)
    by_id = {a["id"]: a for a in got}
    assert by_id["1001"]["skipped"] == "too_large"   # 98816 > 50000
    assert by_id["1001"]["path"] is None
    assert by_id["1002"].get("skipped") is None
    assert Path(by_id["1002"]["path"]).is_file()


def test_default_limit_is_about_50mb():
    assert fetch_attachment.MAX_ATTACHMENT_BYTES == 50 * 1024 * 1024


# ---- size limit on the WIRE, not just in the metadata ---------------------
#
# The tests above stub _download_with_ranges, so they only prove the metadata
# pre-check. These run the real downloader against a fake HTTP layer, which is
# where a server that lies about (or omits) Content-Length gets caught.


class _FakeResp:
    """Counts bytes handed out, so a test can prove the reader stopped early
    instead of swallowing the whole body and deleting it afterwards."""

    served = 0

    def __init__(self, body: bytes, headers: dict):
        self._b, self._pos, self.headers = body, 0, headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        chunk = self._b[self._pos:] if n is None or n < 0 else self._b[self._pos:self._pos + n]
        self._pos += len(chunk)
        type(self).served += len(chunk)
        return chunk


def _serve(monkeypatch, issue: dict, body: bytes, content_length: str | None,
           honour_range: bool = True):
    """Fake Atlassian: the issue endpoint plus one attachment that returns
    `body` regardless of what its Content-Length claims."""
    def _urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/rest/api/3/issue/" in url:
            return io.BytesIO(json.dumps(issue).encode())
        headers = {} if content_length is None else {"Content-Length": content_length}
        rng = req.headers.get("Range") if hasattr(req, "headers") else None
        if rng and honour_range:
            start, end = rng.split("=")[1].split("-")
            return _FakeResp(body[int(start):int(end) + 1], headers)
        return _FakeResp(body, headers)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _urlopen)


def _one_image_issue(size: int) -> dict:
    return {"key": "PROJ-1", "fields": {"summary": "s", "attachment": [
        {"id": "1", "filename": "big.png", "mimeType": "image/png", "size": size,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/1"}]}}


def test_missing_content_length_cannot_stream_past_the_cap(
        creds_file, tmp_path, monkeypatch):
    """No Content-Length at all -> the old code shutil.copyfileobj'd the whole
    body to disk before anyone measured it."""
    cap = 1000
    body = b"\x89PNG\r\n\x1a\n" + b"x" * (40 * 1024 * 1024)
    _serve(monkeypatch, _one_image_issue(100), body, content_length=None)
    outdir = tmp_path / "out"
    _FakeResp.served = 0
    with pytest.raises(SystemExit) as e:
        fetch_attachment.run_inproc(
            "PROJ-1", attachment_id="1", outdir=str(outdir),
            credentials=str(creds_file), max_bytes=cap)
    assert e.value.code == ExitCode.BAD_INPUT
    assert not any(outdir.iterdir()), f"oversize body left on disk: {list(outdir.iterdir())}"
    # The point is that the read STOPS. Writing 40 MB and deleting it after is
    # not a size limit. One 1 MB read block is the reader's granularity.
    assert _FakeResp.served <= cap + 1024 * 1024, (
        f"pulled {_FakeResp.served} bytes off the wire for a {cap}-byte cap")


def test_lying_content_length_cannot_land_on_disk(creds_file, tmp_path, monkeypatch):
    """Metadata and Content-Length both say 'small'; the body is not."""
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 5000
    _serve(monkeypatch, _one_image_issue(100), body, content_length="100",
           honour_range=False)
    outdir = tmp_path / "out"
    with pytest.raises(SystemExit):
        fetch_attachment.run_inproc(
            "PROJ-1", attachment_id="1", outdir=str(outdir),
            credentials=str(creds_file), max_bytes=1000)
    assert not any(outdir.iterdir())


def test_max_bytes_argument_is_what_gates_the_wire(creds_file, tmp_path, monkeypatch):
    """Regression: the wire check compared against the module constant, so a
    caller-supplied max_bytes was ignored once the download started."""
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 2000
    _serve(monkeypatch, _one_image_issue(500), body, content_length=None)
    outdir = tmp_path / "out"
    with pytest.raises(SystemExit):
        fetch_attachment.run_inproc(
            "PROJ-1", attachment_id="1", outdir=str(outdir),
            credentials=str(creds_file), max_bytes=1000)
    assert not any(outdir.iterdir())


def test_wire_oversize_in_bulk_skips_instead_of_aborting(
        creds_file, tmp_path, monkeypatch):
    """One fat attachment must not kill the rest of the batch."""
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 5000
    issue = {"key": "PROJ-1", "fields": {"summary": "s", "attachment": [
        {"id": "1", "filename": "big.png", "mimeType": "image/png", "size": 100,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/1"},
        {"id": "2", "filename": "small.png", "mimeType": "image/png", "size": 8,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/2"}]}}

    def _urlopen(req, timeout=None):
        url = req.full_url
        if "/rest/api/3/issue/" in url:
            return io.BytesIO(json.dumps(issue).encode())
        if url.endswith("/2"):
            return _FakeResp(b"\x89PNG\r\n\x1a\n", {"Content-Length": "8"})
        return _FakeResp(body, {})  # attachment 1: no Content-Length, fat body
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _urlopen)

    got = fetch_attachment.run_inproc(
        "PROJ-1", outdir=str(tmp_path / "out"), credentials=str(creds_file),
        max_bytes=1000)
    by_id = {a["id"]: a for a in got}
    assert by_id["1"]["skipped"] == "too_large" and by_id["1"]["path"] is None
    assert Path(by_id["2"]["path"]).is_file()


# ---- the download URL comes from the API response -------------------------

def test_content_url_off_the_credentialed_site_is_refused(
        creds_file, tmp_path, monkeypatch):
    """attachment.content is server-supplied. Sending the Basic auth header
    wherever it points is not acceptable; the download must stay on the site
    the credentials belong to."""
    issue = {"key": "PROJ-1", "fields": {"summary": "s", "attachment": [
        {"id": "1", "filename": "a.png", "mimeType": "image/png", "size": 8,
         "content": "http://127.0.0.1:1/steal"}]}}
    calls: list[str] = []

    def _urlopen(req, timeout=None):
        calls.append(req.full_url)
        if "/rest/api/3/issue/" in req.full_url:
            return io.BytesIO(json.dumps(issue).encode())
        raise AssertionError("download was attempted against a foreign host")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _urlopen)

    with pytest.raises(SystemExit) as e:
        fetch_attachment.run_inproc(
            "PROJ-1", outdir=str(tmp_path / "out"), credentials=str(creds_file))
    assert e.value.code == ExitCode.BAD_INPUT
    assert not any(c.startswith("http://127.0.0.1") for c in calls)


# ---- the attachment id is server-supplied too -----------------------------

def test_attachment_id_cannot_steer_the_write_path(creds_file, tmp_path, monkeypatch):
    """The duplicate-name branch prefixes the id onto the filename. Ids are
    numeric in the wild, but they come from the API, not from us."""
    issue = {"key": "PROJ-1", "fields": {"summary": "s", "attachment": [
        {"id": "1", "filename": "a.png", "mimeType": "image/png", "size": 8,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/1"},
        {"id": "../pwn", "filename": "a.png", "mimeType": "image/png", "size": 8,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/2"},
        {"id": "C:\\Windows\\win.ini", "filename": "a.png", "mimeType": "image/png",
         "size": 8,
         "content": "https://example.atlassian.net/rest/api/3/attachment/content/3"}]}}
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda req, timeout=None: (
                            io.BytesIO(json.dumps(issue).encode())
                            if "/rest/api/3/issue/" in req.full_url
                            else _FakeResp(b"\x89PNG\r\n\x1a\n", {"Content-Length": "8"})))

    outdir = (tmp_path / "out").resolve()
    got = fetch_attachment.run_inproc(
        "PROJ-1", outdir=str(outdir), credentials=str(creds_file))
    for a in got:
        p = Path(a["path"]).resolve()
        assert p.parent == outdir, f"id {a['id']!r} steered the write to {p}"
    assert len(list(outdir.iterdir())) == 3


# ---- token never leaks ----------------------------------------------------

def test_token_absent_from_result_and_streams(
        creds_file, tmp_path, fake_issue, fake_download, capsys):
    got = fetch_attachment.run_inproc(
        "PROJ-1234", mime_prefix="", outdir=str(tmp_path / "out"),
        credentials=str(creds_file))
    captured = capsys.readouterr()
    blob = json.dumps(got) + captured.out + captured.err
    assert FAKE_TOKEN not in blob
    assert "Basic " not in blob
    # The creds path is what the never-do:secret-file-read guard reacts to;
    # it has no business in the payload either.
    assert "credentials.json" not in json.dumps(got)


def test_token_absent_from_error_paths(creds_file, tmp_path, monkeypatch, capsys):
    def _raise_500(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.invalid", 500, "boom", {}, io.BytesIO(b"server said no"))
    monkeypatch.setattr(fetch.urllib.request, "urlopen", _raise_500)
    with pytest.raises(SystemExit):
        fetch_attachment.run_inproc(
            "PROJ-1234", outdir=str(tmp_path / "out"), credentials=str(creds_file))
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out + captured.err


# ---- CLI ------------------------------------------------------------------

def test_cli_prints_json_list(creds_file, tmp_path, fake_issue, fake_download,
                              capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "fetch_attachment.py", "PROJ-1234",
        "--outdir", str(tmp_path / "out"),
        "--credentials", str(creds_file),
    ])
    rc = fetch_attachment.main()
    assert rc == ExitCode.OK
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [a["id"] for a in payload] == ["1001", "1002"]


# ---- regression: fetch.py's own video path is unchanged -------------------

def test_enumerate_defaults_to_video_for_existing_callers(creds_file, fake_issue):
    creds = json.loads(creds_file.read_text(encoding="utf-8"))
    _, matched = fetch._enumerate_attachments("PROJ-1234", creds)
    assert [a["id"] for a in matched] == ["1003"]


# ---- the MCP layer must survive a top-level JSON array --------------------

def test_extract_final_json_handles_an_array_after_noise():
    """fetch_attachment.py prints a top-level array; _extract_final_json used
    to look for objects only, so noisy stdout would have degraded it to '{}'."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import server

    noisy = 'yt-dlp: something on stdout\n[\n  {"id": "1", "path": "x.png"}\n]\n'
    assert json.loads(server._extract_final_json(noisy)) == [{"id": "1", "path": "x.png"}]
    assert json.loads(server._extract_final_json('[{"id": "1"}]')) == [{"id": "1"}]
