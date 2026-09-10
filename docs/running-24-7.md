# Running 24/7 on macOS

Operational runbook for keeping the quota collector polling continuously as a
background service.

- **Label**: `com.armandbriere.llm-dashboard`
- **Agent plist**: `~/Library/LaunchAgents/com.armandbriere.llm-dashboard.plist`
- **launchd domain**: `gui/$(id -u)`
- **Logs**: `~/Library/Logs/llm-dashboard/{stdout,stderr}.log`
- **Dashboard**: <http://127.0.0.1:8000>

---

## Why a LaunchAgent

`ClaudeCodeProvider` shells out to `/usr/bin/security find-generic-password` to
read Claude Code credentials (`backend/providers/claude_code.py`). That reaches
the **login keychain**, which is only unlocked and reachable from inside your
logged-in GUI session. Everything follows from that constraint:

| Option | Verdict | Reason |
|---|---|---|
| **User LaunchAgent** | ✅ Chosen | Runs as you, in the Aqua session. Keychain works. Native to macOS, no extra runtime. |
| LaunchDaemon | ❌ | Runs as root before login, against the System keychain. `find-generic-password` finds nothing. |
| Docker / container | ❌ | No macOS keychain, no `/usr/bin/security`. Credentials would have to be exported and kept in sync by hand. |
| `nohup` / `screen` / `tmux` | ❌ | Dies with the terminal session or at logout. No restart-on-crash, no start-at-login. |
| Remote host (VM, cloud) | ⚠️ | Never sleeps, but has no access to this Mac's keychain. Would need a different credential source entirely. |

The plist pins this with `LimitLoadToSessionType = Aqua`, so the agent only ever
loads in a GUI login session.

---

## Files

| File | Role |
|---|---|
| `service.sh` | Management CLI: `install` / `uninstall` / `restart` / `status` / `logs`. |
| `run-service.sh` | What launchd actually executes. Service-mode launcher: no browser, no TTY, bootstraps the venv, rotates logs. |
| `com.armandbriere.llm-dashboard.plist.template` | Source of truth for the agent. `service.sh install` renders `__APP_DIR__` / `__HOME__` into the real plist. |

The rendered plist under `~/Library/LaunchAgents/` is **generated**. Edit the
template and re-run `install`, never the rendered copy, or your change is lost on
the next install.

`run-service.sh` exists separately from `start.sh` because `start.sh` opens a
browser and assumes an interactive terminal. Both are kept: `start.sh` for
one-off foreground runs, `run-service.sh` for the service.

---

## Install

```bash
./service.sh install
```

This renders the plist, lints it with `plutil`, boots out any previous version,
bootstraps the agent into `gui/$UID`, and starts it immediately. It is
idempotent, so it doubles as the upgrade path after you edit the template.

Behaviour once installed:

- **Starts at every login** (`RunAtLoad`).
- **Restarts on crash**, no faster than every 10s (`KeepAlive` + `ThrottleInterval`).
- **Survives logout/login cycles** and reboots.

### Verify

```bash
./service.sh status
curl -s http://127.0.0.1:8000/api/status
```

`status` should report `state = running` with a PID. The API should return
`{"status":"ok", ...}` with a recent `last_run_time`.

To prove auto-restart actually works, kill it and watch launchd bring it back
with a new PID:

```bash
kill -9 $(launchctl print gui/$(id -u)/com.armandbriere.llm-dashboard | awk '/^\tpid = /{print $3}')
sleep 5 && ./service.sh status
```

---

## Day-to-day

```bash
./service.sh status      # state / pid / last exit code
./service.sh logs        # tail -f stdout + stderr
./service.sh restart     # kickstart -k, after a code change
./service.sh uninstall   # bootout + remove the plist
```

After changing anything under `backend/`, run `./service.sh restart`. The server
is not started with `--reload`, so code changes are not picked up otherwise.

For pulling merged code into the running service — which checkout launchd
serves, what needs a restart versus a plain browser reload, verification and
rollback — see [deploying.md](deploying.md).

---

## Configuration

