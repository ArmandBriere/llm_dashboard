# LLM Quota Tracker (Claude Code)

A lightweight, local web application to track, store, and visualize LLM quota utilization across multiple Claude Code subscriptions in real-time.

It integrates **directly and natively** with macOS Keychain credentials and Anthropic's OAuth APIs (`/api/oauth/profile` and `/api/oauth/usage`) with **zero dependency on `claude-swap`**.

---

## Claude Code Usage Insights

Below the quota chart the dashboard analyses your local Claude Code transcripts
(`~/.claude*/projects/**/*.jsonl`). Each assistant turn there records the model and
the exact token usage the API returned, which the OAuth usage endpoint does not
expose. The collector indexes new lines incrementally every poll (a cold scan of
~1,100 transcripts takes about 3 s) into `usage_turns`, and the section shows:

- **Stat tiles**: sessions, turns, tokens, output/thinking share, cache hit rate, tool calls, estimated API-equivalent cost per account.
- **5-hour windows**: every reset window the poller observed, with each model's share of the window's peak utilisation (share of estimated cost or raw tokens × peak %).
- **Model mix**, **cache & output** breakdown, **activity over time** (stacked by model), a **weekday × hour heatmap**, **projects** and a **sessions** table (title, project, branch, duration, turns, tools, tokens, models, cost).

Account attribution uses the Claude home directory: `~/.claude` maps to the default
keychain entry and any other `~/.claude_*` directory maps to the keychain entry whose
suffix is `sha256(path)[:8]`, the same rule Claude Code uses. Cost estimates come from
`backend/pricing.py` and are relative weights, not what a subscription bills.

## Features

- **Multi-Subscription Support**: Automatically discovers all local Claude Code subscriptions (`Vooban`, `VoobanLabs`, etc.) directly from macOS Keychain and local configurations.
- **Direct Anthropic API Integration**: Communicates directly with Anthropic's OAuth endpoints to fetch live utilization and token expiration.
- **5-Minute Periodic Polling**: Background collector runs every 5 minutes and persists quota snapshots to a lightweight SQLite database (`llm_dashboard.db`).
- **Smart Quota Reset & Exhaustion Detection**: Automatically detects when a 5-hour quota refreshes (a large utilization drop, or a small drop paired with a new reset deadline), a 7-day weekly reset occurs, or quota hits 100% capacity, logging distinct events.
- **Interactive Timeline Dashboard**:
  - Toggle one or several accounts with colour-coded chips (All, Vooban, VoobanLabs). Each account keeps one hue everywhere: cards, chart lines, reset markers, and the event log.
  - Filter by date (Today, All Time, or specific date).
  - Filter by hour of day with presets (Work day, Morning, Afternoon, Evening, 24h) or a dual-handle slider.
  - Toggle between **Usage % (0% -> 100%)** and **Remaining Quota % (100% -> 0%)**.
  - Visual event markers on the timeline chart, one per account and labelled with the account name.
  - Manual "Poll Now" trigger with live updating status.
- **Extensible Architecture**: Abstract `BaseProvider` interface makes adding OpenAI, Cursor, or Google Gemini trivial in the future.

---

## Discovered Subscriptions

| Subscription | Account Email | 5-Hour Session Quota | 7-Day Weekly Quota | Extra Usage Spend |
|---|---|:---:|:---:|:---:|
| **Vooban** | `armand.briere@vooban.com` | 100.0% | 32.0% | $100.10 / $100.00 CAD (100%) |
| **VoobanLabs** | `armand.briere@voobanlabs.com` | 100.0% | 28.0% | $0.00 / USD (Disabled) |

---

## Quick Start

Run the launcher script:

```bash
./start.sh
```

Or start manually:

```bash
# 1. Create and activate venv
uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python

# 2. Run the server
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open your browser at [http://127.0.0.1:8000](http://127.0.0.1:8000).

---

## Running 24/7 (macOS LaunchAgent)

To keep the collector polling continuously, install it as a user LaunchAgent:

```bash
./service.sh install     # render the plist, load it, start now
./service.sh status      # state / pid / last exit code
./service.sh logs        # tail stdout + stderr
./service.sh restart     # reload after a code change
./service.sh uninstall   # stop and remove
```

It starts at every login and restarts within ~10s if it crashes. Logs go to
`~/Library/Logs/llm-dashboard/`.

It must be a **user** LaunchAgent, not a LaunchDaemon or a container: the
provider reads your login keychain via `/usr/bin/security`, which only works
inside your logged-in GUI session.

The main caveat is sleep: a sleeping Mac leaves gaps in the timeline.

**See [docs/running-24-7.md](docs/running-24-7.md)** for the full runbook:
configuration, log rotation, sleep mitigations, and troubleshooting.

**See [docs/deploying.md](docs/deploying.md)** for how merged code reaches the
running service: which checkout launchd actually serves, what needs a restart,
how to verify, and how to roll back without destroying the quota history.

---

## API Endpoints

- `GET /api/subscriptions`: List all discovered subscriptions with their latest live quota snapshot.
- `GET /api/snapshots?subscription_ids=1,2&date=2026-09-08&start_hour=7&end_hour=21`: Query historical snapshots filtered by account, date, and hour range.
- `GET /api/events?subscription_ids=1&limit=50`: Get detected reset and refresh events.
- `POST /api/refresh`: Trigger an immediate live collection pass across all accounts.
- `GET /api/status`: Check collector health and next scheduled poll.

---

## Project Structure

```
llm_dashboard/
├── backend/
│   ├── database.py         # SQLite schema, snapshot logger, reset event detector
│   ├── collector.py        # 5-minute background polling loop
│   ├── main.py             # FastAPI application and API routes
│   └── providers/
│       ├── base.py         # Abstract BaseProvider interface
│       └── claude_code.py  # Native Claude Code Keychain & Anthropic OAuth client
├── frontend/
│   ├── index.html          # Web dashboard layout
│   ├── style.css           # "Console" design system: theme tokens and layout
│   ├── theme.js            # Theme registry, picker, and token reader for the charts
│   ├── app.js              # Chart.js timeline, quota gauges, filters, and polling
│   └── usage.js            # Claude Code usage insights parsed from local transcripts
├── tests/
│   ├── test_database.py    # Unit tests for database & event logic
│   └── test_api.py         # Integration tests for FastAPI endpoints
├── requirements.txt
├── start.sh                # Quick launch script (foreground, opens browser)
├── run-service.sh          # Service-mode launcher used by launchd
├── service.sh              # install / uninstall / restart / status / logs
├── com.armandbriere.llm-dashboard.plist.template
└── README.md
```
