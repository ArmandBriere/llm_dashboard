"""SQLite database management for LLM Quota Tracker.

Handles table schema initialization, subscription upserts, periodic quota snapshot logging,
smart reset event detection, and filtered time-series queries.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import db_path

DB_PATH = db_path()

# A provider that cannot resolve an account's profile falls back to a
# placeholder identity on the @claude.ai domain. Real subscriptions are always
# on a customer domain, so that address marks a keychain entry we failed to
# identify — a dead entry, or a poll that ran while the machine was offline.
PLACEHOLDER_EMAIL_DOMAIN = "@claude.ai"


def is_placeholder_email(email: str | None) -> bool:
    """True for the stand-in address a provider uses when identity is unknown."""
    return bool(email) and email.lower().endswith(PLACEHOLDER_EMAIL_DOMAIN)


_initialized = False


@contextmanager
def get_db(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite database connection."""
    global _initialized
    target_path = Path(db_path) if db_path else DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _initialized and db_path is None:
        _initialized = True
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL DEFAULT 'claude_code',
                account_uuid TEXT UNIQUE,
                email TEXT NOT NULL,
                organization_name TEXT,
                organization_uuid TEXT,
                keychain_service TEXT,
                is_active INTEGER DEFAULT 0,
                last_seen_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS quota_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                timestamp TEXT DEFAULT (datetime('now')),
                five_hour_pct REAL,
                five_hour_resets_at TEXT,
                seven_day_pct REAL,
                seven_day_resets_at TEXT,
                scoped_model TEXT,
                scoped_pct REAL,
                scoped_resets_at TEXT,
                spend_used REAL,
                spend_limit REAL,
                spend_currency TEXT,
                is_stale INTEGER DEFAULT 0,
                raw_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_sub_time
            ON quota_snapshots(subscription_id, timestamp);
            CREATE TABLE IF NOT EXISTS quota_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                timestamp TEXT DEFAULT (datetime('now')),
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                metadata_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_sub_time
            ON quota_events(subscription_id, timestamp);
            """
        )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    """Initialize database tables and indexes."""
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL DEFAULT 'claude_code',
                account_uuid TEXT UNIQUE,
                email TEXT NOT NULL,
                organization_name TEXT,
                organization_uuid TEXT,
                keychain_service TEXT,
                is_active INTEGER DEFAULT 0,
                last_seen_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS quota_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                timestamp TEXT DEFAULT (datetime('now')),
                five_hour_pct REAL,
                five_hour_resets_at TEXT,
                seven_day_pct REAL,
                seven_day_resets_at TEXT,
                scoped_model TEXT,
                scoped_pct REAL,
                scoped_resets_at TEXT,
                spend_used REAL,
                spend_limit REAL,
                spend_currency TEXT,
                is_stale INTEGER DEFAULT 0,
                raw_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_sub_time
            ON quota_snapshots(subscription_id, timestamp);

            CREATE TABLE IF NOT EXISTS quota_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                timestamp TEXT DEFAULT (datetime('now')),
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_sub_time
            ON quota_events(subscription_id, timestamp);
            """
        )
        _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(quota_snapshots)")}
    if "is_stale" not in existing:
        conn.execute("ALTER TABLE quota_snapshots ADD COLUMN is_stale INTEGER DEFAULT 0")
    if "scoped_resets_at" not in existing:
        conn.execute("ALTER TABLE quota_snapshots ADD COLUMN scoped_resets_at TEXT")


def upsert_subscription(
    email: str,
    account_uuid: str | None = None,
    organization_name: str | None = None,
    organization_uuid: str | None = None,
    keychain_service: str | None = None,
    is_active: bool = False,
    provider: str = "claude_code",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Insert or update a subscription record."""
    with get_db(db_path) as conn:
        now_str = datetime.now(UTC).isoformat()
        # Find existing by account_uuid or email
        cur = conn.execute(
            "SELECT * FROM subscriptions WHERE (account_uuid IS NOT NULL AND account_uuid = ?) OR email = ?",
            (account_uuid or "", email),
        )
        row = cur.fetchone()

        if row:
            sub_id = row["id"]
            conn.execute(
                """
                UPDATE subscriptions
                SET provider = ?,
                    account_uuid = COALESCE(?, account_uuid),
                    email = ?,
                    organization_name = COALESCE(?, organization_name),
                    organization_uuid = COALESCE(?, organization_uuid),
                    keychain_service = COALESCE(?, keychain_service),
                    is_active = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    provider,
                    account_uuid,
                    email,
                    organization_name,
                    organization_uuid,
                    keychain_service,
                    1 if is_active else 0,
                    now_str,
                    sub_id,
                ),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO subscriptions (
                    provider, account_uuid, email, organization_name,
                    organization_uuid, keychain_service, is_active, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    account_uuid,
                    email,
                    organization_name,
                    organization_uuid,
                    keychain_service,
                    1 if is_active else 0,
                    now_str,
                ),
            )
            sub_id = cur.lastrowid

        res = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        return dict(res)


def has_passed(iso_str: str | None) -> bool:
    """True when an ISO timestamp lies in the past."""
    if not iso_str:
        return False
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt <= datetime.now(UTC)


