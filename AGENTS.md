# Working on llm-dashboard

Guidance for coding agents (Claude Code, Codex, Cursor and friends) and for
humans who want the same shortcuts. The README explains what the project is;
this file explains how to change it without breaking it.

## What this is

A local macOS dashboard that polls Anthropic's OAuth usage endpoint for every
Claude Code profile on the machine, stores the readings in SQLite, indexes the
Claude Code transcripts under `~/.claude*/projects`, and serves a static
frontend that charts all of it. Python/FastAPI backend, vanilla JS frontend, no
build step. Read `README.md` → "How it works" first.

## Layout

| Path                            | Owns                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------- |
| `backend/main.py`               | FastAPI app, every `/api/*` route, static file serving.                                   |
| `backend/collector.py`          | The polling loop. Interval from `LLM_DASHBOARD_POLL_SECONDS`, 60 s retry on cached reads. |
| `backend/config.py`             | All `LLM_DASHBOARD_*` environment variables. Add new settings here, nowhere else.         |
| `backend/database.py`           | SQLite schema for subscriptions, snapshots, events; reset/exhaustion detection.           |
| `backend/providers/`            | `BaseProvider` and the Claude Code implementation (keychain, OAuth, fallbacks).           |
| `backend/claude_home.py`        | Where Claude Code keeps state and how a home dir maps to a keychain service.              |
| `backend/usage.py`              | Transcript scanner and the usage aggregates behind `/api/usage/*`.                        |
| `backend/skills.py`             | Skill / plugin / tool extraction from transcripts and their attribution model.            |
| `backend/pricing.py`            | Price table for the cost estimate. Relative weights, not a bill.                          |
| `frontend/`                     | `index.html` + `style.css` (theme tokens) + `theme.js`, `app.js`, `usage.js`, `nav.js`.   |
| `tests/`                        | pytest. Everything runs against temp databases and fake homes; nothing touches the Mac.   |
| `docs/`                         | API reference, usage-insights semantics, 24/7 service runbook, deploy procedure.          |
| `Makefile`, `*.sh`, `*.plist.*` | Dev entrypoints and the launchd service. `make help`.                                     |

## Commands

```bash
make setup     # uv sync + bun install (one-time)
make test      # pytest
make lint      # ruff check, ruff format --check, prettier --check, shellcheck
make format    # fix everything the linters can fix
make check     # lint + test: what CI runs, run it before opening a PR
make dev       # uvicorn --reload on :8001 so the 24/7 service on :8000 keeps running
```

Run a single test with `.venv/bin/python -m pytest tests/test_usage.py -k windows`.

## Rules that are not obvious from the code

- **Never poll the Anthropic API faster than 30 s** and never remove the
  cached-reading fallback. The `/api/oauth/usage` endpoint rate-limits (429)
  readily; a poll storm produces stale data, not more data. `MIN_POLL_SECONDS`
  in `backend/config.py` is the floor.
- **Every setting is an `LLM_DASHBOARD_*` environment variable**, read in
  `backend/config.py`, documented in the README "Configuration" table, and
  present in `com.armandbriere.llm-dashboard.plist.template`. A setting that is
  missing from any of the three is a bug.
- **Tests must stay hermetic.** Patch `backend.database.DB_PATH` (and reset
  `_initialized`), pass `user_home=` to anything that discovers Claude homes,
  and swap `collector.providers` for a fake. `tests/test_api.py` shows the
  pattern. CI runs on Linux with no keychain and no transcripts.
- **Frontend colours come from theme tokens.** `style.css` defines the
  `--*` tokens for four themes; `theme.js` exposes them to the charts. Do not
  hard-code a colour in CSS, JS or HTML. See `docs/usage-insights.md` for the
  meaning of the numbers you are rendering before you change a label.
- **The frontend has no build step and no framework.** `index.html` loads
  Chart.js from a CDN and the four scripts as classic globals. Keep new code in
  that style; do not introduce a bundler or a package for the frontend.
- **Transcript scans are incremental.** `usage_files` records the byte offset
  per transcript; a change to what is extracted must bump `SCAN_VERSION` in
  `backend/usage.py` so existing files are re-indexed.
- **SQLite schema changes are additive.** Both `database.py` and `usage.py`
  create tables with `IF NOT EXISTS` and migrate with `ALTER TABLE ... ADD
COLUMN` guarded by a `PRAGMA table_info` check. The database holds history
  that cannot be re-fetched, so never write a migration that drops data.
- **Deploy is not `git pull`.** launchd serves `~/src/llm_dashboard`, not the
  worktree you edit. `make deploy` is the only supported path; `docs/deploying.md`
  explains why. Never run `git clean -xfd` there: the database lives inside it,
  and never `sudo` it: the agent is per-user (`gui/<uid>`), so root only earns an
  opaque launchctl 125 plus root-owned files in the runtime checkout.
- **Naming trap**: the repo directory is `llm_dashboard` (underscore); the
  launchd label and log directory are `llm-dashboard` (hyphen).

## When you change...

| Change                            | Also do                                                                                        |
| --------------------------------- | ---------------------------------------------------------------------------------------------- |
| A `/api/*` route                  | Update `docs/api.md`.                                                                          |
| An environment variable           | `backend/config.py`, README table, plist template, `docs/running-24-7.md`.                     |
| What the transcript scanner reads | Bump `SCAN_VERSION`; add a fixture in `tests/test_usage.py` or `tests/test_skills.py`.         |
| How a usage number is computed    | Update `docs/usage-insights.md`; the labels in `usage.js` must still describe the number.      |
| `frontend/*.js` or `style.css`    | Bump the `?v=` query strings in `index.html` if already-open tabs must pick the change up.     |
| Pricing                           | `backend/pricing.py` and the pricing note in `docs/usage-insights.md`.                         |
| Shell scripts or the plist        | `make lint` runs shellcheck; the plist is validated by `plutil -lint` in `service.sh install`. |

## Style

- Python: ruff with the rule set in `pyproject.toml`, 120 columns, `from __future__ import annotations`, stdlib `logging` with `%s` formatting.
- JS/CSS/HTML/Markdown: prettier with `.prettierrc`. Run `make format`; do not fight it by hand.
- Comments explain _why_, not _what_. The existing code errs on the side of a short paragraph above anything non-obvious; keep that.
- Commit messages: imperative subject line, body says why. PRs: what changed, why, how it was verified.
