"""Skill, plugin and tool analytics from local Claude Code transcripts.

Three things are recorded while ``backend.usage`` walks a transcript:

* **Skill invocations** — every ``Skill`` tool call, with the plugin it came
  from, the version of that plugin, whether it actually launched, and how many
  characters of instructions it injected into the conversation.
* **Tool calls** — one row per (assistant turn, tool name), which is what makes
  plugin-provided MCP servers countable alongside the skills they ship with.
* **Attribution** — the turns a skill is responsible for. A skill has no token
  usage of its own; what it costs is the work it then drives. Every turn is
  attributed to the skill most recently loaded in its session, so the
  attribution partitions the window's tokens rather than double counting the
  sessions that load several skills.

The transcript gives us three separate records per invocation, tied together
by uuid rather than by position because a live session can be indexed
mid-conversation:

1. the assistant ``tool_use`` block (skill name and args),
2. the ``tool_result`` confirming the launch or reporting an unknown skill,
3. a user text block starting ``Base directory for this skill: <path>`` that
   carries the instructions themselves, whose ``parentUuid`` is (2).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from backend.database import get_db

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SKILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_skill_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES usage_files(id) ON DELETE CASCADE,
    account_key TEXT NOT NULL,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    session_id TEXT NOT NULL,
    project TEXT,
    project_dir TEXT,
    timestamp TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    result_uuid TEXT,
    skill TEXT NOT NULL,
    plugin TEXT,
    skill_name TEXT NOT NULL,
    args TEXT,
    model TEXT,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    source TEXT,
    marketplace TEXT,
    version TEXT,
    base_dir TEXT,
    payload_chars INTEGER NOT NULL DEFAULT 0,
    payload_tokens INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_key, tool_use_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_inv_session ON usage_skill_invocations(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_skill_inv_skill ON usage_skill_invocations(skill);
CREATE INDEX IF NOT EXISTS idx_skill_inv_time ON usage_skill_invocations(subscription_id, timestamp);

CREATE TABLE IF NOT EXISTS usage_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES usage_files(id) ON DELETE CASCADE,
    account_key TEXT NOT NULL,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    session_id TEXT NOT NULL,
    project TEXT,
    timestamp TEXT NOT NULL,
    message_id TEXT,
    request_id TEXT,
    model TEXT,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    tool_name TEXT NOT NULL,
    tool_kind TEXT NOT NULL,
    server TEXT,
    plugin TEXT,
    target TEXT,
    calls INTEGER NOT NULL DEFAULT 1,
    UNIQUE(account_key, message_id, request_id, tool_name)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON usage_tool_calls(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON usage_tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_time ON usage_tool_calls(subscription_id, timestamp);
"""


def init_skills_schema(conn: sqlite3.Connection) -> None:
    """Create the skill/tool tables on an open connection."""
    conn.executescript(SKILLS_SCHEMA)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

BASE_DIR_RE = re.compile(r"^Base directory for this skill:\s*(.+?)\s*$", re.MULTILINE)

# ``mcp__<server>__<tool>``; a plugin-provided server is ``plugin_<plugin>_<server>``.
MCP_PREFIX = "mcp__"
PLUGIN_SERVER_PREFIX = "plugin_"

# Roughly four characters per token for English prose. The transcript never
# records a token count for injected instructions, so this is the only handle
# we have on how much context a skill costs to load.
CHARS_PER_TOKEN = 4


def split_skill_id(skill: str) -> tuple[str | None, str]:
    """Split ``git:pr`` into its plugin and skill name. Unscoped skills have no plugin."""
    if ":" in skill:
        plugin, _, name = skill.partition(":")
        return plugin or None, name
    return None, skill


def parse_skill_base_dir(base_dir: str) -> dict[str, str | None]:
    """Read provenance out of the directory a skill was loaded from.

    A marketplace plugin unpacks to
    ``.../plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>``, which
    is the only place the marketplace and the installed version appear at all.
    The other layouts carry less: skills bundled with the CLI are staged under
    ``bundled-skills/<cli-version>/<hash>/<name>``, a plugin loaded by the
    desktop app sits under ``plugin_<id>/skills/<name>``, and a personal skill
    is just ``~/.claude/skills/<name>``.
    """
    parts = Path(base_dir).parts
    info: dict[str, str | None] = {"source": "unknown", "marketplace": None, "version": None, "plugin": None}
    if "plugins" in parts:
        i = parts.index("plugins")
        if i + 5 < len(parts) and parts[i + 1] == "cache":
            info.update(source="plugin", marketplace=parts[i + 2], plugin=parts[i + 3], version=parts[i + 4])
            return info
    if "bundled-skills" in parts:
        i = parts.index("bundled-skills")
        info.update(source="bundled", version=parts[i + 1] if i + 1 < len(parts) else None)
        return info
    if "skills" in parts:
        i = parts.index("skills")
        if i >= 1 and parts[i - 1].startswith("plugin_"):
            info["source"] = "plugin"
            return info
        if i >= 1 and parts[i - 1].startswith(".claude"):
            info["source"] = "personal"
            return info
        info["source"] = "project"
        return info
    return info


