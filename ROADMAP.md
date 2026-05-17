# Roadmap

Future improvements to `watch-video`, ranked by leverage (effort vs. payoff).
Snapshot date: 2026-05-16. Revisit after each release to re-prioritise.

---

## Status assessment (2026-05-17)

Current rating: **~7.5/10**. Real engineering + well-positioned competitively, but bounded by distribution gaps and thin test coverage. Trajectory is steep -- yesterday it was a private repo with safety bugs; today it's v2.0.0 public with an MCP wrapper across 9 hosts.

### Scored by dimension

| Dimension | Score | Notes |
|---|---|---|
| Feature breadth | 9/10 | Best-in-class Jira integration; only one with a write-safety stack |
| Cost per video | 10/10 | Cheapest credible Tier-3 option by 7-200x; documented in Atlassian case study |
| Wall-clock time (YouTube) | 10/10 | Structurally faster than any Whisper-based service (captions-first skips the work) |
| Privacy posture | 10/10 | Local-by-default; every external call is explicit opt-in |
| Documentation | 9/10 | Above 95th percentile of OSS docs at this size; every metric traceable |
| Roadmap clarity | 9/10 | Versioned, prioritised, dated |
| Polish | 8/10 | README hero card + Atlassian showcase artifact land well |
| Safety design | 10/10 | "No unsolicited Jira writes" encoded across CLI + MCP + memory rule |
| Code maturity (tests + CI) | 5/10 | Smoketest only; no CI; single contributor; review found bugs that had no test coverage |
| Distribution / discoverability | 4/10 | Self-hosted install works; awesome lists in flight; marketplace pending; zero promotion fired |
| Stars / mindshare | 2/10 | Brand new (public for 2 days); ~zero stars at the moment |

### The four highest-ROI fixes (do these next, in order)

| # | Item | Effort | Why it matters |
|---|---|---|---|
| A | **Record a 60-90 second demo video / asciinema** of a real run. Embed at top of README. | ~30 min | Single biggest credibility move. Current hero is static screenshots; a moving demo turns the abstract pitch into something visceral. |
| B | **Fire the already-drafted promotion posts** (HN + Twitter + the awesome-list PRs that are eligible). | ~1 hr | Drives the first 100 GitHub stars. Without traffic, the great docs are invisible. |
| C | **Add GitHub Actions CI** running `smoketest.py` on push. | ~30 min | Signals maturity to anyone evaluating. Catches regressions in the long tail. |
| D | **Test in Claude Desktop directly** (the actual production target). | ~10 min | MCP wrapper is verified via Inspector but not via Claude Desktop yet. Close this loop before promoting. |

Doing all four moves the plugin from "interesting demo with great docs" to "trustworthy production-grade tool" within a week.

### Skip-for-now (don't burn effort here until A-D land)

- GitHub Issues integration (v2.1.0)
- Linear integration (v2.1.0)
- Slack/Discord webhook (v2.1.0)
- Auto-dep installer
- Audio-only mode
- Live screen capture mode
- ChatGPT Custom GPT support (skip indefinitely; SaaS deployment is a different product)

### Real UI bug-triage walkthrough (separate concern)

The Jira-native fetch is the most differentiated feature, but the README claims it without showing it on a real bug. Adding a 60-90s screen-recording of the skill watching a bug video and posting back to a Jira issue would close the biggest **credibility** gap (separate from the four marketing gaps above).

Status: deferred 3 times so far this week. User decided to record their own once the day-to-day Atlassian/Marcin time permits.

---

## Architecture decision: CLI-first + thin MCP wrapper

**Status:** SHIPPED in v2.0.0. See `mcp-server/`.

The CLI is the canonical interface and the source of truth. An MCP server is a thin adapter (~150-250 LOC) that calls the existing scripts under the hood. This shape gives the broadest reach with minimum duplication.

**Why CLI as the core:**

