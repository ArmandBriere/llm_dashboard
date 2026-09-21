# API reference

Everything the frontend shows comes from these endpoints. All responses are
JSON. There is no authentication: the server is meant to listen on loopback.

## Common query parameters

The quota and usage endpoints share one filter vocabulary, which is what lets
the frontend keep a single filter bar for every view.

| Parameter          | Type         | Meaning                                                              |
| ------------------ | ------------ | -------------------------------------------------------------------- |
| `subscription_ids` | `1,2`        | Comma-separated subscription ids. Non-numeric entries are ignored.   |
| `date`             | `YYYY-MM-DD` | One local day. Omit for all time.                                    |
| `start_hour`       | `0`–`23`     | First hour of day to include (local time).                           |
| `end_hour`         | `0`–`23`     | Last hour of day to include (local time).                            |
| `limit`            | integer      | Where a list can be long; each endpoint has its own default and cap. |

## Quota

| Method | Path                 | Returns                                                                                                                                             |
| ------ | -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`  | `/api/subscriptions` | Every account seen, with its latest snapshot (`five_hour_pct`, `seven_day_pct`, reset times, spend, `is_stale`).                                    |
| `GET`  | `/api/snapshots`     | Time series of snapshots. Filters: `subscription_ids`, `date`, `start_hour`, `end_hour`.                                                            |
| `GET`  | `/api/events`        | Detected `five_hour_reset`, `seven_day_reset` and `exhausted` events. Filters: `subscription_ids`, `date`, `limit` (default 100, max 500).          |
| `POST` | `/api/refresh`       | Runs one collection pass now. Returns the pass summary and the refreshed subscriptions.                                                             |
| `GET`  | `/api/status`        | Collector state: `status` (`idle`/`running`/`ok`/`stale`/`error`), `last_run_time`, `next_run_time`, `interval_seconds`, last `usage_scan` summary. |

## Usage insights

All of these read the transcript index. They accept the common filters unless
noted.

| Method | Path                        | Returns                                                                                                                                |
| ------ | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`  | `/api/usage/summary`        | Sessions, turns, tokens by kind, cache hit rate, tool calls and estimated cost, plus a per-account breakdown.                          |
| `GET`  | `/api/usage/models`         | Per-model turns, tokens and estimated cost.                                                                                            |
| `GET`  | `/api/usage/sessions`       | Sessions active in the window: title, project, branch, duration, models, skills, cost. `limit` default 60.                             |
| `GET`  | `/api/usage/projects`       | Per-project totals. `limit` default 15.                                                                                                |
| `GET`  | `/api/usage/heatmap`        | Weekday × hour activity matrix in local time.                                                                                          |
| `GET`  | `/api/usage/timeline`       | Tokens and cost per model per `bucket` (`hour` or `day`, default `hour`).                                                              |
| `GET`  | `/api/usage/windows`        | Observed 5-hour quota windows with each model's share of the window's peak. Filters: `subscription_ids`, `date`, `limit` (default 12). |
| `GET`  | `/api/usage/skills`         | Per-skill and per-plugin invocations, injected context and attributed work.                                                            |
| `GET`  | `/api/usage/tools`          | Tool-call leaderboard with totals per tool kind and MCP server. `limit` default 20.                                                    |
| `GET`  | `/api/usage/skill-timeline` | Skill invocations per `bucket` (`hour` or `day`, default `day`).                                                                       |
| `POST` | `/api/usage/rescan`         | Indexes new transcript lines now. Returns the scan summary.                                                                            |

## Static

| Path        | Serves                                                       |
| ----------- | ------------------------------------------------------------ |
| `/`         | `frontend/index.html`                                        |
| `/static/*` | The rest of `frontend/`, re-read from disk on every request. |

Every response carries `Cache-Control: no-cache, no-store, must-revalidate`, so
a frontend change needs a browser reload and nothing else.

## Examples

```bash
curl -s localhost:8000/api/status | jq
curl -s 'localhost:8000/api/snapshots?subscription_ids=1&date=2026-09-20&start_hour=8&end_hour=18' | jq length
curl -s -X POST localhost:8000/api/refresh | jq .summary
```