def version_key(version: str | None) -> tuple[int, ...]:
    """Sort key for a dotted version, so 0.14.0 ranks above 0.7.0."""
    if not version:
        return ()
    return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))


def classify_tool(name: str, known_plugins: Iterable[str] = ()) -> dict[str, str | None]:
    """Bucket a tool name into builtin / mcp / skill / agent and name its owner.

    Plugin MCP servers are registered as ``mcp__plugin_<plugin>_<server>__<tool>``.
    Both halves may contain underscores, so the plugin is resolved against the
    plugins we have already seen ship a skill, longest match first, and only
    falls back to splitting at the last underscore.
    """
    if name == "Skill":
        return {"tool_kind": "skill", "server": None, "plugin": None}
    if name == "Agent":
        return {"tool_kind": "agent", "server": None, "plugin": None}
    if not name.startswith(MCP_PREFIX):
        return {"tool_kind": "builtin", "server": None, "plugin": None}

    rest = name[len(MCP_PREFIX) :]
    server, _, _tool = rest.partition("__")
    if not server.startswith(PLUGIN_SERVER_PREFIX):
        return {"tool_kind": "mcp", "server": server, "plugin": None}

    tail = server[len(PLUGIN_SERVER_PREFIX) :]
    for plugin in sorted(known_plugins, key=len, reverse=True):
        if tail == plugin:
            return {"tool_kind": "mcp", "server": plugin, "plugin": plugin}
        if tail.startswith(f"{plugin}_"):
            return {"tool_kind": "mcp", "server": tail[len(plugin) + 1 :], "plugin": plugin}
    plugin, _, srv = tail.rpartition("_")
    return {"tool_kind": "mcp", "server": srv or tail, "plugin": plugin or None}


def known_plugin_names(conn: sqlite3.Connection) -> set[str]:
    """Plugins observed shipping a skill, used to split plugin MCP server names."""
    rows = conn.execute("SELECT DISTINCT plugin FROM usage_skill_invocations WHERE plugin IS NOT NULL")
    return {r[0] for r in rows}


