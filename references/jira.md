# Jira: token setup, ambiguous attachments, semi-auto fallback, non-video attachments

Read when the input is a Jira key or URL and the run fails with `credentials not found` (exit 2) or an Atlassian 401 (exit 4), when it exits 5 (several videos on the issue), or when the user asks about screenshots/PDFs on a ticket.

## Jira token setup

Jira mode (`watch_video.py PROJ-1234`) is fully automatic (no manual download click) **if** an Atlassian API token is configured. Setup is a one-time, ~3-minute process.

### Step 1 — Create the token

1. Open https://id.atlassian.com/manage-profile/security/api-tokens
2. Sign in with your Atlassian **login email** (this matters — see Step 3)
3. Click **Create API token**
4. Label it something memorable, e.g. `claude-code-watch-video (YYYY-MM)`
5. Copy the token (starts with `ATATT3xFf...`, ~190 chars). **You can only see it once.**

If the **Create API token** button is greyed out or you see a "Restricted by your organization" banner, your admin has disabled user API tokens — the full-auto mode is not available; the skill falls back to semi-auto.

### Step 2 — Save credentials to disk

Create `~/.atlassian-token/credentials.json` (Windows: `%USERPROFILE%\.atlassian-token\credentials.json`):

```json
{
  "email": "you@yourcompany.com",
  "token": "ATATT3xFf...",
  "site": "yoursite.atlassian.net"
}
```

The token in this file is plaintext, so **lock down the file permissions**:

```bash
chmod 700 ~/.atlassian-token && chmod 600 ~/.atlassian-token/credentials.json
```

Windows (PowerShell): `icacls "$env:USERPROFILE\.atlassian-token\credentials.json" /inheritance:r /grant:r "${env:USERNAME}:R"`

### Step 3 — Verify

```bash
python3 - <<'EOF'
import json, urllib.request, base64, pathlib
c = json.loads((pathlib.Path.home() / ".atlassian-token" / "credentials.json").read_text())
auth = base64.b64encode(f"{c['email']}:{c['token']}".encode()).decode()
req = urllib.request.Request(f"https://{c['site']}/rest/api/3/myself", headers={"Authorization": f"Basic {auth}"})
print(json.load(urllib.request.urlopen(req)).get("displayName"))
EOF
```

(Windows: save the same Python to a file and run it with `py`.)

Should print your display name. If it returns **401 Unauthorized**:
- The `email` in `credentials.json` must match the Atlassian **login** account. This is sometimes different from the email shown on your Jira *profile* (e.g. `name@company.com` for login vs `name@subsidiary.com` for profile). Confirm at https://id.atlassian.com/manage-profile/email and update the JSON if needed.
- Verify the token was copied without truncation or extra whitespace (use a "show token" toggle if your editor hides it).
- Confirm `site` is the Jira hostname without `https://` prefix.

### Security notes

- The credentials file is plaintext. ACL lockdown limits damage if your account is compromised, but anyone with admin/system rights on the machine can still read it.
- **Don't share the file via screen recording or email** — the token grants your full Jira/Confluence access.
- **Rotate periodically.** Revoke unused tokens at the same URL where you created them.
- **If the token leaks** (e.g. echoed into a shell log, chat transcript, screenshot), revoke immediately and create a new one.
- Tokens never expire automatically. Old tokens you forget about stay valid forever — set a calendar reminder to audit yearly.

### How the skill uses it

`fetch.py` reads `credentials.json`, calls Atlassian REST API `/rest/api/3/issue/<KEY>?fields=attachment`, picks the (chosen) `video/*` attachment, then range-downloads the binary to `<workdir>/<filename>`. Range requests in 4 MB chunks with retries handle the CDN's early-connection-close behavior on large files. No browser, no manual click.

When multiple video attachments exist, the script exits with code `5` (AMBIGUOUS) and prints the candidates JSON on stdout — the agent then asks the user to pick, and re-runs with `--attachment-id <ID>`.

