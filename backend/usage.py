"""Local Claude Code usage analytics.

Claude Code writes one JSONL transcript per session under
``~/.claude*/projects/<project-slug>/<session-id>.jsonl``. Every assistant turn
in those files carries the model and the token usage the API reported, so they
are a far richer source than the OAuth usage endpoint (which only exposes
percentages). This module scans those transcripts incrementally into SQLite and
exposes aggregate queries for the dashboard.

Account attribution: each Claude home directory has its own keychain entry
(``Claude Code-credentials-<sha256(path)[:8]>``, or the bare service name for
``~/.claude``). The quota collector stores that service name on the
subscription row, so a transcript's home directory maps to a subscription.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.claude_home import DEFAULT_KEYCHAIN_SERVICE as DEFAULT_KEYCHAIN_SERVICE
from backend.claude_home import discover_claude_homes as discover_homes
from backend.claude_home import keychain_service_for_home
from backend.database import RESET_WINDOW_TOLERANCE_S, get_db
from backend.pricing import estimate_cost_usd, pretty_model_name
from backend.skills import (
    SKILLS_SCHEMA,
    SkillRecorder,
    delete_file_rows,
    get_session_skills,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

USAGE_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS usage_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    account_key TEXT NOT NULL,
    project_slug TEXT,
    session_id TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    offset INTEGER NOT NULL DEFAULT 0,
    scan_version INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES usage_files(id) ON DELETE CASCADE,
    account_key TEXT NOT NULL,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    session_id TEXT NOT NULL,
    project_dir TEXT,
    project TEXT,
    git_branch TEXT,
    model TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    message_id TEXT,
    request_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_1h_tokens INTEGER NOT NULL DEFAULT 0,
    cache_5m_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    speed TEXT,
    service_tier TEXT,
    cli_version TEXT,
    est_cost_usd REAL NOT NULL DEFAULT 0,
    UNIQUE(account_key, message_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_turns_time ON usage_turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_turns_sub_time ON usage_turns(subscription_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_turns_session ON usage_turns(session_id);

CREATE TABLE IF NOT EXISTS usage_sessions (
    session_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    project_dir TEXT,
    title TEXT,
    first_prompt TEXT,
    first_prompt_at TEXT
);
"""
    + SKILLS_SCHEMA
)

# Bumped whenever a scan extracts something new from lines it has already read.
# A transcript indexed by an older version is re-read from the start, after its
# derived rows are dropped, so history survives even for files since deleted.
SCAN_VERSION = 2