class SkillRecorder:
    """Collects skill invocations and tool calls while one transcript is read.

    The three records that make up an invocation can land in different scan
    passes of a live session, so resolution goes through the database by
    ``tool_use_id`` and by the tool result's uuid rather than by adjacency.
    """

    def __init__(self, conn: sqlite3.Connection, file_id: int, account_key: str, subscription_id: int | None):
        self.conn = conn
        self.file_id = file_id
        self.account_key = account_key
        self.subscription_id = subscription_id
        self.known_plugins = known_plugin_names(conn)
        self.invocations = 0
        self.tool_rows: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
        self.seen_blocks: set[str] = set()

    # -- assistant records -------------------------------------------------- #

    def add_assistant(self, record: dict[str, Any], timestamp: str) -> None:
        """Record every tool call in one assistant record, skills included."""
        message = record.get("message") or {}
        model = message.get("model")
        session_id = record.get("sessionId") or ""
        if not session_id:
            return
        from backend.usage import canonical_project  # circular at import time only

        project = canonical_project(record.get("cwd"))
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            block_id = block.get("id")
            if not name or (block_id and block_id in self.seen_blocks):
                continue
            if block_id:
                self.seen_blocks.add(block_id)
            target = None
            if name == "Skill":
                target = str((block.get("input") or {}).get("skill") or "") or None
                if target:
                    self._add_invocation(record, block, target, timestamp, project, model)
            self._add_tool_call(record, name, target, timestamp, project, model, session_id, message)

    def _add_tool_call(self, record, name, target, timestamp, project, model, session_id, message) -> None:
        key = (message.get("id"), record.get("requestId"), name)
        existing = self.tool_rows.get(key)
        if existing:
            existing["calls"] += 1
            return
        self.tool_rows[key] = {
            "file_id": self.file_id,
            "account_key": self.account_key,
            "subscription_id": self.subscription_id,
            "session_id": session_id,
            "project": project,
            "timestamp": timestamp,
            "message_id": message.get("id"),
            "request_id": record.get("requestId"),
            "model": model,
            "is_sidechain": 1 if record.get("isSidechain") else 0,
            "tool_name": name,
            "target": target,
            "calls": 1,
            **classify_tool(name, self.known_plugins),
        }

    def _add_invocation(self, record, block, skill, timestamp, project, model) -> None:
        tool_use_id = block.get("id")
        if not tool_use_id:
            return
        plugin, skill_name = split_skill_id(skill)
        if plugin:
            self.known_plugins.add(plugin)
        args = (block.get("input") or {}).get("args")
        self.conn.execute(
            """
            INSERT INTO usage_skill_invocations (
                file_id, account_key, subscription_id, session_id, project, project_dir,
                timestamp, tool_use_id, skill, plugin, skill_name, args, model, is_sidechain, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
            ON CONFLICT(account_key, tool_use_id) DO NOTHING
            """,
            (
                self.file_id,
                self.account_key,
                self.subscription_id,
                record.get("sessionId") or "",
                project,
                record.get("cwd"),
                timestamp,
                tool_use_id,
                skill,
                plugin,
                skill_name,
                str(args)[:200] if args else None,
                model,
                1 if record.get("isSidechain") else 0,
            ),
        )
        self.invocations += 1

    # -- user records ------------------------------------------------------- #

    def add_user(self, record: dict[str, Any]) -> None:
        """Resolve a launch result or capture the instructions a skill injected."""
        message = record.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                self._resolve_result(record, block)
            elif block.get("type") == "text":
                self._capture_payload(record, block)

    def _resolve_result(self, record: dict[str, Any], block: dict[str, Any]) -> None:
        tool_use_id = block.get("tool_use_id")
        if not tool_use_id:
            return
        row = self.conn.execute(
            "SELECT id, status FROM usage_skill_invocations WHERE account_key = ? AND tool_use_id = ?",
            (self.account_key, tool_use_id),
        ).fetchone()
        if row is None:
            return
        if block.get("is_error"):
            content = block.get("content")
            error = content if isinstance(content, str) else str(content)
            self.conn.execute(
                "UPDATE usage_skill_invocations SET status = 'error', error = ? WHERE id = ?",
                (error[:200], row["id"]),
            )
            return
        self.conn.execute(
            "UPDATE usage_skill_invocations SET status = 'launched', result_uuid = ? WHERE id = ?",
            (record.get("uuid"), row["id"]),
        )

    def _capture_payload(self, record: dict[str, Any], block: dict[str, Any]) -> None:
        text = block.get("text")
        if not isinstance(text, str) or not text.startswith("Base directory for this skill:"):
            return
        parent = record.get("parentUuid")
        if not parent:
            return
        row = self.conn.execute(
            "SELECT id, plugin FROM usage_skill_invocations WHERE account_key = ? AND result_uuid = ?",
            (self.account_key, parent),
        ).fetchone()
        if row is None:
            return
        match = BASE_DIR_RE.search(text)
        base_dir = match.group(1) if match else None
        info = (
            parse_skill_base_dir(base_dir)
            if base_dir
            else {"source": "unknown", "marketplace": None, "version": None, "plugin": None}
        )
        self.conn.execute(
            """
            UPDATE usage_skill_invocations
            SET base_dir = ?, source = ?, marketplace = ?, version = ?,
                plugin = COALESCE(plugin, ?), payload_chars = ?, payload_tokens = ?
            WHERE id = ?
            """,
            (
                base_dir,
                info["source"],
                info["marketplace"],
                info["version"],
                info["plugin"],
                len(text),
                round(len(text) / CHARS_PER_TOKEN),
                row["id"],
            ),
        )

    # -- flush -------------------------------------------------------------- #

    def flush(self) -> int:
        """Write the buffered tool calls. Returns how many rows were touched."""
        for row in self.tool_rows.values():
            self.conn.execute(
                """
                INSERT INTO usage_tool_calls (
                    file_id, account_key, subscription_id, session_id, project, timestamp,
                    message_id, request_id, model, is_sidechain, tool_name, tool_kind, server, plugin, target, calls
                ) VALUES (
                    :file_id, :account_key, :subscription_id, :session_id, :project, :timestamp,
                    :message_id, :request_id, :model, :is_sidechain, :tool_name, :tool_kind,
                    :server, :plugin, :target, :calls
                )
                ON CONFLICT(account_key, message_id, request_id, tool_name) DO UPDATE SET
                    calls = usage_tool_calls.calls + excluded.calls
                """,
                row,
            )
        written = len(self.tool_rows)
        self.tool_rows.clear()
        return written


