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

| Layer    | How it is served                                                                                                        |
| -------- | ----------------------------------------------------------------------------------------------------------------------- |
| Backend  | `uvicorn backend.main:app`, run from the checkout by `run-service.sh`.                                                  |
| Frontend | `app.mount("/static", StaticFiles(directory=FRONTEND_DIR))` in `backend/main.py`, reading `frontend/` on every request. |

So "deploying" means exactly one thing: **make the runtime checkout's working
tree match the ref you want, then restart the process if the change was
server-side.** Merging a PR on GitHub does nothing to this Mac on its own.

---

## Find the runtime first

The directory launchd runs from is authoritative, and it is _not_ wherever you
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
make
```

That is the whole deploy, and it works from any checkout — including a worktree,
since every step targets `RUNTIME_DIR` (`~/src/llm_dashboard`) rather than
`$PWD`. It runs `sync` -> `deps` -> `restart` -> `verify`, which is:

```bash
cd ~/src/llm_dashboard
git fetch origin
git status --short                                          # expect no output
git merge --ff-only origin/main
uv sync --no-dev
./service.sh restart
```

To deploy something other than `origin/main`, override `REF` — it still has to
fast-forward from what the runtime currently has:

```bash
make REF=origin/my-branch
```

Two deliberate choices in there:

- **`git status --short` must be empty.** The runtime is not a place to keep
  local edits; anything uncommitted there is invisible work that the next update
  will collide with.
- **`--ff-only`.** If it refuses, the runtime checkout has diverged from
  `origin/main` — someone committed directly into it. Resolve that on purpose
  rather than letting a merge commit appear in the runtime.

The dependency sync is cheap when nothing changed (`uv sync` only compares the
lockfile against the venv), so it is worth running unconditionally rather than
remembering whether `uv.lock` moved.

---

## What needs a restart

| Changed                      | Action                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------- |
| `frontend/**`                | **Nothing.** Reload the browser.                                                      |
| `backend/**`                 | `./service.sh restart` — uvicorn does not run with `--reload`.                        |
| `pyproject.toml` / `uv.lock` | `uv sync --no-dev` (or `make deps`), then restart.                                    |
| `run-service.sh`             | `./service.sh restart`.                                                               |
| `*.plist.template`           | `./service.sh install` — it re-renders the plist. A restart alone reuses the old one. |

`make` covers every row unconditionally: `make restart` diffs the rendered plist
against the template and escalates to `service.sh install` when they differ, so
you never have to remember which of the two a change needed. `service.sh
restart` also bootstraps the agent when it is not loaded at all, so a deploy
after `make stop`, an `uninstall`, or a logout works the same as any other.

Frontend changes need no restart _and_ no hard reload: `StaticFiles` re-reads
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
git -C ~/src/llm_dashboard log --oneline -5
make rollback SHA=<good-sha>
```

`rollback` resets the runtime to `SHA` and then re-runs `deps`, `restart` and
`verify`, so a rollback is verified the same way a deploy is.

`reset --hard` leaves the database alone — it is gitignored, so it is untracked
as far as git is concerned.

**Never run `git clean -xfd` in the runtime checkout.** `llm_dashboard.db` lives
inside it and holds all quota history and the transcript index (tens of MB, and
not reconstructible: the Anthropic usage API only reports _current_ utilization).
`.venv/` would go with it. If you need the checkout pristine, back the database
up first or point `LLM_DASHBOARD_DB_PATH` outside the repo.

---

## Gotchas

**A restart forces an immediate collection pass, which can come back empty.**
Startup runs one collection before the polling loop begins. Anthropic's
`/api/oauth/usage` rate-limits, and on a 429 the provider falls back to its disk
cache — but if the quota window already reset, that cache is discarded as stale,
so the pass records 0 accounts:

```
[WARNING] Anthropic usage API rate-limited (429). Falling back to cached utilization.
[WARNING] Discarding disk cache for <id>: its quota window already reset...
[WARNING] Skipping snapshot insertion for <email> due to missing quota metrics.
```

This is not a failed deploy. The chart may look empty or stalled until the next
successful poll (one `LLM_DASHBOARD_POLL_SECONDS`, 5 minutes by default). Restarting repeatedly to "fix" it makes the 429s
worse. Confirm recovery with `curl -s http://127.0.0.1:8000/api/status`.

**Never `sudo make`.** Nothing in the deploy wants privileges. The dashboard is
a _per-user_ LaunchAgent living in the `gui/<uid>` domain, and under `sudo` the
uid is 0 — a domain that does not exist — so launchctl fails with an opaque

```
Could not kickstart service "com.armandbriere.llm-dashboard": 125: Domain does not support specified action
```

Worse than the failure is what precedes it: `git fetch` and `uv sync` have
already run as root and can leave root-owned files in the runtime checkout and
the uv cache, which then break the _next_ unprivileged deploy. `make` and
`service.sh` both refuse to run as root for this reason. Run them as yourself.

**Port 8000 may already be taken by a foreground run.** `./start.sh` in a
terminal owns the port and the service cannot bind, so it flaps. Check with
`lsof -nP -iTCP:8000 -sTCP:LISTEN` and stop one of them.

**Log rotation only happens at startup.** `run-service.sh` rolls any log over
5 MB when it boots, so a deploy restart is also the only time logs rotate.