def _migrate_usage_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the usage tables were first created."""
    file_cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_files)")}
    if "scan_version" not in file_cols:
        conn.execute("ALTER TABLE usage_files ADD COLUMN scan_version INTEGER NOT NULL DEFAULT 0")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_turns)")}
    if "project" not in cols:
        conn.execute("ALTER TABLE usage_turns ADD COLUMN project TEXT")
    rows = conn.execute("SELECT DISTINCT project_dir FROM usage_turns WHERE project IS NULL").fetchall()
    for r in rows:
        conn.execute(
            "UPDATE usage_turns SET project = ? WHERE project IS NULL AND project_dir IS ?",
            (canonical_project(r["project_dir"]), r["project_dir"]),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_turns_project ON usage_turns(project)")


def init_usage_schema(db_path: Path | str | None = None) -> None:
    """Create the usage tables if they do not exist."""
    with get_db(db_path) as conn:
        conn.executescript(USAGE_SCHEMA)
        _migrate_usage_schema(conn)


# Directory names that hold worktrees of the repository above them.
WORKTREE_MARKERS = (".claude/worktrees", ".claude-worktrees", ".worktrees", "worktrees")


def canonical_project(cwd: str | None) -> str:
    """Collapse a working directory to the repository it belongs to.

    Worktrees are where most work happens but they are not projects:
    ``~/.t3/worktrees/<repo>/t3code-<hash>`` and ``<repo>/.claude/worktrees/<name>``
    both belong to ``<repo>``, as do any subdirectories inside them.
    """
    if not cwd:
        return "(unknown)"
    parts = [p for p in Path(cwd).parts if p not in ("/", "")]
    # T3 Code runs one-shot helpers (session titles, branch names) in scratch dirs.
    if any(p.startswith("t3code-claude-title-") for p in parts):
        return "T3 Code helpers"
    # Memory directories live inside the Claude config tree, not in a repo.
    if ".claude" in parts and "projects" in parts and parts.index(".claude") < parts.index("projects"):
        return "(claude memory)"
    # macOS/Unix scratch space and the bare home directory are not projects.
    if parts[:1] == ["tmp"] or parts[:2] in (["private", "tmp"], ["private", "var"], ["var", "folders"]):
        return "(scratch)"
    if parts[:1] == ["Users"] and len(parts) == 2:
        return "(home)"
    # T3 Code worktrees: .../.t3/worktrees/<repo>/<worktree>/...
    for i in range(len(parts) - 2):
        if parts[i] == ".t3" and parts[i + 1] == "worktrees":
            return parts[i + 2]
    # Claude Code worktrees: .../<repo>/.claude/worktrees/<name>/...
    for i in range(1, len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return canonical_project(str(Path(*parts[:i])))
        if parts[i] in (".claude-worktrees", ".worktrees"):
            return canonical_project(str(Path(*parts[:i])))
    # A plain checkout under a source root: ~/src/<repo>/sub/dir -> <repo>
    for root in ("src", "code", "projects", "repos", "dev", "work", "git"):
        if root in parts:
            idx = parts.index(root)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return parts[-1] if parts else "(unknown)"


# --------------------------------------------------------------------------- #
# Discovery and account mapping
# --------------------------------------------------------------------------- #


def discover_claude_homes(user_home: Path | None = None) -> list[Path]:
    """Find every ``~/.claude*`` directory that holds session transcripts."""
    return discover_homes(user_home, with_transcripts=True)


def resolve_account_subscriptions(conn: sqlite3.Connection, homes: Iterable[Path]) -> dict[str, int | None]:
    """Map each home directory name (account_key) to a subscription id."""
    rows = conn.execute("SELECT id, keychain_service FROM subscriptions").fetchall()
    by_service = {r["keychain_service"]: r["id"] for r in rows if r["keychain_service"]}
    return {home.name: by_service.get(keychain_service_for_home(home)) for home in homes}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _normalize_ts(ts: str | None) -> str | None:
    """Store timestamps like quota snapshots do: ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``."""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    except ValueError:
        return None


def _first_user_text(message: Any) -> str | None:
    """Extract the human text from a user record, ignoring tool results."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        pieces = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(p for p in pieces if p).strip()
    else:
        return None
    if not text or text.startswith("<"):
        # System-injected reminders and command wrappers are XML-ish; skip them.
        return None
    return text[:300]


class _TurnAccumulator:
    """Collapses the several streamed lines of one assistant message into one row."""

    def __init__(self) -> None:
        self.turns: dict[tuple[str | None, str | None], dict[str, Any]] = {}

    def add(self, record: dict[str, Any], account_key: str, subscription_id: int | None) -> None:
        message = record.get("message") or {}
        model = message.get("model")
        usage = message.get("usage") or {}
        if not model or model.startswith("<") or not usage:
            return
        key = (message.get("id"), record.get("requestId"))
        content = message.get("content") or []
        tool_calls = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
        cache_detail = usage.get("cache_creation") or {}
        output_detail = usage.get("output_tokens_details") or {}
        row = {
            "account_key": account_key,
            "subscription_id": subscription_id,
            "session_id": record.get("sessionId") or "",
            "project_dir": record.get("cwd"),
            "project": canonical_project(record.get("cwd")),
            "git_branch": record.get("gitBranch"),
            "model": model,
            "timestamp": _normalize_ts(record.get("timestamp")),
            "message_id": key[0],
            "request_id": key[1],
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_1h_tokens": int(cache_detail.get("ephemeral_1h_input_tokens") or 0),
            "cache_5m_tokens": int(cache_detail.get("ephemeral_5m_input_tokens") or 0),
            "thinking_tokens": int(output_detail.get("thinking_tokens") or 0),
            "tool_calls": tool_calls,
            "is_sidechain": 1 if record.get("isSidechain") else 0,
            "speed": usage.get("speed"),
            "service_tier": usage.get("service_tier"),
            "cli_version": record.get("version"),
        }
        if row["timestamp"] is None or not row["session_id"]:
            return
        existing = self.turns.get(key)
        if existing:
            # Later lines of the same message carry the final usage; tool_use
            # blocks are spread across lines so those are summed.
            row["tool_calls"] += existing["tool_calls"]
            row["timestamp"] = existing["timestamp"]
        self.turns[key] = row