def delete_file_rows(conn: sqlite3.Connection, file_id: int) -> None:
    """Drop the skill/tool rows of one transcript before it is re-read."""
    conn.execute("DELETE FROM usage_skill_invocations WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM usage_tool_calls WHERE file_id = ?", (file_id,))


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def _where(alias: str, subscription_ids, date_str, start_hour, end_hour) -> tuple[str, list[Any]]:
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
    return (f"WHERE {' AND '.join(conditions)}" if conditions else ""), params


def _blank_totals() -> dict[str, Any]:
    return {"turns": 0, "total_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0, "tool_calls": 0, "sessions": set()}


def _attribute_turns(conn, subscription_ids, date_str, start_hour, end_hour) -> dict[str, Any]:
    """Split the window's turns between the skills that were loaded when they ran.

    A turn belongs to the most recently launched skill in its own transcript,
    so the parts sum back to the window total instead of counting a turn once
    per skill its session happens to have loaded. The walk follows the
    transcript rather than the session because a subagent writes its own file
    under the parent's session id: a skill a subagent loads governs that
    subagent's turns, not whatever the main thread is doing in parallel. Turns
    before the first skill of a transcript are unattributed.
    """
    where, params = _where("t", subscription_ids, date_str, start_hour, end_hour)
    turns = conn.execute(
        f"""
        SELECT t.file_id, t.session_id, t.timestamp, t.tool_calls,
               t.input_tokens + t.cache_creation_tokens + t.cache_read_tokens + t.output_tokens AS total_tokens,
               t.output_tokens, t.est_cost_usd
        FROM usage_turns t {where}
        ORDER BY t.file_id, t.timestamp
        """,
        params,
    ).fetchall()
    if not turns:
        return {"per_skill": {}, "unattributed": _blank_totals(), "attributed": _blank_totals()}

    # Invocations are looked up over the whole life of each transcript, not
    # just the filtered window: a skill loaded this morning still governs the
    # turns an "afternoon only" filter shows.
    file_ids = sorted({r["file_id"] for r in turns})
    invocations: dict[int, list[tuple[str, str]]] = {}
    for start in range(0, len(file_ids), 400):
        chunk = file_ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"""
            SELECT file_id, timestamp, skill FROM usage_skill_invocations
            WHERE file_id IN ({placeholders}) AND status != 'error'
            ORDER BY file_id, timestamp
            """,
            chunk,
        ):
            invocations.setdefault(r["file_id"], []).append((r["timestamp"], r["skill"]))

    per_skill: dict[str, dict[str, Any]] = {}
    unattributed = _blank_totals()
    attributed = _blank_totals()
    current_file = None
    pending: list[tuple[str, str]] = []
    active: str | None = None

    for r in turns:
        if r["file_id"] != current_file:
            current_file = r["file_id"]
            pending = list(invocations.get(current_file, []))
            active = None
        while pending and pending[0][0] <= r["timestamp"]:
            active = pending.pop(0)[1]
        bucket = unattributed if active is None else per_skill.setdefault(active, _blank_totals())
        for target in (bucket,) if active is None else (bucket, attributed):
            target["turns"] += 1
            target["total_tokens"] += r["total_tokens"] or 0
            target["output_tokens"] += r["output_tokens"] or 0
            target["est_cost_usd"] += r["est_cost_usd"] or 0.0
            target["tool_calls"] += r["tool_calls"] or 0
            target["sessions"].add(r["session_id"])

    return {"per_skill": per_skill, "unattributed": unattributed, "attributed": attributed}


def _finalize(totals: dict[str, Any]) -> dict[str, Any]:
    out = dict(totals)
    out["sessions"] = len(totals["sessions"])
    return out


