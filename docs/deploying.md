# Deploying and updating the runtime

How merged code actually reaches the running dashboard on this Mac. For the
service itself — why it is a LaunchAgent, install, configuration, sleep — see
[running-24-7.md](running-24-7.md).

- **Runtime checkout**: `~/src/llm_dashboard` (underscore)
- **Database**: `~/src/llm_dashboard/llm_dashboard.db`, inside the checkout, gitignored
- **Dashboard**: <http://127.0.0.1:8000>

---

## There is no build step

The backend is Python run from source and the frontend is static files served off
disk. Nothing is compiled, bundled, or copied into a separate artifact:

| Layer | How it is served |
|---|---|
| Backend | `uvicorn backend.main:app`, run from the checkout by `run-service.sh`. |
| Frontend | `app.mount("/static", StaticFiles(directory=FRONTEND_DIR))` in `backend/main.py`, reading `frontend/` on every request. |

So "deploying" means exactly one thing: **make the runtime checkout's working
tree match the ref you want, then restart the process if the change was
server-side.** Merging a PR on GitHub does nothing to this Mac on its own.

---

## Find the runtime first

The directory launchd runs from is authoritative, and it is *not* wherever you
happen to be editing. If you work in git worktrees (`~/.t3/worktrees/llm_dashboard/<branch>`),
your working copy and the runtime are different directories on different refs.

```bash
launchctl print gui/$(id -u)/com.armandbriere.llm-dashboard \
  | grep -E '^\s+(state|pid|program) '
awk '/WorkingDirectory/{getline; print}' \
  ~/Library/LaunchAgents/com.armandbriere.llm-dashboard.plist
```

Note the naming split, which is easy to mistype: the **repo directory** is
`llm_dashboard` (underscore), while the **launchd label and log directory** are
`llm-dashboard` (hyphen).

---

## Update procedure

```bash
cd ~/src/llm_dashboard
git fetch origin
git status --short                                          # expect no output
git merge --ff-only origin/main
uv pip install -r requirements.txt --python .venv/bin/python
./service.sh restart
```

Two deliberate choices in there:

- **`git status --short` must be empty.** The runtime is not a place to keep
  local edits; anything uncommitted there is invisible work that the next update
  will collide with.
- **`--ff-only`.** If it refuses, the runtime checkout has diverged from
  `origin/main` — someone committed directly into it. Resolve that on purpose
  rather than letting a merge commit appear in the runtime.

The dependency sync is cheap when nothing changed (`Checked 5 packages in 15ms`),
so it is worth running unconditionally rather than remembering whether
`requirements.txt` moved.

---

## What needs a restart

| Changed | Action |
|---|---|
| `frontend/**` | **Nothing.** Reload the browser. |
| `backend/**` | `./service.sh restart` — uvicorn does not run with `--reload`. |
| `requirements.txt` | `uv pip install -r requirements.txt --python .venv/bin/python`, then restart. |
| `run-service.sh` | `./service.sh restart`. |
| `*.plist.template` | `./service.sh install` — it re-renders the plist. A restart alone reuses the old one. |

Frontend changes need no restart *and* no hard reload: `StaticFiles` re-reads
from disk per request, and the `add_no_cache_headers` middleware in
`backend/main.py` stamps `Cache-Control: no-store, must-revalidate` on every
response. The `?v=` query strings in `index.html` are belt-and-braces for
already-open tabs and stale intermediaries; bump them when you want to be
certain a long-lived tab re-fetches, not as part of every deploy.

---

## Verify

Check the **served bytes**, not the files on disk — that is the only thing that
proves the running process picked the change up.

```bash
./service.sh status                                       # state = running, new pid
git -C ~/src/llm_dashboard log --oneline -1               # the ref you deployed
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/api/status
curl -s http://127.0.0.1:8000/ | grep -o 'data-theme="[^"]*"'   # a marker from the new code
```

`/api/status` should return `"status":"ok"` with a recent `last_run_time`. Pick
the `grep` marker from whatever the deploy actually introduced (a new element id,
a new title, a newly added `/static/*.js` returning `200`).

---

## Rollback

```bash
cd ~/src/llm_dashboard
git log --oneline -5
git reset --hard <good-sha>
./service.sh restart
```

`reset --hard` leaves the database alone — it is gitignored, so it is untracked
as far as git is concerned.

**Never run `git clean -xfd` in the runtime checkout.** `llm_dashboard.db` lives
inside it and holds all quota history and the transcript index (tens of MB, and
not reconstructible: the Anthropic usage API only reports *current* utilization).
`.venv/` would go with it. If you need the checkout pristine, back the database
up first or point `LLM_DASHBOARD_DB_PATH` outside the repo.

---

## Gotchas

**A restart forces an immediate collection pass, which can come back empty.**
Startup runs one collection before the 5-minute loop begins. Anthropic's
`/api/oauth/usage` rate-limits, and on a 429 the provider falls back to its disk
cache — but if the quota window already reset, that cache is discarded as stale,
so the pass records 0 accounts:

```
[WARNING] Anthropic usage API rate-limited (429). Falling back to cached utilization.
[WARNING] Discarding disk cache for <id>: its quota window already reset...
[WARNING] Skipping snapshot insertion for <email> due to missing quota metrics.
```

This is not a failed deploy. The chart may look empty or stalled until the next
successful poll (≤ 5 min). Restarting repeatedly to "fix" it makes the 429s
worse. Confirm recovery with `curl -s http://127.0.0.1:8000/api/status`.

**Port 8000 may already be taken by a foreground run.** `./start.sh` in a
terminal owns the port and the service cannot bind, so it flaps. Check with
`lsof -nP -iTCP:8000 -sTCP:LISTEN` and stop one of them.

**Log rotation only happens at startup.** `run-service.sh` rolls any log over
5 MB when it boots, so a deploy restart is also the only time logs rotate.