## Ambiguous attachments (exit 5)

**Handle ambiguous Jira attachments.** If exit code is `5` (AMBIGUOUS), the JSON on stdout contains `"ambiguous": true`, the `issue_summary`, and a `"candidates"` array (each item: `id`, `filename`, `size_bytes`, `created`, `author`). The agent should:

- List the candidates to the user as a numbered selection: filename, size, who attached it, when. Brief — one line each.
- Wait for the user's pick.
- Re-run with `--attachment-id <ID>` (where `<ID>` is the `id` field from the chosen candidate).

Example:

```
PROJ-1234 has 3 video attachments:
1. before-fix.mp4 (12 MB, by Jane Doe on 2026-04-10)
2. after-fix.mp4 (8 MB, by Jane Doe on 2026-04-10)
3. customer-repro.mov (45 MB, by Support Bot on 2026-04-09)

Which one should I watch?
```

## No token: semi-auto fallback

**Fall back to semi-auto for Jira if no token.** If a Jira-key run (`watch_video.py PROJ-1234`) exits with code `2` (BAD_INPUT) and the message mentions `credentials not found`, fall back to:
- Call MCP `getJiraIssue` with `fields=["attachment", "summary"]`
- Filter attachments to `mimeType` starting with `video/`. If 0 → tell user, stop. If 1 → use it. If >1 → ask user.
- Print the attachment's web UI URL, ask user to click-download in the browser
- Run the orchestrator again with `auto` as input — it picks the freshly downloaded MP4
- Suggest: "to skip the manual download next time, see the watch-video skill's references/jira.md → Jira token setup"

## Auth note (Jira)

Two-tier support:

- **Full auto (recommended):** Configure an Atlassian API token once (see [Jira token setup](#jira-token-setup) at the top of this file). After that, `watch_video.py PROJ-1234` does everything — enumerate attachments, pick the video, download. No browser, no manual click. Works because Atlassian Cloud REST API accepts personal API tokens via HTTP Basic Auth, which the skill handles via `urllib` (range requests + retries for CDN robustness on large files).

- **Semi-auto fallback:** If no token is configured (or the org blocks API tokens), the skill uses the Atlassian MCP (OAuth via Claude) to enumerate attachments + print the attachment URL, then asks you to click-download in the browser. `fetch.py --auto-downloads` picks the freshly downloaded MP4 up automatically. Removes the "tell me the path" and "find the attachment" steps, but the click stays.

Note: Atlassian Cloud disabled password-based REST API auth in 2019 — a personal API token is the only practical way to authenticate from scripts. Most orgs allow personal tokens (they're scoped only to *your* permissions), but some admins disable them via the "User API tokens" policy; in that case, semi-auto is the only path.

## Fetching non-video Jira attachments (images, PDFs)

`scripts/fetch_attachment.py` downloads any attachment, not just the videos the
pipeline consumes. It exists because an agent sandbox usually cannot make the
authenticated Atlassian call itself, while this skill already holds the token.

```bash
# every image on the ticket -> ${TMPDIR:-/tmp}/jira-PROJ-1234/
python3 "${CLAUDE_SKILL_DIR}/scripts/fetch_attachment.py" PROJ-1234

# everything, into a chosen directory
python3 "${CLAUDE_SKILL_DIR}/scripts/fetch_attachment.py" PROJ-1234 --mime-prefix "" --outdir /tmp/proj-1234

# one specific attachment
python3 "${CLAUDE_SKILL_DIR}/scripts/fetch_attachment.py" PROJ-1234 --attachment-id 1001
```

Stdout is a JSON array of `{id, filename, mime_type, size_bytes, path}`; open the
paths with an ordinary file read. Read-only: it never writes to Jira. Files above
50 MB are refused (single-id mode) or reported with `path: null` and
`skipped: "too_large"` (bulk mode). Via MCP the same thing is
`fetch_jira_attachment(jira_key, mime_prefix, attachment_id, outdir)`.