def get_skill_usage(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, db_path=None
) -> dict[str, Any]:
    """Per-skill and per-plugin metrics for the filtered window."""
    where, params = _where("i", subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT i.skill, i.plugin, i.skill_name,
                   COUNT(*) AS invocations,
                   SUM(CASE WHEN i.status = 'error' THEN 1 ELSE 0 END) AS errors,
                   COUNT(DISTINCT i.session_id) AS sessions,
                   COUNT(DISTINCT i.project) AS projects,
                   SUM(i.payload_tokens) AS payload_tokens,
                   MAX(i.payload_tokens) AS payload_tokens_each,
                   MIN(i.timestamp) AS first_used,
                   MAX(i.timestamp) AS last_used,
                   MAX(i.source) AS source,
                   MAX(i.marketplace) AS marketplace,
                   COUNT(DISTINCT i.version) AS version_count
            FROM usage_skill_invocations i {where}
            GROUP BY i.skill
            ORDER BY invocations DESC
            """,
            params,
        ).fetchall()
        version_rows = conn.execute(
            f"SELECT i.skill, i.version FROM usage_skill_invocations i {where} "
            f"{'AND' if where else 'WHERE'} i.version IS NOT NULL GROUP BY i.skill, i.version",
            params,
        ).fetchall()
        latest_versions: dict[str, str] = {}
        for r in version_rows:
            best = latest_versions.get(r["skill"])
            if best is None or version_key(r["version"]) > version_key(best):
                latest_versions[r["skill"]] = r["version"]
        attribution = _attribute_turns(conn, subscription_ids, date_str, start_hour, end_hour)

        tool_where, tool_params = _where("c", subscription_ids, date_str, start_hour, end_hour)
        plugin_tools = conn.execute(
            f"""
            SELECT c.plugin, c.server, SUM(c.calls) AS calls, COUNT(DISTINCT c.tool_name) AS tools
            FROM usage_tool_calls c {tool_where} {"AND" if tool_where else "WHERE"} c.plugin IS NOT NULL
            GROUP BY c.plugin, c.server
            """,
            tool_params,
        ).fetchall()

        turn_where, turn_params = _where("t", subscription_ids, date_str, start_hour, end_hour)
        sessions_total = conn.execute(
            f"SELECT COUNT(DISTINCT t.session_id) FROM usage_turns t {turn_where}", turn_params
        ).fetchone()[0]

    per_skill = attribution["per_skill"]
    skills: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["latest_version"] = latest_versions.get(d["skill"])
        attr = _finalize(per_skill.get(d["skill"], _blank_totals()))
        d["attributed_turns"] = attr["turns"]
        d["attributed_tokens"] = attr["total_tokens"]
        d["attributed_output_tokens"] = attr["output_tokens"]
        d["attributed_cost_usd"] = attr["est_cost_usd"]
        d["attributed_tool_calls"] = attr["tool_calls"]
        d["tokens_per_invocation"] = (attr["total_tokens"] / d["invocations"]) if d["invocations"] else 0
        d["cost_per_invocation"] = (attr["est_cost_usd"] / d["invocations"]) if d["invocations"] else 0.0
        d["payload_tokens"] = d["payload_tokens"] or 0
        d["payload_tokens_each"] = d["payload_tokens_each"] or 0
        d["ok"] = d["invocations"] - (d["errors"] or 0)
        skills.append(d)

    plugins: dict[str, dict[str, Any]] = {}
    for s in skills:
        key = s["plugin"] or "(unscoped)"
        p = plugins.setdefault(
            key,
            {
                "plugin": key,
                "is_plugin": bool(s["plugin"]),
                "source": s["source"],
                "marketplace": s["marketplace"],
                "latest_version": s["latest_version"],
                "version_count": 0,
                "skills": [],
                "invocations": 0,
                "errors": 0,
                "payload_tokens": 0,
                "attributed_turns": 0,
                "attributed_tokens": 0,
                "attributed_cost_usd": 0.0,
                "mcp_calls": 0,
                "mcp_servers": [],
                "sessions": set(),
            },
        )
        p["skills"].append({"skill": s["skill"], "skill_name": s["skill_name"], "invocations": s["invocations"]})
        p["invocations"] += s["invocations"]
        p["errors"] += s["errors"] or 0
        p["payload_tokens"] += s["payload_tokens"]
        p["attributed_turns"] += s["attributed_turns"]
        p["attributed_tokens"] += s["attributed_tokens"]
        p["attributed_cost_usd"] += s["attributed_cost_usd"]
        p["version_count"] = max(p["version_count"], s["version_count"] or 0)
        if version_key(s["latest_version"]) > version_key(p["latest_version"]):
            p["latest_version"] = s["latest_version"]

    # A plugin that only ships an MCP server still deserves a row.
    for r in plugin_tools:
        p = plugins.setdefault(
            r["plugin"],
            {
                "plugin": r["plugin"],
                "is_plugin": True,
                "source": "plugin",
                "marketplace": None,
                "latest_version": None,
                "version_count": 0,
                "skills": [],
                "invocations": 0,
                "errors": 0,
                "payload_tokens": 0,
                "attributed_turns": 0,
                "attributed_tokens": 0,
                "attributed_cost_usd": 0.0,
                "mcp_calls": 0,
                "mcp_servers": [],
                "sessions": set(),
            },
        )
        p["mcp_calls"] += r["calls"]
        p["mcp_servers"].append({"server": r["server"], "calls": r["calls"], "tools": r["tools"]})

    plugin_list = []
    for p in plugins.values():
        p.pop("sessions", None)
        p["skills"].sort(key=lambda s: -s["invocations"])
        p["mcp_servers"].sort(key=lambda s: -s["calls"])
        p["skill_count"] = len(p["skills"])
        plugin_list.append(p)
    plugin_list.sort(key=lambda p: (-p["attributed_cost_usd"], -p["invocations"], -p["mcp_calls"]))

    attributed = _finalize(attribution["attributed"])
    unattributed = _finalize(attribution["unattributed"])
    return {
        "skills": skills,
        "plugins": plugin_list,
        "totals": {
            "invocations": sum(s["invocations"] for s in skills),
            "errors": sum(s["errors"] or 0 for s in skills),
            "distinct_skills": len(skills),
            "distinct_plugins": sum(1 for p in plugin_list if p["is_plugin"]),
            "payload_tokens": sum(s["payload_tokens"] for s in skills),
            "sessions_with_skills": attributed["sessions"],
            "sessions_total": sessions_total or 0,
            "attributed": attributed,
            "unattributed": unattributed,
        },
    }


def get_tool_usage(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, limit: int = 20, db_path=None
) -> dict[str, Any]:
    """Tool-call leaderboard for the window, plus totals per tool kind and MCP server."""
    where, params = _where("c", subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        tools = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT c.tool_name, c.tool_kind, c.server, c.plugin,
                       SUM(c.calls) AS calls, COUNT(DISTINCT c.session_id) AS sessions
                FROM usage_tool_calls c {where}
                GROUP BY c.tool_name ORDER BY calls DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        ]
        kinds = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT c.tool_kind, SUM(c.calls) AS calls, COUNT(DISTINCT c.tool_name) AS tools
                FROM usage_tool_calls c {where} GROUP BY c.tool_kind ORDER BY calls DESC
                """,
                params,
            ).fetchall()
        ]
        servers = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT c.server, c.plugin, SUM(c.calls) AS calls, COUNT(DISTINCT c.tool_name) AS tools,
                       COUNT(DISTINCT c.session_id) AS sessions
                FROM usage_tool_calls c {where} {"AND" if where else "WHERE"} c.tool_kind = 'mcp'
                GROUP BY c.server ORDER BY calls DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        ]
        total = conn.execute(f"SELECT SUM(c.calls) FROM usage_tool_calls c {where}", params).fetchone()[0] or 0
    return {"tools": tools, "kinds": kinds, "servers": servers, "total_calls": total}