def _upsert_turns(conn: sqlite3.Connection, file_id: int, turns: Iterable[dict[str, Any]]) -> int:
    count = 0
    for t in turns:
        t["est_cost_usd"] = estimate_cost_usd(
            t["model"],
            t["input_tokens"],
            t["cache_creation_tokens"],
            t["cache_read_tokens"],
            t["output_tokens"],
        )
        conn.execute(
            """
            INSERT INTO usage_turns (
                file_id, account_key, subscription_id, session_id, project_dir, project, git_branch,
                model, timestamp, message_id, request_id,
                input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens,
                cache_1h_tokens, cache_5m_tokens, thinking_tokens, tool_calls, is_sidechain,
                speed, service_tier, cli_version, est_cost_usd
            ) VALUES (
                :file_id, :account_key, :subscription_id, :session_id, :project_dir, :project, :git_branch,
                :model, :timestamp, :message_id, :request_id,
                :input_tokens, :cache_creation_tokens, :cache_read_tokens, :output_tokens,
                :cache_1h_tokens, :cache_5m_tokens, :thinking_tokens, :tool_calls, :is_sidechain,
                :speed, :service_tier, :cli_version, :est_cost_usd
            )
            ON CONFLICT(account_key, message_id, request_id) DO UPDATE SET
                input_tokens = excluded.input_tokens,
                cache_creation_tokens = excluded.cache_creation_tokens,
                cache_read_tokens = excluded.cache_read_tokens,
                output_tokens = excluded.output_tokens,
                cache_1h_tokens = excluded.cache_1h_tokens,
                cache_5m_tokens = excluded.cache_5m_tokens,
                thinking_tokens = excluded.thinking_tokens,
                tool_calls = usage_turns.tool_calls + excluded.tool_calls,
                est_cost_usd = excluded.est_cost_usd
            """,
            {"file_id": file_id, **t},
        )
        count += 1
    return count