def format_countdown(iso_str: str | None) -> str:
    """Format an ISO timestamp into a human-readable countdown string."""
    if not iso_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        diff_s = int((dt - now).total_seconds())
        if diff_s <= 0:
            return "Refreshing now"
        hours = diff_s // 3600
        mins = (diff_s % 3600) // 60
        local_dt = dt.astimezone()
        time_str = local_dt.strftime("%I:%M %p").lstrip("0")
        if hours >= 24:
            days = hours // 24
            rem_h = hours % 24
            date_str = local_dt.strftime("%b %d")
            return f"in {days}d {rem_h}h ({date_str})"
        return f"in {hours}h {mins}m ({time_str})"
    except Exception:
        return "Unknown"


def get_subscriptions(db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Retrieve all subscriptions with their latest quota snapshot."""
    with get_db(db_path) as conn:
        # Unidentified accounts are excluded: they carry no readings, so a card
        # for one would render an authoritative-looking 0% against nothing.
        subs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM subscriptions WHERE email NOT LIKE ? ORDER BY id ASC",
                (f"%{PLACEHOLDER_EMAIL_DOMAIN}",),
            ).fetchall()
        ]
        for sub in subs:
            # The newest snapshot, full stop. Filtering on a non-null
            # five_hour_pct used to skip idle-window rows and surface a reading
            # hours old, which then displayed as a pre-reset value.
            latest = conn.execute(
                """
                SELECT * FROM quota_snapshots
                WHERE subscription_id = ?
                ORDER BY timestamp DESC, id DESC LIMIT 1
                """,
                (sub["id"],),
            ).fetchone()
            if latest:
                snap_dict = dict(latest)
                snap_dict["five_hour_countdown"] = format_countdown(snap_dict.get("five_hour_resets_at"))
                snap_dict["seven_day_countdown"] = format_countdown(snap_dict.get("seven_day_resets_at"))
                snap_dict["scoped_countdown"] = format_countdown(snap_dict.get("scoped_resets_at"))
                # A reading whose own reset moment has passed describes a window
                # that no longer exists, so its percentages are not current.
                snap_dict["is_expired"] = has_passed(snap_dict.get("five_hour_resets_at"))
                snap_dict["is_stale"] = bool(snap_dict.get("is_stale"))
                sub["latest_snapshot"] = snap_dict
            else:
                sub["latest_snapshot"] = None
        return subs


def get_latest_snapshot(subscription_id: int, db_path: Path | str | None = None) -> dict[str, Any] | None:
    """Get the most recent snapshot for a given subscription."""
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM quota_snapshots
            WHERE subscription_id = ?
            ORDER BY timestamp DESC, id DESC LIMIT 1
            """,
            (subscription_id,),
        ).fetchone()
        return dict(row) if row else None


