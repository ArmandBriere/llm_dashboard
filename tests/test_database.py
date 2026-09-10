"""Unit tests for SQLite database operations and reset event detection."""

import os
import tempfile
from pathlib import Path

import pytest
from backend.database import (
    get_events,
    get_latest_snapshot,
    get_snapshots,
    get_subscriptions,
    init_db,
    insert_snapshot,
    upsert_subscription,
)


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    yield db_path
    if db_path.exists():
        os.unlink(db_path)


def test_init_and_upsert_subscription(temp_db):
    sub = upsert_subscription(
        email="test@vooban.com",
        account_uuid="uuid-1234",
        organization_name="Vooban",
        organization_uuid="org-1234",
        is_active=True,
        db_path=temp_db,
    )
    assert sub["id"] == 1
    assert sub["email"] == "test@vooban.com"
    assert sub["organization_name"] == "Vooban"
    assert sub["is_active"] == 1

    # Update subscription
    sub_updated = upsert_subscription(
        email="test@vooban.com",
        account_uuid="uuid-1234",
        organization_name="Vooban Updated",
        is_active=False,
        db_path=temp_db,
    )
    assert sub_updated["id"] == 1
    assert sub_updated["organization_name"] == "Vooban Updated"
    assert sub_updated["is_active"] == 0


def test_insert_snapshot_and_event_detection(temp_db):
    sub = upsert_subscription(email="dev@vooban.com", db_path=temp_db)
    sub_id = sub["id"]

    # Initial snapshot: 100% quota used (exhausted)
    snap1_id = insert_snapshot(
        subscription_id=sub_id,
        five_hour_pct=100.0,
        five_hour_resets_at="2026-09-09T02:00:00Z",
        seven_day_pct=30.0,
        seven_day_resets_at="2026-09-13T18:00:00Z",
        spend_used=50.0,
        spend_limit=100.0,
        spend_currency="CAD",
        timestamp="2026-09-08T10:00:00Z",
        db_path=temp_db,
    )
    assert snap1_id == 1

    latest = get_latest_snapshot(sub_id, db_path=temp_db)
    assert latest["five_hour_pct"] == 100.0

    # Second snapshot: Quota drops to 10.0% -> Triggers five_hour_reset event!
    snap2_id = insert_snapshot(
        subscription_id=sub_id,
        five_hour_pct=10.0,
        five_hour_resets_at="2026-09-09T07:00:00Z",
        seven_day_pct=30.0,
        seven_day_resets_at="2026-09-13T18:00:00Z",
        timestamp="2026-09-08T10:05:00Z",
        db_path=temp_db,
    )
    assert snap2_id == 2

    events = get_events(subscription_ids=[sub_id], db_path=temp_db)
    assert len(events) >= 1
    assert events[0]["event_type"] == "five_hour_reset"
    assert "Dropped from 100.0% to 10.0%" in events[0]["description"]


def test_snapshot_filtering(temp_db):
    sub = upsert_subscription(email="filter@vooban.com", db_path=temp_db)
    sub_id = sub["id"]

    # Local time is EDT (UTC-4).
    # 06:30 local = 10:30 UTC
    # 14:15 local = 18:15 UTC
    # 22:00 local = 02:00 UTC (next day 2026-09-09)
    insert_snapshot(
        subscription_id=sub_id,
        five_hour_pct=50.0,
        five_hour_resets_at=None,
        seven_day_pct=20.0,
        seven_day_resets_at=None,
        timestamp="2026-09-08T10:30:00+00:00",
        db_path=temp_db,
    )
    insert_snapshot(
        subscription_id=sub_id,
        five_hour_pct=60.0,
        five_hour_resets_at=None,
        seven_day_pct=22.0,
        seven_day_resets_at=None,
        timestamp="2026-09-08T18:15:00+00:00",
        db_path=temp_db,
    )
    insert_snapshot(
        subscription_id=sub_id,
        five_hour_pct=70.0,
        five_hour_resets_at=None,
        seven_day_pct=25.0,
        seven_day_resets_at=None,
        timestamp="2026-09-09T02:00:00+00:00",
        db_path=temp_db,
    )

    # Filter 7am to 9pm (07 to 21) on 2026-09-08 local
    results = get_snapshots(
        subscription_ids=[sub_id],
        date_str="2026-09-08",
        start_hour=7,
        end_hour=21,
        db_path=temp_db,
    )
    assert len(results) == 1
    assert results[0]["five_hour_pct"] == 60.0

    # Full day filter
    all_day = get_snapshots(
        subscription_ids=[sub_id],
        date_str="2026-09-08",
        start_hour=0,
        end_hour=23,
        db_path=temp_db,
    )
    assert len(all_day) == 3


def test_small_drop_with_new_window_is_reset(temp_db):
    """A lightly used window (13% -> 0%) still counts as a reset when the deadline moves."""
    sub = upsert_subscription(email="small@example.com", organization_name="Small", db_path=temp_db)
    insert_snapshot(
        subscription_id=sub["id"],
        five_hour_pct=13.0,
        five_hour_resets_at="2026-09-09T17:40:00Z",
        seven_day_pct=44.0,
        seven_day_resets_at="2026-09-13T18:00:00Z",
        timestamp="2026-09-09T17:37:00+00:00",
        db_path=temp_db,
    )
    # Idle: API reports 0% and no open window right after the deadline passed.
    insert_snapshot(
        subscription_id=sub["id"],
        five_hour_pct=0.0,
        five_hour_resets_at=None,
        seven_day_pct=44.0,
        seven_day_resets_at="2026-09-13T18:00:00Z",
        timestamp="2026-09-09T17:40:18+00:00",
        db_path=temp_db,
    )
    events = get_events(subscription_ids=[sub["id"]], db_path=temp_db)
    assert [e["event_type"] for e in events] == ["five_hour_reset"]

    # Same window, tiny jitter on the deadline, usage flat: no new event.
    insert_snapshot(
        subscription_id=sub["id"],
        five_hour_pct=2.0,
        five_hour_resets_at="2026-09-09T22:40:00Z",
        seven_day_pct=44.0,
        seven_day_resets_at="2026-09-13T18:00:00Z",
        timestamp="2026-09-09T17:42:00+00:00",
        db_path=temp_db,
    )
    insert_snapshot(
        subscription_id=sub["id"],
        five_hour_pct=1.0,
        five_hour_resets_at="2026-09-09T22:40:03Z",
        seven_day_pct=44.0,
        seven_day_resets_at="2026-09-13T18:00:00Z",
        timestamp="2026-09-09T17:47:00+00:00",
        db_path=temp_db,
    )
    events = get_events(subscription_ids=[sub["id"]], db_path=temp_db)
    assert len(events) == 1