Edit the `EnvironmentVariables` dict in
`com.armandbriere.llm-dashboard.plist.template`, then re-run `./service.sh install`.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Listen address. Loopback only by default. |
| `PORT` | `8000` | Listen port. |
| `LLM_DASHBOARD_LOG_DIR` | `~/Library/Logs/llm-dashboard` | Where `run-service.sh` writes and rotates logs. |
| `LLM_DASHBOARD_CAFFEINATE` | `0` | `1` wraps uvicorn in `caffeinate -is` to block idle sleep. |
| `LLM_DASHBOARD_DB_PATH` | `<repo>/llm_dashboard.db` | Read by `backend/database.py`. Add it to the plist to relocate the database. |

`StandardOutPath` / `StandardErrorPath` are separate keys in the plist and are
**not** driven by `LLM_DASHBOARD_LOG_DIR`. If you change the log directory,
change all three.

Binding `HOST` to `0.0.0.0` exposes live quota data and account emails to your
whole network with no authentication in front of it. Keep it on loopback unless
you have deliberately put something in front.

---

## Logs

launchd truncates nothing and rotates nothing. `run-service.sh` handles it at
startup: any log over 5 MB is moved to `.1` before the server boots. Only one
generation is kept.

Because rotation happens **at startup**, a service that runs for months without
restarting never rotates. If that matters, restart it periodically or add a
`newsyslog.d` entry.

```bash
tail -f ~/Library/Logs/llm-dashboard/stdout.log
```

Uvicorn and the app log at `INFO` to stdout; Python tracebacks land in stderr.

---

## Sleep: the real limitation

A sleeping Mac stops polling and leaves gaps in the timeline. The collector
recovers on its own after wake, but the snapshots for the sleep window are
simply never taken.

Options, ranked by how well they hold up:

1. **Keep it plugged in**, and set System Settings → Battery → Options →
   *Prevent automatic sleeping on power adapter when the display is off*.
   Check the current state with `pmset -g | grep '^ sleep'` (`0` means never).
2. **`LLM_DASHBOARD_CAFFEINATE=1`**: holds a power assertion for exactly as long
   as the service runs. Same effect, scoped to this app rather than system-wide.
   Verify with `pmset -g assertions`.
3. **`sudo pmset repeat wakeorpoweron MTWRFSU 06:00:00`**: let it sleep, but
   guarantee a daily wake so the gap is bounded.

None of these survive **closing the lid on battery**. macOS sleeps regardless
unless you are in clamshell mode with both external power and an external
display. There is no software fix for this from inside the app.

If you need genuinely uninterrupted coverage, the collector has to live on a
machine that never sleeps, which means solving credential access first, since
the keychain is local to this Mac.

---

## Troubleshooting

**`status` says "is not loaded"**
The agent was never bootstrapped, or you are in a non-GUI session (SSH).
`LimitLoadToSessionType = Aqua` means it will not load over SSH. Run
`./service.sh install` from a normal desktop terminal.

**Service flaps: `last exit code` is non-zero and the PID keeps changing**
Read `~/Library/Logs/llm-dashboard/stderr.log`. Most common causes: port 8000
already taken, or a missing dependency in `.venv`. Check the port with:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

If `start.sh` is already running in a terminal, it owns the port and the service
cannot bind. Stop one of them.

**No subscriptions, or `/api/subscriptions` returns `[]`**
Keychain access is failing. The provider tries several service names (the plain
`Claude Code-credentials`, plus one per `~/.claude*` directory suffixed with a
hash of its path). List what actually exists, then read one:

```bash
/usr/bin/security dump-keychain | grep 'Claude Code-credentials'
/usr/bin/security find-generic-password -s "<service-name>" -a "$USER" -w
```

On the first run after install, macOS may raise a GUI dialog asking whether
`security` may read the item. Choose **Always Allow**. If the prompt was
dismissed, the read fails silently from then on; re-run the command above in a
terminal to get the prompt back.

**Timeline has a gap**
Almost always sleep. Cross-check against the sleep/wake history:

```bash
pmset -g log | grep -E 'Sleep|Wake' | tail -20
```

**Changes to `backend/` have no effect**
The service does not hot-reload. `./service.sh restart`.

**`plutil -lint` fails during install**
The template has malformed XML. Fix the template; `install` deliberately refuses
to load an invalid plist.

---

## Uninstall

```bash
./service.sh uninstall
```

Boots the agent out of `gui/$UID` and deletes the rendered plist. Leaves the
database, logs, and repo scripts untouched. To go all the way:

```bash
rm -rf ~/Library/Logs/llm-dashboard
```
