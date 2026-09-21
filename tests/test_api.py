"""Integration tests for the FastAPI endpoints.

The app is exercised end to end, but against a temporary database, a fake
provider and an empty Claude home, so the suite runs the same on a machine
with no keychain or transcripts (CI) as on a developer's Mac.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import collector as collector_module
from backend import database, usage
from backend.main import app
from backend.providers.base import BaseProvider


class FakeProvider(BaseProvider):
    """Two accounts with fixed quota readings."""

    calls = 0

    @property
    def provider_id(self) -> str:
        return "fake"

    @property
    def display_name(self) -> str:
        return "Fake"

    async def discover_and_fetch_all(self) -> list[dict[str, Any]]:
        FakeProvider.calls += 1
        return [
            _account("u1", "one@example.com", "One", 40.0, True),
            _account("u2", "two@example.com", "Two", 75.0, False),
        ]


def _account(uuid: str, email: str, org: str, pct: float, active: bool) -> dict[str, Any]:
    return {
        "account_uuid": uuid,
        "email": email,
        "organization_name": org,
        "organization_uuid": f"org-{uuid}",
        "keychain_service": "Claude Code-credentials",
        "is_active": active,
        "quota": {
            "five_hour_pct": pct,
            "five_hour_resets_at": "2030-01-01T00:00:00Z",
            "seven_day_pct": pct / 2,
            "seven_day_resets_at": "2030-01-07T00:00:00Z",
            "scoped_model": None,
            "scoped_pct": None,
            "scoped_resets_at": None,
            "spend_used": None,
            "spend_limit": None,
            "spend_currency": None,
            "is_stale": False,
            "raw": {},
        },
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(database, "_initialized", False)
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    real_scan = usage.scan_transcripts
    monkeypatch.setattr(collector_module, "scan_transcripts", lambda: real_scan(user_home=empty_home))
    monkeypatch.setattr(collector_module.collector, "providers", [FakeProvider()])
    FakeProvider.calls = 0
    with TestClient(app) as test_client:
        yield test_client


def test_status_reports_the_configured_interval(client):
    data = client.get("/api/status").json()
    assert data["status"] == "ok"
    assert data["interval_seconds"] == collector_module.collector.interval_seconds
    assert data["last_run_time"] is not None
    assert data["usage_scan"]["files_scanned"] == 0


def test_startup_collects_once_and_records_every_account(client):
    assert FakeProvider.calls == 1
    subs = client.get("/api/subscriptions").json()
    assert [s["email"] for s in subs] == ["one@example.com", "two@example.com"]
    assert {s["is_active"] for s in subs} == {0, 1}

    snaps = client.get("/api/snapshots").json()
    assert len(snaps) == 2
    assert sorted(s["five_hour_pct"] for s in snaps) == [40.0, 75.0]


def test_refresh_runs_another_pass(client):
    resp = client.post("/api/refresh")
    assert resp.status_code == 200
    assert resp.json()["summary"]["collected_accounts"] == 2
    assert FakeProvider.calls == 2
    assert len(client.get("/api/snapshots").json()) == 4


def test_filters_parse_subscription_ids(client):
    subs = client.get("/api/subscriptions").json()
    one = next(s["id"] for s in subs if s["email"] == "one@example.com")
    snaps = client.get(f"/api/snapshots?subscription_ids={one},abc").json()
    assert [s["subscription_id"] for s in snaps] == [one]

    assert client.get("/api/events").json() == []
    assert client.get("/api/snapshots?start_hour=25").status_code == 422


def test_usage_endpoints_answer_on_an_empty_index(client):
    assert client.get("/api/usage/summary").status_code == 200
    assert client.get("/api/usage/models").json() == []
    assert client.get("/api/usage/skills").status_code == 200
    assert client.get("/api/usage/tools").status_code == 200
    assert client.get("/api/usage/timeline?bucket=week").status_code == 422


def test_frontend_is_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Quota Console" in resp.text
    assert resp.headers["cache-control"].startswith("no-cache")
    assert client.get("/static/app.js").status_code == 200