def _scan_file(
    conn: sqlite3.Connection,
    path: Path,
    account_key: str,
    subscription_id: int | None,
) -> int:
    """Parse new bytes of one transcript. Returns the number of turns written."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0

    # <home>/projects/<project-slug>/<session>.jsonl, or nested
    # <session>/subagents/agent-*.jsonl for subagent transcripts.
    parts = path.parts
    try:
        proj_idx = len(parts) - 1 - parts[::-1].index("projects")
        project_slug = parts[proj_idx + 1] if proj_idx + 1 < len(parts) else path.parent.name
        session_id = parts[proj_idx + 2].removesuffix(".jsonl") if proj_idx + 2 < len(parts) else path.stem
    except ValueError:
        project_slug = path.parent.name
        session_id = path.stem
    row = conn.execute("SELECT id, size, offset, scan_version FROM usage_files WHERE path = ?", (str(path),)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO usage_files (path, account_key, project_slug, session_id, size, mtime, offset) "
            "VALUES (?,?,?,?,0,0,0)",
            (str(path), account_key, project_slug, session_id),
        )
        file_id = cur.lastrowid
        offset = 0
    else:
        file_id = row["id"]
        offset = row["offset"]
        stale_version = (row["scan_version"] or 0) < SCAN_VERSION
        if stat.st_size == row["size"] and offset >= stat.st_size and not stale_version:
            return 0
        if stat.st_size < offset or stale_version:
            # Truncated, rewritten, or indexed by a scanner that read less out
            # of each line than this one does: start over for this file.
            conn.execute("DELETE FROM usage_turns WHERE file_id = ?", (file_id,))
            delete_file_rows(conn, file_id)
            offset = 0

    recorder = SkillRecorder(conn, file_id, account_key, subscription_id)
    acc = _TurnAccumulator()
    titles: dict[str, str] = {}
    first_prompt: tuple[str, str | None] | None = None
    have_prompt = bool(
        conn.execute(
            "SELECT 1 FROM usage_sessions WHERE session_id = ? AND first_prompt IS NOT NULL", (session_id,)
        ).fetchone()
    )

    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # Partial trailing line still being written; pick it up next pass.
                break
            offset += len(line)
            # Key order and spacing differ between writers, so sniff the whole
            # line (a C-speed substring search) and confirm the type after parsing.
            if b'"assistant"' in line:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if rec.get("type") == "assistant":
                    acc.add(rec, account_key, subscription_id)
                    ts = _normalize_ts(rec.get("timestamp"))
                    if ts:
                        recorder.add_assistant(rec, ts)
            elif b'"ai-title"' in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "ai-title" and rec.get("aiTitle"):
                    titles[rec.get("sessionId") or session_id] = str(rec["aiTitle"])[:200]
            elif b'"user"' in line:
                # User records answer skill launches and carry the instructions
                # a skill injected; the first human one titles the session.
                is_skill_record = b'"tool_result"' in line or b"Base directory for this skill" in line
                wants_prompt = not have_prompt and first_prompt is None and b"tool_result" not in line
                if not is_skill_record and not wants_prompt:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "user":
                    continue
                if is_skill_record:
                    recorder.add_user(rec)
                if wants_prompt and not rec.get("isMeta") and not rec.get("isSidechain"):
                    text = _first_user_text(rec.get("message") or {})
                    if text:
                        first_prompt = (text, _normalize_ts(rec.get("timestamp")))

    written = _upsert_turns(conn, file_id, acc.turns.values())
    recorder.flush()

    project_dir = next((t["project_dir"] for t in acc.turns.values() if t.get("project_dir")), None)
    if titles or first_prompt or written:
        conn.execute(
            """
            INSERT INTO usage_sessions (session_id, account_key, project_dir, title, first_prompt, first_prompt_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                project_dir = COALESCE(excluded.project_dir, usage_sessions.project_dir),
                title = COALESCE(excluded.title, usage_sessions.title),
                first_prompt = COALESCE(usage_sessions.first_prompt, excluded.first_prompt),
                first_prompt_at = COALESCE(usage_sessions.first_prompt_at, excluded.first_prompt_at)
            """,
            (
                session_id,
                account_key,
                project_dir,
                titles.get(session_id),
                first_prompt[0] if first_prompt else None,
                first_prompt[1] if first_prompt else None,
            ),
        )

    conn.execute(
        "UPDATE usage_files SET size = ?, mtime = ?, offset = ?, scan_version = ?, last_scanned_at = ? WHERE id = ?",
        (stat.st_size, stat.st_mtime, offset, SCAN_VERSION, datetime.now(UTC).isoformat(), file_id),
    )
    return written


def scan_transcripts(user_home: Path | None = None, db_path: Path | str | None = None) -> dict[str, Any]:
    """Incrementally ingest every transcript from every Claude home directory."""
    started = time.monotonic()
    homes = discover_claude_homes(user_home)
    summary: dict[str, Any] = {
        "homes": [h.name for h in homes],
        "files_scanned": 0,
        "turns_written": 0,
        "reindexed_files": 0,
        "scan_version": SCAN_VERSION,
    }

    with get_db(db_path) as conn:
        conn.executescript(USAGE_SCHEMA)
        _migrate_usage_schema(conn)
        account_map = resolve_account_subscriptions(conn, homes)
        # Subscriptions can appear after transcripts were first ingested.
        for account_key, sub_id in account_map.items():
            if sub_id is not None:
                conn.execute(
                    "UPDATE usage_turns SET subscription_id = ? WHERE account_key = ? AND subscription_id IS NULL",
                    (sub_id, account_key),
                )

        known = {
            r["path"]: (r["size"], r["mtime"], r["scan_version"] or 0)
            for r in conn.execute("SELECT path, size, mtime, scan_version FROM usage_files").fetchall()
        }
        for home in homes:
            for path in (home / "projects").rglob("*.jsonl"):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                prev = known.get(str(path))
                if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1e-6 and prev[2] >= SCAN_VERSION:
                    continue
                if prev and prev[2] < SCAN_VERSION:
                    summary["reindexed_files"] += 1
                summary["turns_written"] += _scan_file(conn, path, home.name, account_map.get(home.name))
                summary["files_scanned"] += 1
                conn.commit()

    summary["account_map"] = account_map
    summary["duration_s"] = round(time.monotonic() - started, 2)
    logger.info(
        "Transcript scan: %d files, %d turns in %.1fs",
        summary["files_scanned"],
        summary["turns_written"],
        summary["duration_s"],
    )
    return summary


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def _filters(
    subscription_ids: list[int] | None,
    date_str: str | None,
    start_hour: int | None,
    end_hour: int | None,
    alias: str = "t",
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if subscription_ids:
        placeholders = ",".join("?" for _ in subscription_ids)
        conditions.append(f"{alias}.subscription_id IN ({placeholders})")
        params.extend(subscription_ids)
    if date_str:
        conditions.append(f"strftime('%Y-%m-%d', datetime({alias}.timestamp, 'localtime')) = ?")
        params.append(date_str)
    if start_hour is not None:
        conditions.append(f"CAST(strftime('%H', datetime({alias}.timestamp, 'localtime')) AS INTEGER) >= ?")
        params.append(int(start_hour))
    if end_hour is not None:
        conditions.append(f"CAST(strftime('%H', datetime({alias}.timestamp, 'localtime')) AS INTEGER) <= ?")
        params.append(int(end_hour))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


TOKEN_SUMS = """
    COUNT(*) AS turns,
    COUNT(DISTINCT t.session_id) AS sessions,
    SUM(t.input_tokens) AS input_tokens,
    SUM(t.cache_creation_tokens) AS cache_creation_tokens,
    SUM(t.cache_read_tokens) AS cache_read_tokens,
    SUM(t.output_tokens) AS output_tokens,
    SUM(t.cache_1h_tokens) AS cache_1h_tokens,
    SUM(t.cache_5m_tokens) AS cache_5m_tokens,
    SUM(t.thinking_tokens) AS thinking_tokens,
    SUM(t.tool_calls) AS tool_calls,
    SUM(t.is_sidechain) AS sidechain_turns,
    SUM(t.est_cost_usd) AS est_cost_usd
"""


def _finish_row(d: dict[str, Any]) -> dict[str, Any]:
    for k, v in list(d.items()):
        if v is None and k not in ("model", "subscription_id", "account_key"):
            d[k] = 0
    total_in = d.get("input_tokens", 0) + d.get("cache_creation_tokens", 0) + d.get("cache_read_tokens", 0)
    d["total_input_tokens"] = total_in
    d["total_tokens"] = total_in + d.get("output_tokens", 0)
    d["cache_hit_ratio"] = (d.get("cache_read_tokens", 0) / total_in) if total_in else 0.0
    if "model" in d:
        d["model_label"] = pretty_model_name(d["model"])
    return d


def get_usage_summary(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, db_path=None
) -> dict[str, Any]:
    """Totals for the filtered window, overall and per account."""
    where, params = _filters(subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        total = dict(conn.execute(f"SELECT {TOKEN_SUMS} FROM usage_turns t {where}", params).fetchone())
        per_account = [
            _finish_row(dict(r))
            for r in conn.execute(
                f"SELECT t.subscription_id, t.account_key, {TOKEN_SUMS} FROM usage_turns t {where} "
                "GROUP BY t.subscription_id, t.account_key ORDER BY t.subscription_id",
                params,
            ).fetchall()
        ]
        # History span is deliberately unfiltered: it describes the index, not the view.
        span = conn.execute(
            "SELECT MIN(timestamp) AS first_turn, MAX(timestamp) AS last_turn FROM usage_turns"
        ).fetchone()
        scan = conn.execute(
            "SELECT COUNT(*) AS files, MAX(last_scanned_at) AS last_scanned_at FROM usage_files"
        ).fetchone()
    result = _finish_row(total)
    result["per_account"] = per_account
    result["first_turn"] = span["first_turn"] if span else None
    result["last_turn"] = span["last_turn"] if span else None
    result["files_indexed"] = scan["files"] if scan else 0
    result["last_scanned_at"] = scan["last_scanned_at"] if scan else None
    return result


def get_usage_by_model(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, db_path=None
) -> list[dict[str, Any]]:
    """Per-model totals with share of turns, tokens and estimated cost."""
    where, params = _filters(subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = [
            _finish_row(dict(r))
            for r in conn.execute(
                f"SELECT t.model, {TOKEN_SUMS} FROM usage_turns t {where} GROUP BY t.model ORDER BY est_cost_usd DESC",
                params,
            ).fetchall()
        ]
    total_turns = sum(r["turns"] for r in rows) or 1
    total_tokens = sum(r["total_tokens"] for r in rows) or 1
    total_cost = sum(r["est_cost_usd"] for r in rows) or 1
    for r in rows:
        r["share_turns"] = r["turns"] / total_turns
        r["share_tokens"] = r["total_tokens"] / total_tokens
        r["share_cost"] = r["est_cost_usd"] / total_cost
    return rows


def get_usage_sessions(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, limit: int = 60, db_path=None
) -> list[dict[str, Any]]:
    """Sessions touched inside the filtered window, newest first."""
    where, params = _filters(subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT t.session_id, t.subscription_id, t.account_key,
                   MIN(t.timestamp) AS started_at, MAX(t.timestamp) AS ended_at,
                   MAX(t.project_dir) AS project_dir, MAX(t.project) AS project, MAX(t.git_branch) AS git_branch,
                   {TOKEN_SUMS}
            FROM usage_turns t {where}
            GROUP BY t.session_id
            ORDER BY ended_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        sessions = [_finish_row(dict(r)) for r in rows]
        if not sessions:
            return []
        ids = [s["session_id"] for s in sessions]
        placeholders = ",".join("?" for _ in ids)
        meta = {
            r["session_id"]: dict(r)
            for r in conn.execute(
                f"SELECT session_id, title, first_prompt FROM usage_sessions WHERE session_id IN ({placeholders})", ids
            ).fetchall()
        }
        models = conn.execute(
            f"""
            SELECT t.session_id, t.model, COUNT(*) AS turns, SUM(t.est_cost_usd) AS est_cost_usd
            FROM usage_turns t {where} {"AND" if where else "WHERE"} t.session_id IN ({placeholders})
            GROUP BY t.session_id, t.model
            """,
            [*params, *ids],
        ).fetchall()
        session_skills = get_session_skills(ids, conn)
    by_session: dict[str, list[dict[str, Any]]] = {}
    for m in models:
        by_session.setdefault(m["session_id"], []).append(
            {
                "model": m["model"],
                "model_label": pretty_model_name(m["model"]),
                "turns": m["turns"],
                "est_cost_usd": m["est_cost_usd"],
            }
        )
    for s in sessions:
        m = meta.get(s["session_id"], {})
        s["skills"] = session_skills.get(s["session_id"], [])
        s["title"] = m.get("title")
        s["first_prompt"] = m.get("first_prompt")
        s["models"] = sorted(by_session.get(s["session_id"], []), key=lambda x: -x["est_cost_usd"])
        start = datetime.fromisoformat(s["started_at"])
        end = datetime.fromisoformat(s["ended_at"])
        s["duration_s"] = max(0.0, (end - start).total_seconds())
        s["project_name"] = s.get("project") or (Path(s["project_dir"]).name if s.get("project_dir") else None)
        s["worktree"] = (
            Path(s["project_dir"]).name
            if s.get("project_dir") and Path(s["project_dir"]).name != s["project_name"]
            else None
        )
    return sessions


def get_usage_projects(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, limit: int = 15, db_path=None
) -> list[dict[str, Any]]:
    where, params = _filters(subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT t.project, COUNT(DISTINCT t.project_dir) AS directories, {TOKEN_SUMS}
            FROM usage_turns t {where}
            GROUP BY t.project
            ORDER BY est_cost_usd DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    out = []
    for r in rows:
        d = _finish_row(dict(r))
        d["project_name"] = d.get("project") or "(unknown)"
        out.append(d)
    return out


def get_usage_heatmap(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, db_path=None
) -> list[dict[str, Any]]:
    """Weekday x hour matrix (local time) of turns, tokens and cost."""
    where, params = _filters(subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT CAST(strftime('%w', datetime(t.timestamp, 'localtime')) AS INTEGER) AS weekday,
                   CAST(strftime('%H', datetime(t.timestamp, 'localtime')) AS INTEGER) AS hour,
                   COUNT(*) AS turns,
                   SUM(t.input_tokens + t.cache_creation_tokens + t.cache_read_tokens + t.output_tokens)
                       AS total_tokens,
                   SUM(t.output_tokens) AS output_tokens,
                   SUM(t.est_cost_usd) AS est_cost_usd
            FROM usage_turns t {where}
            GROUP BY weekday, hour
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_usage_timeline(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, bucket: str = "hour", db_path=None
) -> list[dict[str, Any]]:
    """Token/cost totals per model per time bucket (local time)."""
    fmt = "%Y-%m-%dT%H:00:00" if bucket == "hour" else "%Y-%m-%d"
    where, params = _filters(subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT strftime('{fmt}', datetime(t.timestamp, 'localtime')) AS bucket,
                   t.model,
                   COUNT(*) AS turns,
                   SUM(t.input_tokens + t.cache_creation_tokens + t.cache_read_tokens + t.output_tokens)
                       AS total_tokens,
                   SUM(t.output_tokens) AS output_tokens,
                   SUM(t.est_cost_usd) AS est_cost_usd
            FROM usage_turns t {where}
            GROUP BY bucket, t.model
            ORDER BY bucket ASC
            """,
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["model_label"] = pretty_model_name(d["model"])
        out.append(d)
    return out


def get_five_hour_windows(subscription_ids=None, date_str=None, limit: int = 12, db_path=None) -> list[dict[str, Any]]:
    """Attribute each observed 5-hour quota window to the models used inside it.

    A window is identified by the ``five_hour_resets_at`` deadline reported by
    the usage API; it spans the five hours before that deadline. The window's
    utilisation is the highest ``five_hour_pct`` reading observed for that
    deadline. Each model's slice of the window is its share of the estimated
    cost (and, separately, of raw tokens) of the turns inside the window,
    multiplied by the window's utilisation.
    """
    conditions = ["s.five_hour_resets_at IS NOT NULL"]
    params: list[Any] = []
    if subscription_ids:
        placeholders = ",".join("?" for _ in subscription_ids)
        conditions.append(f"s.subscription_id IN ({placeholders})")
        params.extend(subscription_ids)
    if date_str:
        # Windows that overlap the selected local day.
        conditions.append(
            "strftime('%Y-%m-%d', datetime(s.five_hour_resets_at, 'localtime')) >= ? AND "
            "strftime('%Y-%m-%d', datetime(s.five_hour_resets_at, '-5 hours', 'localtime')) <= ?"
        )
        params.extend([date_str, date_str])
    where = " AND ".join(conditions)

    with get_db(db_path) as conn:
        readings = conn.execute(
            f"""
            SELECT s.subscription_id, s.five_hour_resets_at AS resets_at, s.five_hour_pct AS pct, s.timestamp
            FROM quota_snapshots s
            WHERE {where}
            ORDER BY s.subscription_id, s.five_hour_resets_at ASC
            """,
            params,
        ).fetchall()

        # The API reports the deadline with sub-second jitter between polls, so
        # readings whose deadlines fall within the tolerance are one window.
        windows: list[dict[str, Any]] = []
        for r in readings:
            deadline = datetime.fromisoformat(r["resets_at"].replace("Z", "+00:00"))
            pct = float(r["pct"] or 0.0)
            current = windows[-1] if windows else None
            if (
                current is not None
                and current["subscription_id"] == r["subscription_id"]
                and abs((deadline - current["deadline"]).total_seconds()) <= RESET_WINDOW_TOLERANCE_S
            ):
                current["deadline"] = max(current["deadline"], deadline)
                current["peak_pct"] = max(current["peak_pct"], pct)
                current["first_pct"] = min(current["first_pct"], pct)
                current["first_seen"] = min(current["first_seen"], r["timestamp"])
                current["last_seen"] = max(current["last_seen"], r["timestamp"])
                current["readings"] += 1
            else:
                windows.append(
                    {
                        "subscription_id": r["subscription_id"],
                        "deadline": deadline,
                        "peak_pct": pct,
                        "first_pct": pct,
                        "first_seen": r["timestamp"],
                        "last_seen": r["timestamp"],
                        "readings": 1,
                    }
                )
        windows.sort(key=lambda w: w["deadline"], reverse=True)
        windows = windows[:limit]

        now = datetime.now(UTC)
        out = []
        for w in windows:
            deadline = w["deadline"]
            start = deadline.timestamp() - 5 * 3600
            start_iso = datetime.fromtimestamp(start, tz=UTC).isoformat()
            end_iso = deadline.astimezone(UTC).isoformat()
            turns = conn.execute(
                f"""
                SELECT t.model, {TOKEN_SUMS}
                FROM usage_turns t
                WHERE t.subscription_id = ? AND t.timestamp >= ? AND t.timestamp < ?
                GROUP BY t.model
                ORDER BY est_cost_usd DESC
                """,
                (w["subscription_id"], start_iso, end_iso),
            ).fetchall()
            models = [_finish_row(dict(r)) for r in turns]
            total_cost = sum(m["est_cost_usd"] for m in models)
            total_tokens = sum(m["total_tokens"] for m in models)
            peak = float(w["peak_pct"] or 0.0)
            for m in models:
                m["share_cost"] = (m["est_cost_usd"] / total_cost) if total_cost else 0.0
                m["share_tokens"] = (m["total_tokens"] / total_tokens) if total_tokens else 0.0
                m["window_pct_by_cost"] = m["share_cost"] * peak
                m["window_pct_by_tokens"] = m["share_tokens"] * peak
            out.append(
                {
                    "subscription_id": w["subscription_id"],
                    "starts_at": start_iso,
                    "resets_at": end_iso,
                    "is_active": deadline > now,
                    "peak_pct": peak,
                    "readings": w["readings"],
                    "first_seen": w["first_seen"],
                    "last_seen": w["last_seen"],
                    "turns": sum(m["turns"] for m in models),
                    "sessions": conn.execute(
                        "SELECT COUNT(DISTINCT session_id) FROM usage_turns "
                        "WHERE subscription_id = ? AND timestamp >= ? AND timestamp < ?",
                        (w["subscription_id"], start_iso, end_iso),
                    ).fetchone()[0],
                    "total_tokens": total_tokens,
                    "est_cost_usd": total_cost,
                    "models": models,
                }
            )
    return out