def get_session_skills(session_ids: list[str], conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Skills invoked in each of the given sessions, most-invoked first."""
    if not session_ids:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(session_ids), 400):
        chunk = session_ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(
            f"""
            SELECT session_id, skill, plugin, skill_name, COUNT(*) AS invocations,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                   SUM(payload_tokens) AS payload_tokens
            FROM usage_skill_invocations
            WHERE session_id IN ({placeholders})
            GROUP BY session_id, skill
            ORDER BY invocations DESC
            """,
            chunk,
        ):
            out.setdefault(r["session_id"], []).append(dict(r))
    return out


def get_skill_timeline(
    subscription_ids=None, date_str=None, start_hour=None, end_hour=None, bucket: str = "day", db_path=None
) -> list[dict[str, Any]]:
    """Invocations per skill per time bucket (local time)."""
    fmt = "%Y-%m-%dT%H:00:00" if bucket == "hour" else "%Y-%m-%d"
    where, params = _where("i", subscription_ids, date_str, start_hour, end_hour)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT strftime('{fmt}', datetime(i.timestamp, 'localtime')) AS bucket,
                   i.skill, i.plugin, COUNT(*) AS invocations
            FROM usage_skill_invocations i {where}
            GROUP BY bucket, i.skill ORDER BY bucket ASC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]