def record_event(
    subscription_id: int,
    event_type: str,
    description: str,
    metadata: dict[str, Any] | None = None,
    timestamp: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Record a quota or reset event."""
    ts = timestamp or datetime.now(UTC).isoformat()
    meta_json = json.dumps(metadata or {})
    with get_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO quota_events (subscription_id, timestamp, event_type, description, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (subscription_id, ts, event_type, description, meta_json),
        )
        return cur.lastrowid


def _parse_iso(iso_str: str | None) -> datetime | None:
    """Parse an ISO timestamp to an aware UTC datetime, or None."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# Anthropic re-reports the 5-hour deadline with a few seconds of jitter between
# polls, so a genuinely new window has to move it by more than that.
RESET_WINDOW_TOLERANCE_S = 60.0


def is_five_hour_reset(
    prev_pct: float,
    new_pct: float,
    prev_resets_at: str | None,
    new_resets_at: str | None,
    observed_at: str | None = None,
) -> bool:
    """Decide whether two consecutive readings straddle a 5-hour window reset.

    A large drop in utilization is always a reset. A small drop (a lightly used
    window, e.g. 13% -> 0%) is only a reset when the window itself changed: the
    previous deadline has passed, the deadline jumped to a new window, or the API
    reports no open window at all (idle between sessions).
    """
    if new_pct >= prev_pct:
        return False

    if new_pct < prev_pct - 15.0:
        return True

    prev_deadline = _parse_iso(prev_resets_at)
    if prev_deadline is None:
        return False

    now = _parse_iso(observed_at) or datetime.now(UTC)
    if prev_deadline <= now:
        return True

    if new_resets_at is None:
        return True

    new_deadline = _parse_iso(new_resets_at)
    if new_deadline is None:
        return False
    return abs((new_deadline - prev_deadline).total_seconds()) > RESET_WINDOW_TOLERANCE_S


def insert_snapshot(
    subscription_id: int,
    five_hour_pct: float | None,
    five_hour_resets_at: str | None,
    seven_day_pct: float | None,
    seven_day_resets_at: str | None,
    scoped_model: str | None = None,
    scoped_pct: float | None = None,
    scoped_resets_at: str | None = None,
    spend_used: float | None = None,
    spend_limit: float | None = None,
    spend_currency: str | None = None,
    raw_data: dict[str, Any] | None = None,
    timestamp: str | None = None,
    is_stale: bool = False,
    db_path: Path | str | None = None,
) -> int:
    """Record a new quota snapshot and automatically detect reset/exhaustion events."""
    ts = timestamp or datetime.now(UTC).isoformat()
    raw_json = json.dumps(raw_data) if raw_data is not None else None

    prev = get_latest_snapshot(subscription_id, db_path=db_path)

    with get_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO quota_snapshots (
                subscription_id, timestamp, five_hour_pct, five_hour_resets_at,
                seven_day_pct, seven_day_resets_at, scoped_model, scoped_pct,
                scoped_resets_at, spend_used, spend_limit, spend_currency,
                is_stale, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription_id,
                ts,
                five_hour_pct,
                five_hour_resets_at,
                seven_day_pct,
                seven_day_resets_at,
                scoped_model,
                scoped_pct,
                scoped_resets_at,
                spend_used,
                spend_limit,
                spend_currency,
                1 if is_stale else 0,
                raw_json,
            ),
        )
        snap_id = cur.lastrowid

    # Reset & Threshold Event Detection. Cached readings repeat an older
    # measurement, so treating them as new observations would fabricate
    # phantom refresh/exhaustion events.
    if is_stale:
        return snap_id

    if prev and five_hour_pct is not None:
        prev_5h = prev.get("five_hour_pct")
        if prev_5h is not None:
            if is_five_hour_reset(
                prev_pct=prev_5h,
                new_pct=five_hour_pct,
                prev_resets_at=prev.get("five_hour_resets_at"),
                new_resets_at=five_hour_resets_at,
                observed_at=ts,
            ):
                record_event(
                    subscription_id=subscription_id,
                    event_type="five_hour_reset",
                    description=f"5-Hour Quota Refreshed: Dropped from {prev_5h:.1f}% to {five_hour_pct:.1f}%",
                    metadata={"previous_pct": prev_5h, "new_pct": five_hour_pct, "resets_at": five_hour_resets_at},
                    timestamp=ts,
                    db_path=db_path,
                )
            # Reached full exhaustion (100%)
            elif five_hour_pct >= 100.0 and prev_5h < 100.0:
                record_event(
                    subscription_id=subscription_id,
                    event_type="quota_exhausted",
                    description=f"5-Hour Quota Exhausted (100% reached). Resets at {five_hour_resets_at or 'unknown'}",
                    metadata={"resets_at": five_hour_resets_at},
                    timestamp=ts,
                    db_path=db_path,
                )

    if prev and seven_day_pct is not None:
        prev_7d = prev.get("seven_day_pct")
        if prev_7d is not None and seven_day_pct < prev_7d - 10.0:
            record_event(
                subscription_id=subscription_id,
                event_type="seven_day_reset",
                description=f"7-Day Quota Refreshed: Dropped from {prev_7d:.1f}% to {seven_day_pct:.1f}%",
                metadata={"previous_pct": prev_7d, "new_pct": seven_day_pct, "resets_at": seven_day_resets_at},
                timestamp=ts,
                db_path=db_path,
            )

    return snap_id


def get_snapshots(
    subscription_ids: list[int] | None = None,
    date_str: str | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
    limit: int = 2000,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Query time-series quota snapshots with flexible filtering (date, hour window)."""
    with get_db(db_path) as conn:
        conditions: list[str] = []
        params: list[Any] = []

        if subscription_ids:
            placeholders = ",".join("?" for _ in subscription_ids)
            conditions.append(f"subscription_id IN ({placeholders})")
            params.extend(subscription_ids)

        if date_str:
            # Match date in user local timezone
            conditions.append("strftime('%Y-%m-%d', datetime(timestamp, 'localtime')) = ?")
            params.append(date_str)

        if start_hour is not None:
            conditions.append("CAST(strftime('%H', datetime(timestamp, 'localtime')) AS INTEGER) >= ?")
            params.append(int(start_hour))

        if end_hour is not None:
            conditions.append("CAST(strftime('%H', datetime(timestamp, 'localtime')) AS INTEGER) <= ?")
            params.append(int(end_hour))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT * FROM quota_snapshots
            {where_clause}
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_events(
    subscription_ids: list[int] | None = None,
    date_str: str | None = None,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve recorded quota reset and threshold events."""
    with get_db(db_path) as conn:
        conditions: list[str] = []
        params: list[Any] = []

        if subscription_ids:
            placeholders = ",".join("?" for _ in subscription_ids)
            conditions.append(f"e.subscription_id IN ({placeholders})")
            params.extend(subscription_ids)

        if date_str:
            conditions.append("strftime('%Y-%m-%d', datetime(e.timestamp, 'localtime')) = ?")
            params.append(date_str)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT e.*, s.email, s.organization_name
            FROM quota_events e
            JOIN subscriptions s ON s.id = e.subscription_id
            {where_clause}
            ORDER BY e.timestamp DESC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("metadata_json"):
                try:
                    d["metadata"] = json.loads(d["metadata_json"])
                except Exception:
                    d["metadata"] = {}
            result.append(d)
        return result