- Pipeline is filesystem-oriented (workdirs, atomic-staged writes, per-step cache, artifacts the user can `cat` / open / grep). CLI is native to that shape.
- Easy to debug (`python scripts/watch_video.py foo.mp4 --verbose`), pipeable, CI-friendly (GitHub Actions etc.).
- Smoketest stays a plain `python` invocation -- no MCP client needed.

**Why an MCP wrapper as the second front door:**

- Non-CLI hosts (Claude Desktop, Cursor, Continue.dev, Cline, Windsurf, Zed, VS Code Copilot Tool Mode, Codex CLI's MCP mode) can't shell out -- they speak MCP only.
- One wrapper unlocks ~9 surfaces.
- Wrapper is glue: `subprocess.run(["python", "scripts/watch_video.py", ...])` per MCP tool. No business logic duplication.

**MCP tool shape (proposed):**

| MCP tool | Wraps | Returns |
|---|---|---|
| `watch_video(input, options)` | `watch_video.py` | workdir path, `meta.json` summary |
| `read_transcript(workdir)` | reads `transcript.md` | string |
| `read_report(workdir, format)` | reads report.md / .html / .docx | string or path |
| `read_highlights(workdir)` | reads `highlights.json` | structured picks |
| `post_to_jira(workdir, options)` | `post_to_jira.py` | comment id, status |

Confirmation gate for `post_to_jira` lives in the wrapper -- requires `confirm=true` in the tool call OR a separate interactive confirmation. Same "no unsolicited Jira writes" posture as the CLI.

## Ranked improvements

### High leverage (do first)

| # | Item | Effort | Status |
|---|---|---|---|
| 1 | **MCP server wrapper.** Thin adapter calling existing scripts. New `mcp-server/` dir; published as `claude-watch-video-mcp` install option. | 1-2d | **Shipped in v2.0.0** (6 tools + 1 resource; FastMCP-based; `post_to_jira` dry-run default with explicit `confirm=True` for writes). Unlocks Claude Desktop, Codex MCP-mode, Cursor, Continue, Cline, Windsurf, Zed, VS Code Copilot Chat. |
| 2 | **Free YouTube captions first, Whisper fallback.** `yt-dlp --writesubtitles` before paying for Whisper. | 2-3h | **Shipped in v1.13.0** (7.6x speedup on YouTube vs local Whisper). |
| 3 | **GitHub Issues integration** alongside Jira. Same code structure (`fetch.py`, `post_to_jira.py` analogues). | 1d | Planned for v2.1.0. 10x audience -- GitHub has millions of devs vs. Jira's tens of thousands. |
| 4 | **Multi-provider highlights** (`--highlights-provider openai|anthropic|groq`). | ~2h | **Shipped in v1.13.0.** |
| 5 | **README "Use cases" expansion**. | 2h | **Shipped in v1.13.0** -- 8 new scenarios + new-flag rows in the config table. |

### Medium leverage

| # | Item | Effort | Status |
|---|---|---|---|
| 6 | **Auto-dep installer** (`python scripts/setup.py`) -- winget/brew/apt detection. | 4h | Planned. Drops first-time friction. |
| 7 | **Linear integration**. Same shape as Jira/GitHub Issues. | 1d | Planned for v2.1.0. ~200k devs use Linear. |
| 8 | **Adaptive frame budget by duration**. | 1h | **Already in place** since v1.10; v1.13.0 tweaked the short-video curve and added inline rationale doc. |
| 9 | **CI integration recipe** -- example GitHub Actions workflow processing Playwright/Cypress video output on test failure. | 2h | **Drafted in README v1.13.0 Use Cases**; a full GitHub Actions YAML lands in v2.1.0. |
| 10 | **Slack/Discord webhook output** -- post highlights to a channel as an alternative to (or alongside) Jira. | 1d | Planned. Closes the loop for non-Jira teams. |

### Lower leverage / nice-to-have

| # | Item | Effort | Why |
|---|---|---|---|
| 11 | **Gemini backend** for video understanding (native YouTube URL support, no download). | 1d | Trade-off: less local, less private. Worth it for users with Gemini-only billing. |
| 12 | **Audio-only mode** flag. | 2h | Podcasts/interviews become same pipeline. Small audience expansion. |
| 13 | **VTT/SRT export** of transcript. | 1h | Niche; some workflows need it. |
| 14 | **Live screen capture mode** (record + process). | 2-3d | Narrow audience. Defer unless requested. |
| 15 | **ChatGPT Custom GPT support.** | weeks | Requires hosted SaaS deployment -- different product category. Defer indefinitely unless user demand appears. |

## Use cases worth surfacing in README

Each maps to a real, existing audience. Currently under-marketed.

1. **Sprint retro on video bug tickets** -- already supported via `watch_batch.py --jira-jql`; not surfaced as a primary use case.
2. **GitHub Issues / Linear / Asana integration** -- after items #3 / #7.
3. **Customer-support video tickets** (Zendesk / Freshdesk / Intercom attachments) -- same fetch pattern as Jira.
4. **CI/CD bug-repro auto-analysis** -- every Playwright/Cypress test failure that uploads a video gets a 30-second auto-report.
5. **Onboarding videos -> process docs** -- record a Loom of "how to release X", get a Markdown checklist.
6. **Compliance / call review** -- 100% local Whisper mode -> no data leaves the machine. Suitable for regulated industries.
7. **Lecture / classroom notes** -- transcript + highlights = automated study guide.
8. **Loom alternative for async dev demos** -- record once, share the `report.html` link, no one has to watch.
9. **Knowledge-base ingestion** -- process a backlog of training videos into searchable text.
10. **Sales / support call review** -- privacy-preserving alternative to Gong / Chorus / Fathom.

## Competitive landscape snapshot

Direct Claude Code video plugins (2026-05):

| Plugin | What it does | Where it falls short vs. watch-video |
|---|---|---|
| [bradautomates/claude-video](https://github.com/bradautomates/claude-video) | URL/local -> frames + transcript. Adaptive frame budget. Captions first. | No Jira, no OCR, no highlights, no batch, no DOCX/HTML, no smart dedup. |
| [jordanrendric/claude-video-vision](https://github.com/jordanrendric/claude-video-vision) | Multi-backend (Gemini/Whisper/OpenAI). MCP-based. Multiple slash commands. | v1.0.0, only tested macOS Apple Silicon. No Jira, no OCR, no batch, no highlights. |
| Various YouTube transcript MCPs | Transcript fetching only. | No frames, no OCR, no Jira workflow, no LLM-driven moment picking. |

**watch-video's unique combination:** Jira-native fetch + Jira opt-in post + OCR (screen-recording tuned) + transcript-aware smart dedup + user-prompt LLM highlights + batch mode + three report formats + per-step content-hash cache.

**Strategic positioning:** lean into the Jira / bug-triage / sprint-retro angle in marketing. Generic YouTube transcription is commoditized; the dev-workflow integration is differentiated.

## Versioning notes

- **v1.12.x branch** -- bug fixes, security/safety improvements, doc polish. (Shipped.)
- **v1.13.0** -- items #2, #4, #5, #8: captions-first transcription, multi-provider highlights, expanded Use Cases, frame-budget tuning. (Shipped 2026-05-16.)
- **v2.0.0** -- the MCP wrapper (item #1). (Shipped 2026-05-16.) New install surface justified the major bump.
- **v2.1+** -- GitHub Issues (#3), Linear (#7), CI recipe (#9), Slack/Discord (#10).

## Sources for future reference

- Anthropic plugins reference: <https://code.claude.com/docs/en/plugins-reference>
- Codex CLI MCP support: <https://developers.openai.com/codex/mcp>
- MCP spec / SDKs: <https://modelcontextprotocol.io/>
- Anthropic plugin submission: <https://claude.ai/settings/plugins/submit>
